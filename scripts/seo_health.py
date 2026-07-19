#!/usr/bin/env python3
"""
MigrateState — SEO health check & auto-repair.

Run from the repo root:
    python3 scripts/seo_health.py           # check only, exit 1 if problems
    python3 scripts/seo_health.py --fix     # repair what is safely repairable, then check

Why this exists
---------------
The content engine adds new pages by copying existing templates. It copies the
*tags* (e.g. <meta property="og:image" content=".../og/<slug>.png">) but nothing
generates the referenced PNG, and older pages never learn about new country hubs.
That regressed silently: on 2026-07-19, 8 live pages pointed at 404 OG images.

Everything here is idempotent — safe to run on every weekly pass.

WHAT IT AUTO-FIXES (--fix)
  1. Missing OG card PNGs        -> renders a branded 1200x630 card from the page title
  2. Missing og:image/url/type/site_name, twitter:* -> injected into <head>
  3. Missing BreadcrumbList      -> built from hub membership (guide-card links)
  4. Stale footer hub index      -> re-rendered with every current *-guides page
  5. sitemap.xml                 -> rebuilt from the filesystem, lastmod from git
  6. CollectionPage ItemList     -> regenerated from the hub's real guide cards

WHAT IT ONLY REPORTS (needs a human)
  - broken internal links, unbalanced <div>, invalid JSON-LD, truncated files
  - titles >65 chars, meta descriptions >165 chars, missing description
  - orphan pages (<2 inbound links), missing GA / HubSpot tags
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict

BASE = "https://migratestate.com"
GA_ID = "G-46194TH8M5"
HS_LOADER = "hs-script-loader"

# Pages intentionally minimal — excluded from marketing-tag and sitemap checks.
BARE_PAGES = {"privacy", "terms"}

# Hub labels that don't derive cleanly from the slug.
HUB_LABELS = {
    "general-guides": "General",
    "indonesia-guides": "Bali &amp; Indonesia",
    "dominican-republic-guides": "Dominican Republic",
    "costa-rica-guides": "Costa Rica",
}

FONT_SERIF = "/usr/share/fonts/truetype/google-fonts/Lora-Variable.ttf"
FONT_SANS_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
FONT_SANS = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"

NAVY = (15, 42, 67)
GOLD = (200, 162, 74)
CREAM = (247, 244, 238)
MUTED = (150, 168, 186)
CARD_W, CARD_H = 1200, 630


# ---------------------------------------------------------------- helpers


def hub_label(slug: str) -> str:
    return HUB_LABELS.get(slug, slug[:-7].replace("-", " ").title())


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def clean_title(raw: str) -> str:
    """Page title without the site suffix, entities decoded."""
    return re.sub(r"\s*\|\s*MigrateState\s*$", "", html.unescape(strip_tags(raw))).strip()


def page_title(doc: str) -> str:
    m = re.search(r"<title>(.*?)</title>", doc, re.S)
    return m.group(1) if m else ""


def head_of(doc: str) -> str:
    i = doc.find("</head>")
    return doc[:i] if i != -1 else doc


def git_lastmod(path: str, repo_root: str) -> str:
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cs", "--", path],
            cwd=repo_root, capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        return out or "1970-01-01"
    except Exception:
        return "1970-01-01"


def og_title_of(doc: str, slug: str) -> str:
    for pat in (
        r'<meta property="og:title" content="(.*?)"',
        r"<h1[^>]*>(.*?)</h1>",
        r"<title>(.*?)</title>",
    ):
        m = re.search(pat, doc, re.S)
        if m:
            return clean_title(m.group(1))
    return slug.replace("-", " ").title()


# ------------------------------------------------------------ OG rendering


def render_og_card(slug: str, title: str, out_dir: str) -> None:
    """Branded navy/gold card matching the site palette."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (CARD_W, CARD_H), NAVY)
    d = ImageDraw.Draw(img)

    for y in range(CARD_H):  # vertical gradient
        f = y / CARD_H
        d.line([(0, y), (CARD_W, y)],
               fill=(int(15 + 7 * f), int(42 + 16 * f), int(67 + 25 * f)))

    d.rectangle([0, 0, 10, CARD_H], fill=GOLD)              # left accent bar
    d.rounded_rectangle([64, 54, 116, 106], 12, fill=GOLD)  # logo mark

    mark = ImageFont.truetype(FONT_SERIF, 34)
    mark.set_variation_by_axes([700])
    d.text((90, 80), "M", font=mark, fill=NAVY, anchor="mm")
    d.text((132, 80), "MigrateState",
           font=ImageFont.truetype(FONT_SANS_BOLD, 26), fill=CREAM, anchor="lm")

    def wrap(text, font, max_w):
        lines, cur = [], ""
        for word in text.split():
            trial = (cur + " " + word).strip()
            if d.textlength(trial, font=font) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines

    size = 64
    while size >= 34:  # shrink until it fits in 4 lines
        font = ImageFont.truetype(FONT_SERIF, size)
        font.set_variation_by_axes([600])
        lines = wrap(title, font, CARD_W - 128)
        if len(lines) <= 4:
            break
        size -= 4

    line_h = int(size * 1.28)
    y = CARD_H - 150 - len(lines) * line_h
    for line in lines:
        d.text((64, y), line, font=font, fill=CREAM)
        y += line_h

    d.rectangle([64, CARD_H - 116, 184, CARD_H - 112], fill=GOLD)
    d.text((64, CARD_H - 80), "Lawyer-reviewed guides for Americans buying abroad",
           font=ImageFont.truetype(FONT_SANS, 24), fill=MUTED)

    img.save(os.path.join(out_dir, f"{slug}.png"), "PNG", optimize=True)


