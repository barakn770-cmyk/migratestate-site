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
  5. sitemap.xml                 -> rebuilt from the filesystem; lastmod matches schema dateModified
  6. CollectionPage ItemList     -> regenerated from the hub's real guide cards
  7. Sources block               -> official references from scripts/sources.json on every article
  8. Related guides              -> stable scored links (unchanged blocks are left alone)
  9. Hub FAQPage schema          -> built from the on-page FAQ
 10. Taxes & Money nav          -> points at the tax pillar, not the homepage guide grid
 11. AdSense                    -> stripped from lead pages, country hubs, and NO_ADS
 12. Mobile nav                 -> homepage #mobileMenu + wiring, and a header Get Matched button
 13. Mobile sticky CTA          -> bottom bar on article and hub pages
 14. Early CTA                  -> short Get Matched box after the answer box / Key Facts
 15. Checklist signup           -> MailerLite block before the end of every article
 16. Get-started countries      -> dropdown derived from country hub pages; dead # partner links removed

WHAT IT ONLY REPORTS (needs a human)
  - broken internal links, unbalanced <div>, invalid JSON-LD, truncated files
  - titles >65 chars, meta descriptions >165 chars, missing description
  - orphan pages (<2 inbound links), missing GA / HubSpot tags
"""

from __future__ import annotations

import argparse
import glob
import hashlib
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

# Font candidates, first existing file wins. The Linux paths are what the CI
# runner has; the Windows ones let the same card render when the pipeline runs
# from the PC. Without a fallback, OG rendering fails there — not fatally (see
# render_missing_og), but every new article would ship a 404 og:image.
FONT_SERIF = ["/usr/share/fonts/truetype/google-fonts/Lora-Variable.ttf",
              "C:/Windows/Fonts/georgia.ttf"]
FONT_SERIF_BOLD = ["/usr/share/fonts/truetype/google-fonts/Lora-Variable.ttf",
                   "C:/Windows/Fonts/georgiab.ttf"]
FONT_SANS_BOLD = ["/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                  "C:/Windows/Fonts/arialbd.ttf"]
FONT_SANS = ["/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
             "C:/Windows/Fonts/arial.ttf"]


def load_font(candidates: list[str], size: int, weight: int | None = None):
    """First candidate that exists, at `size`.

    `weight` is a variable-font axis (Lora). Static faces such as Georgia raise
    on set_variation_by_axes, so their own weight is used instead — which is why
    the bold candidates list names the bold file directly.
    """
    from PIL import ImageFont

    for path in candidates:
        if not os.path.exists(path):
            continue
        font = ImageFont.truetype(path, size)
        if weight is not None:
            try:
                font.set_variation_by_axes([weight])
            except OSError:
                pass
        return font
    return ImageFont.load_default(size)

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


def _today() -> str:
    import datetime as _dt
    return _dt.date.today().isoformat()


def git_lastmod(path: str, repo_root: str) -> str:
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cs", "--", path],
            cwd=repo_root, capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        return out or _today()
    except Exception:
        return _today()


def content_body(doc: str) -> str:
    """Page content used for lastmod. Chrome, generated blocks, and JSON-LD are ignored."""
    body = doc
    # Generated conversion chrome must not move dateModified / sitemap lastmod.
    # The optional newline is the one --fix inserts in front of a marker. Eating it
    # keeps generated chrome from changing the content hash (and sitemap lastmod).
    for name in ("mobilemenu", "mobilecta", "earlycta", "checklist"):
        body = re.sub(
            rf"\n?<!-- {name}:start -->.*?<!-- {name}:end -->",
            "",
            body,
            flags=re.S,
        )
    body = re.sub(r"<header\b[^>]*>.*?</header>", "", body, flags=re.S | re.I)
    body = re.sub(r"<footer\b[^>]*>.*?</footer>", "", body, flags=re.S | re.I)
    body = re.sub(r'<div class="mobile-menu"[^>]*>.*?</div>', "", body, flags=re.S)
    body = re.sub(r"<!-- related:start -->.*?<!-- related:end -->", "", body, flags=re.S)
    body = re.sub(r'<nav class="related-guides"[^>]*>.*?</nav>', "", body, flags=re.S)
    body = re.sub(r'<div class="related-guides"[^>]*>.*?</div>', "", body, flags=re.S)
    body = re.sub(r"<!-- sources:start -->.*?<!-- sources:end -->", "", body, flags=re.S)
    body = re.sub(r"<script\b[^>]*>.*?</script>", "", body, flags=re.S)
    return body


def content_hash(doc: str) -> str:
    return hashlib.sha256(content_body(doc).encode("utf-8")).hexdigest()[:16]


def content_dates_path(repo_root: str) -> str:
    return os.path.join(repo_root, "scripts", "content_dates.json")


def load_content_dates(repo_root: str) -> dict:
    path = content_dates_path(repo_root)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def save_content_dates(repo_root: str, data: dict) -> None:
    with open(content_dates_path(repo_root), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def schema_date_modified(doc: str) -> str:
    m = re.search(r'"dateModified"\s*:\s*"(\d{4}-\d{2}-\d{2})"', doc)
    return m.group(1) if m else ""


def upsert_date_modified(doc: str, date: str) -> str:
    if re.search(r'"dateModified"\s*:', doc):
        return re.sub(
            r'("dateModified"\s*:\s*")\d{4}-\d{2}-\d{2}(")',
            rf"\g<1>{date}\2",
            doc,
            count=1,
        )
    for pat in (
        r'("datePublished"\s*:\s*"\d{4}-\d{2}-\d{2}")',
        r'("headline"\s*:\s*"(?:\\.|[^"\\])*")',
        r'("name"\s*:\s*"(?:\\.|[^"\\])*")',
    ):
        m = re.search(pat, doc)
        if m:
            return doc[:m.end()] + f',\n      "dateModified": "{date}"' + doc[m.end():]
    return doc


def content_lastmod(site: "Site", slug: str) -> str:
    """Sitemap lastmod tracks schema dateModified, then the stored content date."""
    schema = schema_date_modified(site.docs[slug])
    if schema:
        return schema
    rec = load_content_dates(site.repo_root).get(slug) or {}
    return rec.get("date") or git_lastmod(f"public/{slug}.html", site.repo_root)


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

    mark = load_font(FONT_SERIF_BOLD, 34, 700)
    d.text((90, 80), "M", font=mark, fill=NAVY, anchor="mm")
    d.text((132, 80), "MigrateState",
           font=load_font(FONT_SANS_BOLD, 26), fill=CREAM, anchor="lm")

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
        font = load_font(FONT_SERIF, size, 600)
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
           font=load_font(FONT_SANS, 24), fill=MUTED)

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
        has_faqpage = False
        for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', doc, re.S):
            try:
                d = json.loads(block)
            except json.JSONDecodeError:
                continue
            g = d.get("@graph", [d]) if isinstance(d, dict) else d
            if any(isinstance(n, dict) and n.get("@type") == "BreadcrumbList" for n in g):
                has_breadcrumb = True
            if any(isinstance(n, dict) and n.get("@type") == "FAQPage" for n in g):
                has_faqpage = True

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

            if not has_breadcrumb and slug != "index":
                graph.append(site.breadcrumb(slug))
                changed = True

            faq = faq_entities(doc) if slug in site.hubs else []
            if faq and not has_faqpage:
                graph.append({"@type": "FAQPage", "mainEntity": faq})
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
            graph = [node, site.breadcrumb(slug)]
            faq = faq_entities(doc) if slug in site.hubs else []
            if faq:
                graph.append({"@type": "FAQPage", "mainEntity": faq})
            data = {"@context": "https://schema.org", "@graph": graph}
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
        rows.append((site.url(slug), content_lastmod(site, slug), pri))

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



# ------------------------------------------------ sources / related / ads

NON_ARTICLES = {"index", "contact", "get-started", "privacy", "terms"}

# Lead pages: no AdSense (they exist to convert to "Get matched", not to sell clicks).
# Country hubs (*-guides) are added in ads_blocked() so a new hub is covered automatically.
NO_ADS = {
    "index", "contact", "get-started", "buying-property-abroad-experts",
    "portugal-golden-visa", "greece-golden-visa", "spain-golden-visa", "italy-golden-visa",
    "malta-golden-visa", "hungary-golden-visa", "dubai-golden-visa",
    "best-golden-visa-for-americans", "ultimate-guide-golden-visa", "greece-vs-portugal-golden-visa",
    "portugal-d7-visa", "spain-non-lucrative-visa", "panama-friendly-nations-visa",
    "mexico", "buying-property-portugal", "buying-property-spain", "buying-property-greece",
    "buying-property-italy", "buying-property-dubai", "us-tax-foreign-property",
}


def ads_blocked(site: "Site", slug: str) -> bool:
    return slug in NO_ADS or slug in site.hubs

COUNTRY_TOKENS = {
    "portugal", "spain", "greece", "italy", "malta", "hungary", "cyprus", "croatia", "france",
    "germany", "turkey", "dubai", "mexico", "costa-rica", "panama", "colombia", "dominican-republic",
    "belize", "uruguay", "brazil", "japan", "thailand", "philippines", "malaysia", "bali", "indonesia",
    "ecuador", "vietnam", "montenegro", "argentina", "morocco", "albania", "paraguay",
}
COUNTRY_ALIAS = {"bali": "indonesia", "dubai": "dubai"}

TAX_WORDS = ("tax", "fbar", "fatca", "8938", "8949", "1031", "section-121", "capital-gains", "rental",
             "depreciation", "inheritance", "estate", "foreign-bank", "llc", "currency-gain",
             "schedule-e", "streamlined", "gifting")
MONEY_WORDS = ("mortgage", "transfer-money", "closing-costs", "annual-property-taxes",
               "currency-exchange", "financing")
VISA_WORDS = ("visa", "residency", "residence", "nif", "citizenship", "passport", "nomad", "pensionado",
              "mm2h", "ltr", "srrv", "beckham", "flat-tax", "non-dom", "nhr")


def is_article(site: "Site", slug: str) -> bool:
    return slug not in NON_ARTICLES and slug not in BARE_PAGES and slug not in site.hubs


def slug_countries(slug: str) -> set:
    found = set()
    for tok in COUNTRY_TOKENS:
        if re.search(rf"(^|-){re.escape(tok)}(-|$)", slug):
            found.add(COUNTRY_ALIAS.get(tok, tok))
    return found


def slug_category(slug: str) -> str:
    if slug == "mexico" or slug.startswith("buying-property-"):
        return "buy"
    if slug.startswith("selling-property-"):
        return "sell"
    if any(w in slug for w in TAX_WORDS):
        return "tax"
    if any(w in slug for w in MONEY_WORDS):
        return "money"
    if any(w in slug for w in VISA_WORDS):
        return "visa"
    return "general"


JOURNEY = {  # complementary next-step categories from the reader's point of view
    "buy": {"visa": 2, "sell": 2, "tax": 2, "money": 2, "buy": 2.5},
    "visa": {"visa": 2.5, "buy": 2, "tax": 1},
    "sell": {"tax": 2, "buy": 1},
    "tax": {"tax": 1, "sell": 1, "buy": 1},
    "money": {"buy": 2, "tax": 1},
    "general": {"buy": 2, "visa": 1, "tax": 1},
}


def inbound_counts(site: "Site") -> Counter:
    inbound: Counter = Counter()
    for slug in site.slugs:
        seen = set()
        # count editorial links only (body, hub cards, nav) — not the generated related block,
        # so recommendations stay deterministic run-to-run
        body = re.sub(r"<!-- related:start -->.*?<!-- related:end -->", "", site.docs[slug], flags=re.S)
        body = re.sub(r'<div class="related-guides".*?</div>', "", body, flags=re.S)
        for m in re.finditer(r'href="(?!https?:|mailto:|tel:|#|/)([^"#?]+)"', body):
            t = m.group(1).rstrip("/")
            if t in site.slugs and t != slug and t not in seen:
                seen.add(t)
                inbound[t] += 1
    return inbound


def load_sources(repo_root: str) -> dict:
    p = os.path.join(repo_root, "scripts", "sources.json")
    if not os.path.exists(p):
        return {"pages": {}, "rules": []}
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def sources_for(slug: str, cfg: dict, limit: int = 6) -> list:
    out, seen = [], set()
    for s in cfg.get("pages", {}).get(slug, []):
        if s["u"] not in seen:
            seen.add(s["u"]); out.append(s)
    for rule in cfg.get("rules", []):
        if re.search(rule["match"], slug):
            for s in rule["sources"]:
                if s["u"] not in seen and len(out) < limit:
                    seen.add(s["u"]); out.append(s)
    return out[:limit]


def last_verified(repo_root: str, slug: str) -> str:
    p = os.path.join(repo_root, "factcheck_state.json")
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh).get(f"{slug}.html", "")
    except Exception:
        return ""


def render_sources(slug: str, items: list, verified: str) -> str:
    lis = "\n".join(
        f'      <li><a href="{html.escape(s["u"], quote=True)}" target="_blank" rel="noopener">'
        f'{html.escape(s["n"])}</a></li>' for s in items)
    note = (f'    <p class="sources-note">Claims on this page are re-verified against primary sources '
            f'by the MigrateState fact-check rotation'
            + (f' &middot; last verified <time datetime="{verified}">{verified}</time>' if verified else '')
            + '.</p>\n')
    return ('<!-- sources:start -->\n'
            '  <section class="sources" id="sources" aria-label="Sources and official references">\n'
            '    <strong>Sources &amp; official references</strong>\n'
            f'    <ul>\n{lis}\n    </ul>\n{note}'
            '  </section>\n<!-- sources:end -->')


def fix_sources(site: "Site", log: list) -> None:
    cfg = load_sources(site.repo_root)
    for slug in sorted(site.slugs):
        if not is_article(site, slug):
            continue
        items = sources_for(slug, cfg)
        if not items:
            continue
        doc = site.docs[slug]
        block = render_sources(slug, items, last_verified(site.repo_root, slug))
        if "<!-- sources:start -->" in doc:
            new = re.sub(r"<!-- sources:start -->.*?<!-- sources:end -->", lambda m: block, doc, flags=re.S)
        else:
            anchor = (re.search(r"\n\s*</article>", doc) or re.search(r"\n\s*</main>", doc)
                      or re.search(r'\n\s*<div class="related-guides"', doc))
            if not anchor:
                continue
            new = doc[:anchor.start()] + "\n\n" + block + doc[anchor.start():]
        if new != doc:
            site.write(slug, new)
            log.append(f"sources block {'updated' if '<!-- sources:start -->' in doc else 'added'}: {slug} ({len(items)})")


def related_candidates(site: "Site", slug: str, exclude: set, n: int, min_score: float) -> list:
    """Highest-relevance articles not already chosen. No popularity or coverage bias."""
    scored = []
    for other in site.slugs:
        if other == slug or other in exclude or not is_article(site, other):
            continue
        score = relevance(site, slug, other)
        if score >= min_score:
            scored.append((score, other))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [other for _, other in scored[:n]]


def faq_entities(doc: str) -> list:
    """Question/answer pairs from the on-page FAQ, for FAQPage schema."""
    m = re.search(r'<div class="faq">(.*?)</div>', doc, re.S)
    if not m:
        return []
    out = []
    for part in re.split(r"<h3>", m.group(1))[1:]:
        qm = re.match(r"(.*?)</h3>\s*(.*)", part, re.S)
        if not qm:
            continue
        question = html.unescape(strip_tags(qm.group(1))).strip()
        answers = re.findall(r"<p>(.*?)</p>", qm.group(2), re.S)
        answer = " ".join(html.unescape(strip_tags(p)).strip() for p in answers).strip()
        if question and answer:
            out.append({
                "@type": "Question",
                "name": question,
                "acceptedAnswer": {"@type": "Answer", "text": answer},
            })
    return out


def link_label(site: "Site", slug: str) -> str:
    t = clean_title(page_title(site.docs[slug]))
    return html.escape(re.sub(r"\s*\(2026\)\s*$", "", t))


def render_related(site: "Site", slugs: list) -> str:
    links = "\n".join(
        f'    <a href="{s}">{link_label(site, s)} &rarr;</a>' for s in slugs)
    return ('<!-- related:start -->\n'
            '  <nav class="related-guides" aria-label="Related guides">\n'
            '    <strong>Related guides</strong>\n'
            f'{links}\n'
            '  </nav>\n<!-- related:end -->')


def relevance(site: "Site", a: str, b: str) -> float:
    """Editorial relatedness of b to a (no popularity terms)."""
    s = 0.0
    ca, cb = slug_countries(a), slug_countries(b)
    if ca and ca & cb:
        s += 4
    if site.hub_of.get(a) and site.hub_of.get(a) == site.hub_of.get(b):
        s += 2
    s += JOURNEY.get(slug_category(a), {}).get(slug_category(b), 0)
    if not ca and not cb:
        s += 1
    if ca and not cb and slug_category(b) in ("tax", "money", "general"):
        s += 1
    if not ca and cb and slug_category(b) == "buy":
        s += 1
    return s


def stable_related(site: "Site", slug: str, existing: list, per_page: int = 6) -> list:
    """Keep relevant links in their current order. Fill only with clearly related guides."""
    kept = [h for h in existing if relevance(site, slug, h) >= 2.0]
    if len(kept) < per_page:
        kept += related_candidates(site, slug, set(kept), per_page - len(kept), 2.5)
    if len(kept) < 3:
        rest = [h for h in existing if h not in kept]
        rest.sort(key=lambda h: (-relevance(site, slug, h), h))
        for h in rest:
            if len(kept) >= 3:
                break
            kept.append(h)
    return kept[:per_page]


def fix_related_guides(site: "Site", log: list, per_page: int = 6) -> None:
    articles = [s for s in sorted(site.slugs) if is_article(site, s)]
    for slug in articles:
        doc = site.docs[slug]
        m = re.search(r"<!-- related:start -->.*?<!-- related:end -->", doc, re.S)
        if not m:
            m = re.search(r'<div class="related-guides".*?</div>', doc, re.S)
        existing: list = []
        if m:
            for h in re.findall(r'href="([^"#?]+)"', m.group(0)):
                h = h.rstrip("/")
                if h in site.slugs and h != slug and h not in existing and is_article(site, h):
                    existing.append(h)
        picks = stable_related(site, slug, existing, per_page)
        if m and picks == existing:
            continue  # do not rewrite a block whose links did not change
        if not picks:
            continue
        block = render_related(site, picks)
        close = re.search(r"\n\s*</article>", doc) or re.search(r"\n\s*</main>", doc)
        if m and close and m.start() > close.start():
            doc = doc[:m.start()] + doc[m.end():]
            new = re.sub(
                r"\n+(\s*)(</article>|</main>)",
                "\n\n" + block.replace("\\", "\\\\") + r"\n\1\2",
                doc,
                count=1,
            )
        elif m:
            new = doc[:m.start()] + block + doc[m.end():]
        else:
            anchor = re.search(r"\n\s*</article>", doc) or re.search(r"\n\s*</main>", doc)
            if not anchor:
                continue
            new = doc[:anchor.start()] + "\n\n" + block + doc[anchor.start():]
        if new != doc:
            site.write(slug, new)
            log.append(f"related guides {'refreshed' if m else 'added'}: {slug}")


TAX_NAV_RE = re.compile(r'(<a href=")/#guides(">\s*Taxes &amp; Money\s*</a>)')


def fix_tax_nav(site: "Site", log: list) -> None:
    """The Taxes & Money item was sending readers to the homepage country grid."""
    for slug in sorted(site.slugs):
        if slug in BARE_PAGES:
            continue
        doc = site.docs[slug]
        new = TAX_NAV_RE.sub(r"\1us-tax-foreign-property\2", doc)
        if new != doc:
            site.write(slug, new)
            log.append(f"tax nav retargeted: {slug}")


def sync_content_dates(site: "Site", log: list) -> None:
    """Record a content hash per page. lastmod moves only when that hash changes.

    The first time a page is seen, the date is the JSON-LD dateModified already
    on the page, or the last git date — never today — so a first run does not
    mark the whole site as freshly updated.
    """
    store = load_content_dates(site.repo_root)
    changed = False
    for slug in sorted(site.slugs):
        if slug in BARE_PAGES:
            continue
        doc = site.docs[slug]
        digest = content_hash(doc)
        rec = store.get(slug)
        if not isinstance(rec, dict) or "hash" not in rec:
            date = schema_date_modified(doc) or git_lastmod(f"public/{slug}.html", site.repo_root)
            store[slug] = {"hash": digest, "date": date}
            changed = True
            continue
        if rec.get("hash") == digest:
            continue
        date = _today()
        store[slug] = {"hash": digest, "date": date}
        changed = True
        new = upsert_date_modified(doc, date)
        if new != doc:
            site.write(slug, new)
            log.append(f"dateModified set to {date}: {slug}")
    if changed:
        save_content_dates(site.repo_root, store)
        log.append(f"content dates recorded: {len(store)} pages")


# Do not eat surrounding whitespace: the content hash ignores the script tag itself
# but counts the bytes around it, and lastmod should not move just because an ad tag left.
ADSENSE_RE = re.compile(r'<script async src="https://pagead2\.googlesyndication\.com/pagead/js/adsbygoogle\.js[^"]*"[^>]*></script>')


def fix_adsense(site: "Site", log: list) -> None:
    for slug in sorted(site.slugs):
        if not ads_blocked(site, slug):
            continue
        doc = site.docs[slug]
        new = ADSENSE_RE.sub("", doc)
        if new != doc:
            site.write(slug, new)
            log.append(f"AdSense removed from lead page: {slug}")


# --------------------------------------------------------------- conversion
# Marker-wrapped blocks. Idempotent: a second --fix rewrites the same bytes.


ML_SUBSCRIBE_URL = "https://assets.mailerlite.com/jsonp/2428927/forms/190085025285276727/subscribe"
ML_MAILTO = "mailto:barakn770@gmail.com?subject=Overseas%20property%20checklist"

# Reads the JSON {success:...} body. If the browser cannot read it (CORS), falls
# back to the callback= JSONP pattern MailerLite's embed script uses.
SUBSCRIBE_JS = """function msSubscribe(e){
  e.preventDefault();
  var f=e.target, b=f.querySelector('button'), box=f.parentNode;
  var ok=box.querySelector('.ms-success'), err=box.querySelector('.ms-error');
  var data=new FormData(f);
  data.append('ml-submit','1');
  data.append('anticsrf','true');
  var endpoint='%s';
  b.textContent='Sending…';
  b.disabled=true;
  if(err) err.hidden=true;
  function succeed(){ if(ok) ok.hidden=false; f.hidden=true; }
  function fail(){
    b.textContent='Send me the checklist';
    b.disabled=false;
    f.hidden=false;
    if(err) err.hidden=false;
  }
  function readSuccess(payload){
    if(payload && payload.success===true) succeed();
    else fail();
  }
  function jsonp(){
    var cb='msCb'+Date.now()+Math.floor(Math.random()*10000);
    var params=new URLSearchParams(data).toString();
    var done=false, s=document.createElement('script');
    function cleanup(){
      clearTimeout(timer);
      try{ delete window[cb]; }catch(ex){ window[cb]=undefined; }
      if(s.parentNode) s.parentNode.removeChild(s);
    }
    var timer=setTimeout(function(){ if(done) return; done=true; cleanup(); fail(); }, 8000);
    window[cb]=function(payload){ if(done) return; done=true; cleanup(); readSuccess(payload); };
    s.onerror=function(){ if(done) return; done=true; cleanup(); fail(); };
    s.src=endpoint+'?callback='+encodeURIComponent(cb)+'&'+params;
    document.head.appendChild(s);
  }
  fetch(endpoint,{method:'POST',body:data,headers:{'Accept':'application/json'}})
    .then(function(r){ return r.text().then(function(t){ try{ return JSON.parse(t); }catch(ex){ return null; } }); })
    .then(function(j){ if(j) readSuccess(j); else jsonp(); })
    .catch(jsonp);
  return false;
}""" % ML_SUBSCRIBE_URL


def subscribe_script() -> str:
    return "<script>\n" + SUBSCRIBE_JS + "\n</script>"


EARLY_CTA = """<!-- earlycta:start -->
<div class="cta-box">
  <div><strong>Want this checked for your own plans?</strong><p>Get matched with a vetted local expert — free, and no obligation.</p></div>
  <a class="btn" href="get-started">Get matched →</a>