# ------------------------------------------------------------- site model


class Site:
    def __init__(self, public_dir: str, repo_root: str):
        self.dir = public_dir
        self.repo_root = repo_root
        self.og_dir = os.path.join(public_dir, "og")
        self.files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(public_dir, "*.html")))
        self.slugs = {f[:-5] for f in self.files}
        self.hubs = sorted(s for s in self.slugs if s.endswith("-guides"))
        self.docs = {f[:-5]: self._read(f) for f in self.files}
        self.hub_of, self.hub_name = self._map_hubs()

    def _read(self, fname: str) -> str:
        with open(os.path.join(self.dir, fname), encoding="utf-8") as fh:
            return fh.read()

    def write(self, slug: str, doc: str) -> None:
        with open(os.path.join(self.dir, slug + ".html"), "w", encoding="utf-8") as fh:
            fh.write(doc)
        self.docs[slug] = doc

    def _map_hubs(self):
        """article -> hub, derived ONLY from guide cards (not nav/footer links)."""
        hub_of, hub_name = {}, {}
        for hub in self.hubs:
            doc = self.docs[hub]
            m = re.search(r"<h1[^>]*>(.*?)</h1>", doc, re.S)
            hub_name[hub] = html.unescape(strip_tags(m.group(1))).strip() if m else hub
            for href in re.findall(r'<a class="guide" href="([^"#?]+)"', doc):
                target = href.rstrip("/")
                if target in self.slugs:
                    hub_of.setdefault(target, hub)
        return hub_of, hub_name

    def url(self, slug: str) -> str:
        return f"{BASE}/" if slug == "index" else f"{BASE}/{slug}"

    def guide_cards(self, hub: str) -> list[str]:
        seen = dict.fromkeys(
            h.rstrip("/") for h in re.findall(r'<a class="guide" href="([^"#?]+)"', self.docs[hub])
        )
        return [s for s in seen if s in self.slugs]

    def breadcrumb(self, slug: str) -> dict:
        items = [{"@type": "ListItem", "position": 1, "name": "Home", "item": f"{BASE}/"}]
        pos = 2
        hub = self.hub_of.get(slug)
        if hub:
            items.append({"@type": "ListItem", "position": pos,
                          "name": self.hub_name[hub], "item": f"{BASE}/{hub}"})
            pos += 1
        items.append({"@type": "ListItem", "position": pos,
                      "name": clean_title(page_title(self.docs[slug])), "item": self.url(slug)})
        return {"@type": "BreadcrumbList", "itemListElement": items}

    def footer_nav(self) -> str:
        return "<nav>" + "".join(
            f'<a href="{h}">{hub_label(h)}</a>' for h in self.hubs
        ) + "</nav>"


# ----------------------------------------------------------------- fixers


def fix_og_images(site: Site, log: list, warnings: list) -> None:
    """Render any missing OG card. Never fatal — this runs inside a deploy."""
    os.makedirs(site.og_dir, exist_ok=True)
    missing = [s for s in sorted(site.slugs)
               if not os.path.exists(os.path.join(site.og_dir, f"{s}.png"))]
    if not missing:
        return
    try:
        import PIL  # noqa: F401
    except ImportError:
        warnings.append(
            f"Pillow not installed — {len(missing)} OG card(s) not rendered "
            f"({', '.join(missing[:5])}{'...' if len(missing) > 5 else ''}). "
            f"Install with: pip install Pillow --break-system-packages")
        return
    for slug in missing:
        try:
            render_og_card(slug, og_title_of(site.docs[slug], slug), site.og_dir)
            log.append(f"generated OG card: og/{slug}.png")
        except Exception as exc:  # a bad font/title must not break the deploy
            warnings.append(f"could not render og/{slug}.png: {exc}")


def fix_head_tags(site: Site, log: list) -> None:
    for slug in sorted(site.slugs):
        doc = site.docs[slug]
        cut = doc.find("</head>")
        if cut == -1:
            continue
        head, rest = doc[:cut], doc[cut:]
        before = head

        title = page_title(doc)
        desc_m = re.search(r'<meta name="description" content="(.*?)"', head, re.S)
        desc = desc_m.group(1) if desc_m else ""
        og_img = f"{BASE}/og/{slug}.png"

        additions = []
        if 'property="og:title"' not in head:
            additions.append(f'<meta property="og:title" content="{title}">')
        if 'property="og:description"' not in head:
            additions.append(f'<meta property="og:description" content="{desc}">')
        if 'property="og:type"' not in head:
            kind = "website" if slug.endswith("-guides") or slug == "index" else "article"
            additions.append(f'<meta property="og:type" content="{kind}">')
        if 'property="og:url"' not in head:
            additions.append(f'<meta property="og:url" content="{site.url(slug)}">')
        if 'property="og:site_name"' not in head:
            additions.append('<meta property="og:site_name" content="MigrateState">')
        if 'property="og:image"' not in head:
            alt = html.escape(clean_title(title), quote=True)
            additions += [
                f'<meta property="og:image" content="{og_img}">',
                '<meta property="og:image:width" content="1200">',
                '<meta property="og:image:height" content="630">',
                f'<meta property="og:image:alt" content="{alt}">',
            ]
        if "twitter:card" not in head:
            additions += [
                '<meta name="twitter:card" content="summary_large_image">',
                f'<meta name="twitter:title" content="{title}">',
                f'<meta name="twitter:description" content="{desc}">',
                f'<meta name="twitter:image" content="{og_img}">',
            ]

        if additions:
            head = head.rstrip() + "\n" + "\n".join(additions) + "\n"

        # Repoint og:image/twitter:image at a card that actually exists.
        def repoint(m):
            url = m.group(2)
            fname = url.rsplit("/", 1)[-1] if "/og/" in url else None
            if fname and os.path.exists(os.path.join(site.og_dir, fname)):
                return m.group(0)
            return f'{m.group(1)}"{og_img}"'

        head = re.sub(r'(<meta property="og:image" content=)"([^"]+)"', repoint, head)
        head = re.sub(r'(<meta name="twitter:image" content=)"([^"]+)"', repoint, head)

        if head != before:
            site.write(slug, head + rest)
            log.append(f"head tags repaired: {slug}")