</div>
<!-- earlycta:end -->"""


def checklist_block() -> str:
    return f"""<!-- checklist:start -->
<section class="checklist-signup" aria-label="Free checklist">
  <div class="newsletter">
    <span class="eyebrow" style="color:var(--gold)">Free checklist</span>
    <h2>The Americans' Overseas Property Checklist</h2>
    <p>The legal, tax and financial checks to run before you buy abroad, delivered to your email. Plus new country guides as they drop.</p>
    <form class="form-row" onsubmit="return msSubscribe(event)">
      <input type="email" name="fields[email]" placeholder="Your email address" autocomplete="email" required>
      <button class="btn" type="submit">Send me the checklist</button>
    </form>
    <p class="ms-success" hidden>You're in. Check your inbox to confirm — your checklist is on the way.</p>
    <p class="ms-error" hidden>We couldn't sign you up just now. Email <a href="{ML_MAILTO}">barakn770@gmail.com</a> and we'll send the checklist.</p>
    <p class="checklist-note">No spam. Unsubscribe anytime. We never share your email.</p>
  </div>
</section>
{subscribe_script()}
<!-- checklist:end -->"""


MOBILE_STICKY = """<!-- mobilecta:start -->
<a class="mobile-cta-bar" href="get-started" style="display:none">Get matched with a vetted local expert</a>
<!-- mobilecta:end -->"""


def wants_mobile_sticky(site: "Site", slug: str) -> bool:
    return is_article(site, slug) or slug in site.hubs


def _nav_anchors(doc: str) -> list[tuple[str, str]]:
    m = re.search(r'<nav class="nav-links">(.*?)</nav>', doc, re.S)
    if not m:
        return []
    out = []
    for attrs, text in re.findall(r"<a\b([^>]*)>(.*?)</a>", m.group(1), re.S):
        href_m = re.search(r'href="([^"]*)"', attrs)
        href = href_m.group(1) if href_m else "get-started"
        label = re.sub(r"\s+", " ", text).strip()
        out.append((href, label))
    return out


def render_mobile_menu(doc: str) -> str:
    """Homepage slide-in menu, filled from this page's own nav links."""
    lines = []
    seen_matched = False
    for href, label in _nav_anchors(doc):
        plain = strip_tags(label)
        if plain == "Get Matched":
            seen_matched = True
            lines.append(f'  <a class="btn" href="{href}">Get Matched</a>')
        else:
            lines.append(f'  <a href="{href}">{label}</a>')
    if not seen_matched:
        lines.append('  <a class="btn" href="get-started">Get Matched</a>')
    links = "\n".join(lines)
    return f"""<!-- mobilemenu:start -->
<div class="mobile-menu" id="mobileMenu">
  <button class="menu-close" id="menuClose" type="button" aria-label="Close menu">✕</button>
{links}
</div>
<script>
  const toggle = document.querySelector('.menu-toggle');
  const menu = document.getElementById('mobileMenu');
  const close = document.getElementById('menuClose');
  if(toggle && menu && close){{
    toggle.addEventListener('click', () => menu.classList.add('open'));
    close.addEventListener('click', () => menu.classList.remove('open'));
    menu.querySelectorAll('a').forEach(a => a.addEventListener('click', () => menu.classList.remove('open')));
  }}
</script>
<!-- mobilemenu:end -->"""