def fix_schema(site: Site, log: list) -> None:
    for slug in sorted(site.slugs):
        if slug in BARE_PAGES:
            continue
        doc = site.docs[slug]
        og_img = f"{BASE}/og/{slug}.png"
        m = re.search(r'<script type="application/ld\+json">(.*?)</script>', doc, re.S)

        # A breadcrumb may live in any block; only add one if the page has none.
        has_breadcrumb = False
        for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', doc, re.S):
            try:
                d = json.loads(block)
            except json.JSONDecodeError:
                continue
            g = d.get("@graph", [d]) if isinstance(d, dict) else d
            if any(isinstance(n, dict) and n.get("@type") == "BreadcrumbList" for n in g):
                has_breadcrumb = True
                break

        if m:
            try:
                data = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue  # reported, never auto-edited
            if not (isinstance(data, dict) and "@graph" in data):
                data = {"@context": "https://schema.org",
                        "@graph": data if isinstance(data, list) else [data]}
            graph = data["@graph"]
            changed = False

            for node in graph:
                if node.get("@type") in ("Article", "BlogPosting") and "image" not in node:
                    node["image"] = {"@type": "ImageObject", "url": og_img,
                                     "width": 1200, "height": 630}
                    changed = True
                if node.get("@type") == "CollectionPage" and slug in site.hubs:
                    cards = site.guide_cards(slug)
                    if cards:
                        want = {"@type": "ItemList", "numberOfItems": len(cards),
                                "itemListElement": [
                                    {"@type": "ListItem", "position": i + 1,
                                     "url": f"{BASE}/{c}"} for i, c in enumerate(cards)]}
                        if node.get("mainEntity") != want:
                            node["mainEntity"] = want
                            changed = True

            if not has_breadcrumb:
                graph.append(site.breadcrumb(slug))
                changed = True

            if changed:
                block = json.dumps(data, ensure_ascii=False, indent=2)
                site.write(slug, doc[:m.start()] +
                           f'<script type="application/ld+json">\n{block}\n</script>' +
                           doc[m.end():])
                log.append(f"schema repaired: {slug}")
        else:
            kind = "CollectionPage" if slug in site.hubs else "WebPage"
            desc_m = re.search(r'<meta name="description" content="(.*?)"', doc, re.S)
            node = {
                "@type": kind,
                "name": clean_title(page_title(doc)),
                "description": html.unescape(desc_m.group(1)) if desc_m else "",
                "url": site.url(slug),
                "inLanguage": "en-US",
                "isPartOf": {"@type": "WebSite", "name": "MigrateState", "url": f"{BASE}/"},
                "primaryImageOfPage": {"@type": "ImageObject", "url": og_img,
                                       "width": 1200, "height": 630},
            }
            if slug in site.hubs:
                cards = site.guide_cards(slug)
                if cards:
                    node["mainEntity"] = {
                        "@type": "ItemList", "numberOfItems": len(cards),
                        "itemListElement": [{"@type": "ListItem", "position": i + 1,
                                             "url": f"{BASE}/{c}"} for i, c in enumerate(cards)]}
            data = {"@context": "https://schema.org", "@graph": [node, site.breadcrumb(slug)]}
            block = json.dumps(data, ensure_ascii=False, indent=2)
            site.write(slug, doc.replace(
                "</head>", f'<script type="application/ld+json">\n{block}\n</script>\n</head>', 1))
            log.append(f"schema created: {slug}")


def fix_footer_hubs(site: Site, log: list) -> None:
    """Keep the country-hub index in sync so new hubs are never orphaned."""
    nav = site.footer_nav()
    for slug in sorted(site.slugs):
        doc = site.docs[slug]
        if "footer-hubs" in doc:
            new = re.sub(
                r'(<div class="footer-hubs">\s*<h4>Guides by country</h4>\s*)<nav>.*?</nav>',
                lambda m: m.group(1) + nav, doc, flags=re.S)
            if new != doc:
                site.write(slug, new)
                log.append(f"footer hub index refreshed: {slug}")
            continue

        block = (f'<div class="footer-hubs">\n      <h4>Guides by country</h4>\n'
                 f'      {nav}\n    </div>\n    ')
        for anchor in (r'<div class="disclaimer">',
                       r'<p style="margin-top:32px',
                       r'<p class="footer-copy">'):
            m = re.search(anchor, doc)
            if m:
                site.write(slug, doc[:m.start()] + block + doc[m.start():])
                log.append(f"footer hub index added: {slug}")
                break