def _strip_nav_scripts(doc: str) -> str:
    """Drop legacy hamburger handlers (inline nav toggle, or an older mobile menu)."""
    def repl(m: re.Match) -> str:
        body = m.group(1)
        if any(tok in body for tok in ("mobileMenu", "menu-toggle", "menuClose", "closeMenu")):
            return ""
        return m.group(0)
    return re.sub(r"<script\b[^>]*>(.*?)</script>", repl, doc, flags=re.S)


def _strip_marker(doc: str, name: str) -> str:
    """Remove one generated block and the single newline --fix put in front of it."""
    return re.sub(
        rf"\n?<!-- {name}:start -->.*?<!-- {name}:end -->",
        "",
        doc,
        count=1,
        flags=re.S,
    )


def _insert_after_footer(doc: str, block: str) -> str:
    # One new newline, which content_body strips with the marker. Existing
    # whitespace after the footer stays put so the content hash does not move.
    return re.sub(
        r"</footer>",
        lambda m: m.group(0) + "\n" + block,
        doc,
        count=1,
        flags=re.I,
    )


def fix_mobile_nav(site: "Site", log: list) -> None:
    header_cta = (
        '<a class="btn nav-cta nav-cta-mobile" href="get-started" style="display:none">Get Matched</a>\n'
    )
    for slug in sorted(site.slugs):
        doc = site.docs[slug]
        block = render_mobile_menu(doc)
        new = _strip_marker(doc, "mobilemenu")
        new = re.sub(r'<div class="mobile-menu" id="mobileMenu">.*?</div>', "", new, count=1, flags=re.S)
        new = _strip_nav_scripts(new)
        if "nav-cta-mobile" not in new:
            new2, n = re.subn(
                r"([ \t]*)<button class=\"menu-toggle\"",
                lambda m: f"{m.group(1)}{header_cta}{m.group(1)}<button class=\"menu-toggle\"",
                new,
                count=1,
            )
            if n:
                new = new2
        if "</footer>" in new.lower():
            new = _insert_after_footer(new, block)
        else:
            new = re.sub(r"</body>", "\n" + block + "</body>", new, count=1, flags=re.I)
        if new != doc:
            site.write(slug, new)
            log.append(f"mobile nav wired: {slug}")