def fix_sitemap(site: Site, log: list) -> None:
    rows = []
    for slug in sorted(site.slugs):
        if slug in BARE_PAGES:
            continue
        if slug == "index":
            pri = "1.0"
        elif slug in ("best-golden-visa-for-americans", "ultimate-guide-golden-visa", "get-started"):
            pri = "0.9"
        elif slug in site.hubs:
            pri = "0.7"
        else:
            pri = "0.8"
        rows.append((site.url(slug),
                     git_lastmod(f"public/{slug}.html", site.repo_root),
                     pri))

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for url, lastmod, pri in rows:
        out.append(f"  <url>\n    <loc>{url}</loc>\n    <lastmod>{lastmod}</lastmod>"
                   f"\n    <priority>{pri}</priority>\n  </url>")
    out.append("</urlset>")
    body = "\n".join(out) + "\n"

    path = os.path.join(site.dir, "sitemap.xml")
    old = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    if old != body:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        log.append(f"sitemap rebuilt: {len(rows)} URLs")


# ---------------------------------------------------------------- checkers


def run_checks(site: Site) -> dict:
    problems: dict[str, list] = defaultdict(list)
    inbound: Counter = Counter()

    sitemap_path = os.path.join(site.dir, "sitemap.xml")
    sitemap_slugs = set()
    if os.path.exists(sitemap_path):
        for loc in re.findall(r"<loc>(.*?)</loc>", open(sitemap_path, encoding="utf-8").read()):
            sitemap_slugs.add(loc.rstrip("/").split("/")[-1] or "index")

    for slug in sorted(site.slugs):
        doc = site.docs[slug]
        head = head_of(doc)

        # structural integrity
        if not doc.rstrip().endswith("</html>"):
            problems["truncated_file"].append(slug)
        if len(re.findall(r"<div\b", doc)) != doc.count("</div>"):
            problems["unbalanced_div"].append(slug)

        # meta
        title = page_title(doc)
        if not title:
            problems["missing_title"].append(slug)
        elif len(html.unescape(title)) > 65:
            problems["title_too_long"].append(f"{slug} ({len(html.unescape(title))})")
        desc_m = re.search(r'<meta name="description" content="(.*?)"', head, re.S)
        if not desc_m:
            problems["missing_description"].append(slug)
        elif len(html.unescape(desc_m.group(1))) > 165:
            problems["description_too_long"].append(
                f"{slug} ({len(html.unescape(desc_m.group(1)))})")
        if "<link rel=\"canonical\"" not in head:
            problems["missing_canonical"].append(slug)

        # OG card actually exists
        og_m = re.search(r'<meta property="og:image" content="([^"]+)"', head)
        if not og_m:
            problems["missing_og_image"].append(slug)
        elif "/og/" in og_m.group(1):
            fname = og_m.group(1).rsplit("/", 1)[-1]
            if not os.path.exists(os.path.join(site.og_dir, fname)):
                problems["og_image_404"].append(f"{slug} -> {fname}")
        if "twitter:card" not in head:
            problems["missing_twitter_card"].append(slug)

        # marketing tags
        if slug not in BARE_PAGES:
            if GA_ID not in head:
                problems["missing_ga_tag"].append(slug)
            if HS_LOADER not in head:
                problems["missing_hubspot_tag"].append(slug)
            if slug not in sitemap_slugs and slug != "index":
                problems["not_in_sitemap"].append(slug)
            if "footer-hubs" not in doc:
                problems["missing_footer_hub_index"].append(slug)

        # schema — aggregate every JSON-LD block, since a page may legitimately
        # split Article/BreadcrumbList and FAQPage across separate <script> tags.
        blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', doc, re.S)
        if not blocks and slug not in BARE_PAGES:
            problems["missing_schema"].append(slug)
        nodes = []
        for block in blocks:
            try:
                data = json.loads(block)
            except json.JSONDecodeError as exc:
                problems["invalid_json_ld"].append(f"{slug}: {exc}")
                continue
            graph = data.get("@graph", [data]) if isinstance(data, dict) else data
            nodes += [n for n in graph if isinstance(n, dict)]

        if blocks and slug not in BARE_PAGES and not any(
            n.get("@type") == "BreadcrumbList" for n in nodes
        ):
            problems["missing_breadcrumb"].append(slug)

        for node in nodes:
            if node.get("@type") == "CollectionPage" and slug in site.hubs:
                declared = node.get("mainEntity", {}).get("numberOfItems", 0)
                actual = len(site.guide_cards(slug))
                if declared != actual:
                    problems["itemlist_mismatch"].append(
                        f"{slug} (schema {declared} vs {actual} cards)")

        # breadcrumb points at the right hub
        crumb = re.search(r'<div class="breadcrumb">(.*?)</div>', doc, re.S)
        hub = site.hub_of.get(slug)
        if crumb and hub:
            hrefs = re.findall(r'href="([^"]+)"', crumb.group(1))
            if len(hrefs) < 2 or hrefs[1].rstrip("/") != hub:
                problems["breadcrumb_wrong_hub"].append(f"{slug} (expected {hub})")

        # links
        for m in re.finditer(r'href="(?!https?:|mailto:|tel:|#|/)([^"#?]+)"', doc):
            target = m.group(1).rstrip("/")
            if target.endswith((".css", ".js", ".png", ".jpg", ".svg", ".xml", ".txt", ".ico")):
                continue
            if target not in site.slugs:
                problems["broken_internal_link"].append(f"{slug} -> {target}")
            elif target != slug:
                inbound[target] += 1

    for slug in sorted(site.slugs):
        if slug != "index" and inbound[slug] < 2:
            problems["orphan_page"].append(f"{slug} ({inbound[slug]} inbound)")

    for slug in sorted(site.slugs):
        if not slug.endswith("-guides") and slug not in BARE_PAGES and slug not in (
            "index", "get-started", "contact"
        ) and slug not in site.hub_of:
            problems["article_not_in_any_hub"].append(slug)

    return problems


# -------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="MigrateState SEO health check")
    ap.add_argument("--fix", action="store_true", help="auto-repair what is safe")
    ap.add_argument("--public", default="public", help="path to the public/ dir")
    args = ap.parse_args()

    public_dir = os.path.abspath(args.public)
    repo_root = os.path.dirname(public_dir)
    if not os.path.isdir(public_dir):
        print(f"ERROR: {public_dir} not found — run from the repo root.", file=sys.stderr)
        return 2

    site = Site(public_dir, repo_root)
    print(f"MigrateState SEO health — {len(site.files)} pages, {len(site.hubs)} hubs")

    if args.fix:
        log: list[str] = []
        warnings: list[str] = []
        fix_og_images(site, log, warnings)
        fix_head_tags(site, log)
        fix_schema(site, log)
        fix_footer_hubs(site, log)
        fix_sitemap(site, log)

        print(f"\nREPAIRS ({len(log)}):")
        if log:
            for line in log:
                print(f"  + {line}")
        else:
            print("  (nothing needed — site already consistent)")

        if warnings:
            print(f"\nWARNINGS ({len(warnings)}):")
            for line in warnings:
                print(f"  ! {line}")

        site = Site(public_dir, repo_root)  # reload after edits

    problems = run_checks(site)
    total = sum(len(v) for v in problems.values())

    print(f"\nCHECKS: {'PASS — no issues' if not total else f'{total} issue(s)'}")
    for kind in sorted(problems):
        items = problems[kind]
        print(f"\n  [{kind}] {len(items)}")
        for item in items[:10]:
            print(f"     - {item}")
        if len(items) > 10:
            print(f"     ... and {len(items) - 10} more")

    if total:
        print("\nThese need a human — auto-fix does not touch content or broken markup.")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