def fix_mobile_sticky(site: "Site", log: list) -> None:
    for slug in sorted(site.slugs):
        doc = site.docs[slug]
        new = _strip_marker(doc, "mobilecta")
        if wants_mobile_sticky(site, slug):
            new = re.sub(r"</body>", "\n" + MOBILE_STICKY + "</body>", new, count=1, flags=re.I)
        if new != doc:
            site.write(slug, new)
            log.append(f"mobile sticky CTA {'added' if wants_mobile_sticky(site, slug) else 'removed'}: {slug}")


def _early_anchor(doc: str):
    """End of the intro block a reader sees first: answer box, else Key Facts, else the top disclaimer."""
    m = re.search(r'<div class="answer-box">.*?</div>', doc, re.S)
    if m:
        return m
    m = re.search(r"<h[23][^>]*>[^<]*[Kk]ey [Ff]acts[^<]*</h[23]>", doc)
    if m:
        return m
    m = re.search(r'<div class="disclaim-box">.*?</div>', doc, re.S)
    if m:
        return m
    return re.search(r'<div class="disclaimer-box">.*?</div>', doc, re.S)


def fix_early_cta(site: "Site", log: list) -> None:
    for slug in sorted(site.slugs):
        if not is_article(site, slug):
            continue
        doc = site.docs[slug]
        if "<!-- earlycta:start -->" in doc:
            new = re.sub(r"<!-- earlycta:start -->.*?<!-- earlycta:end -->", EARLY_CTA, doc, count=1, flags=re.S)
        else:
            anchor = _early_anchor(doc)
            if not anchor:
                log.append(f"early CTA skipped (no answer box): {slug}")
                continue
            new = doc[:anchor.end()] + "\n" + EARLY_CTA + doc[anchor.end():]
        if new != doc:
            site.write(slug, new)
            log.append(f"early CTA added: {slug}")


def _article_close(doc: str):
    return re.search(r"\n[ \t]*</article>", doc) or re.search(r"\n[ \t]*</main>", doc)


def fix_checklist(site: "Site", log: list) -> None:
    block = checklist_block()
    for slug in sorted(site.slugs):
        if not is_article(site, slug):
            continue
        doc = site.docs[slug]
        if "<!-- checklist:start -->" in doc:
            new = re.sub(r"<!-- checklist:start -->.*?<!-- checklist:end -->", lambda m: block, doc, count=1, flags=re.S)
        else:
            close = _article_close(doc)
            if not close:
                log.append(f"checklist skipped (no article close): {slug}")
                continue
            new = doc[:close.start()] + "\n" + block + doc[close.start():]
        if new != doc:
            site.write(slug, new)
            log.append(f"checklist signup added: {slug}")


def fix_home_subscribe(site: "Site", log: list) -> None:
    """Homepage newsletter uses the same reader as the article checklist block."""
    if "index" not in site.docs:
        return
    doc = site.docs["index"]
    new = doc.replace(
        "A 2-page PDF: the 18 legal, tax and financial checks to run before you buy abroad. Plus new country guides as they drop.",
        "The legal, tax and financial checks to run before you buy abroad, delivered to your email. Plus new country guides as they drop.",
    )
    new = re.sub(
        r"<script>\s*function msSubscribe\(e\)\{.*?</script>",
        lambda m: subscribe_script(),
        new,
        count=1,
        flags=re.S,
    )
    if 'class="ms-error"' not in new:
        new = new.replace(
            '<p class="ms-success" style="display:none;color:#fff;font-size:18px;margin:6px 0 0">✓ You\'re in! Check your inbox to confirm — your checklist is on the way.</p>',
            '<p class="ms-success" hidden style="color:#fff;font-size:18px;margin:6px 0 0">You\'re in. Check your inbox to confirm — your checklist is on the way.</p>\n'
            f'      <p class="ms-error" hidden style="color:#ffd0c8;font-size:15.5px;margin:8px 0 0">We couldn\'t sign you up just now. Email <a href="{ML_MAILTO}" style="color:#fff;text-decoration:underline">barakn770@gmail.com</a> and we\'ll send the checklist.</p>',
            1,
        )
    # The shared reader toggles the hidden attribute, so an inline display:none would stick.
    new = new.replace(
        '<p class="ms-success" style="display:none;color:#fff;font-size:18px;margin:6px 0 0">',
        '<p class="ms-success" hidden style="color:#fff;font-size:18px;margin:6px 0 0">',
        1,
    )
    if new != doc:
        site.write("index", new)
        log.append("homepage checklist subscribe reads the MailerLite response")


def country_option_labels(site: "Site") -> list[str]:
    labels = []
    for hub in site.hubs:
        if hub == "general-guides":
            continue
        labels.append(html.unescape(hub_label(hub)))
    return sorted(set(labels), key=lambda s: s.casefold())


def fix_get_started(site: "Site", log: list) -> None:
    """Country dropdown follows the hub pages. Drop the dead '#' partner links."""
    if "get-started" not in site.docs:
        return
    doc = site.docs["get-started"]
    lines = ['          <option value="">Select…</option>']
    for label in country_option_labels(site):
        lines.append(f"          <option>{html.escape(label)}</option>")
    lines.append("          <option>Still deciding</option>")
    lines.append("          <option>Other</option>")
    inner = "\n" + "\n".join(lines) + "\n        "
    new, n = re.subn(
        r'(<select id="lf-country" name="fields\[country\]" required>)(.*?)(</select>)',
        lambda m: m.group(1) + inner + m.group(3),
        doc,
        count=1,
        flags=re.S,
    )
    if n:
        doc = new
    doc2, n = re.subn(
        r'\n[ \t]*<!-- AFFILIATE PARTNERS.*?<div class="partners">.*?</div>\s*(?=</article>)',
        "\n",
        doc,
        count=1,
        flags=re.S,
    )
    if n:
        doc = doc2
    doc = doc.replace(
        "  .partners{background:var(--cream);border-radius:16px;padding:26px;margin:34px 0 0}\n"
        "  .partners h2{font-size:22px;margin:0 0 6px}\n"
        "  .partners p{color:var(--muted);margin:0 0 18px;font-size:15.5px}\n"
        "  .partner-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}\n"
        "  .partner{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px 18px}\n"
        "  .partner h3{font-size:16px;margin:0 0 4px}\n"
        "  .partner p{font-size:14px;color:var(--muted);margin:0 0 10px}\n"
        "  .partner a{font-weight:600;color:var(--gold-dark);font-size:14px}\n"
        "  @media(max-width:680px){.lead-grid,.partner-grid{grid-template-columns:1fr}}\n",
        "  @media(max-width:680px){.lead-grid{grid-template-columns:1fr}}\n",
    )
    if doc != site.docs["get-started"]:
        site.write("get-started", doc)
        log.append(f"get-started countries: {len(country_option_labels(site))} hubs, dead partner links removed")


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

        # audit blocks (Sept 2026 SEO audit)
        if is_article(site, slug):
            if "<!-- sources:start -->" not in doc:
                problems["missing_sources_block"].append(slug)
            if "<!-- related:start -->" not in doc:
                problems["missing_related_guides"].append(slug)
        if ads_blocked(site, slug) and "adsbygoogle.js" in doc:
            problems["adsense_on_lead_page"].append(slug)
        if 'id="mobileMenu"' not in doc:
            problems["missing_mobile_menu"].append(slug)
        if "nav-cta-mobile" not in doc:
            problems["missing_mobile_header_cta"].append(slug)
        if wants_mobile_sticky(site, slug):
            if "<!-- mobilecta:start -->" not in doc:
                problems["missing_mobile_sticky_cta"].append(slug)
        elif "<!-- mobilecta:start -->" in doc:
            problems["unexpected_mobile_sticky_cta"].append(slug)
        if is_article(site, slug):
            if "<!-- earlycta:start -->" not in doc:
                problems["missing_early_cta"].append(slug)
            if "<!-- checklist:start -->" not in doc:
                problems["missing_checklist"].append(slug)
        if "no-cors" in doc:
            problems["subscribe_ignores_response"].append(slug)
        if desc_m and len(html.unescape(desc_m.group(1))) < 110:
            problems["description_too_short"].append(f"{slug} ({len(html.unescape(desc_m.group(1)))})")

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

        if blocks and slug not in BARE_PAGES and slug != "index" and not any(
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
        fix_adsense(site, log)
        fix_tax_nav(site, log)
        fix_mobile_nav(site, log)
        fix_mobile_sticky(site, log)
        fix_early_cta(site, log)
        fix_get_started(site, log)
        fix_home_subscribe(site, log)
        fix_sources(site, log)
        sync_content_dates(site, log)
        fix_related_guides(site, log)
        fix_checklist(site, log)
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
