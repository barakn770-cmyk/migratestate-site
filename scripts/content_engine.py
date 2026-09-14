#!/usr/bin/env python3
"""
MigrateState — daily content engine.

Takes the first pending topic from queue.json, researches it with web search
(official sources), and writes a new article that mirrors the site's existing
template exactly (head/meta, JSON-LD Article + FAQPage + BreadcrumbList,
Key Facts box, internal links, disclaimer, nav/footer). Also updates the
relevant hub page with a guide card so seo_health can rebuild the hub's
CollectionPage ItemList. Updates progress.json.

queue.json item: {"slug", "title", "hub", "template", "keywords", "angle"}
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys

import anthropic

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUB = os.path.join(ROOT, "public")
QUEUE = os.path.join(ROOT, "queue.json")
PROGRESS = os.path.join(ROOT, "progress.json")
SUMMARY = os.path.join(ROOT, ".pipeline_summary.md")
SOURCES = os.path.join(ROOT, "scripts", "sources.json")

MODEL = os.environ.get("MODEL", "claude-sonnet-5")
DEFAULT_TEMPLATE = "buying-property-portugal.html"

SYSTEM = """You are the MigrateState Content Engine writing for
migratestate.com — legal & tax-first guides for Americans buying property and
obtaining residency abroad. Today is {today}.

You are given: (1) a REFERENCE ARTICLE that defines the exact template — copy
its head structure, meta/OG/twitter tags, JSON-LD types (Article, FAQPage,
BreadcrumbList), CSS classes, Key Facts box, section layout, internal-link
style, disclaimer and nav/footer verbatim in structure; (2) the HUB PAGE the
new article belongs to; (3) the new topic.

Requirements for the new article:
- Research with web search first. Every legal/tax/threshold claim must come
  from an OFFICIAL source (government portal, official gazette, IRS/FinCEN,
  official immigration authority) and be cited inline the same way the
  reference article cites sources. No invented numbers. If official sourcing
  is impossible for a sub-topic, write around it honestly.
- 2,000+ words of substantive, expert-level content. Practical, precise,
  US-reader oriented (US tax interplay, FBAR/FATCA where relevant).
- canonical + og:url = https://migratestate.com/{slug}
- og:image = https://migratestate.com/og/{slug}.png (the PNG is generated
  later by seo_health — just reference it).
- Date stamp "Updated {month_year}".
- 4-8 FAQ items mirrored in the FAQPage JSON-LD.
- 3-6 internal links to existing site pages (choose from the provided list).
- Same disclaimer block as the reference.
- At least TWO data tables built from figures you actually sourced.
- At least ONE inline-SVG figure, placed right after the section it
  illustrates — a cost breakdown from the article's own cost table, or a step
  timeline from its step-by-step section:
  <figure class="fig"><svg viewBox="0 0 720 300" role="img"
  aria-labelledby="..."><title>...</title>...</svg><figcaption>...</figcaption>
  </figure>
  Palette: navy #0f2a43, gold #c8a24a, cream #f7f4ee, muted #96a8ba. System
  font, no external assets, and no number that is not already on the page.
- If the topic asks for a calculator, write it as inline JS inside the article:
  no external scripts, no network calls, defaults labelled as estimates.
- Do NOT write a "Sources" block or a "Related guides" block. Both are
  generated later from the JSON you return.

For the hub page: insert one guide card for the new article, identical in
markup to the existing cards, in a sensible position. Change nothing else.

Return ONLY a JSON object (no markdown fences). "sources" holds the 3-6
OFFICIAL sources you actually relied on, most important first:
{{"article_html": "<!doctype html>...", "hub_html": "<!doctype html>...",
  "sources": [{{"n": "<Authority — document>", "u": "<official URL>"}}]}}"""


def month_year() -> str:
    return datetime.date.today().strftime("%B %Y")


def register_sources(slug: str, items: list[dict], max_per_page: int = 6) -> int:
    """Add official sources for a slug to scripts/sources.json.

    seo_health renders that file as the page's "Sources & official references"
    block, so an article without an entry here ships without its E-E-A-T block.
    The file's existing indentation is preserved so the diff stays small.
    """
    if not items:
        return 0
    original = open(SOURCES, encoding="utf-8").read()
    data = json.loads(original)
    pages = data.setdefault("pages", {})
    lst = pages.setdefault(slug, [])
    added = 0
    for it in items:
        url, name = (it.get("u") or "").strip(), (it.get("n") or "").strip()
        if not url or not name or len(lst) >= max_per_page:
            continue
        if any(x.get("u") == url for x in lst):
            continue
        lst.append({"n": name, "u": url})
        added += 1
    body = original.rstrip("\n")
    for indent in (1, 2, 4, None):
        for ascii_only in (False, True):
            if json.dumps(json.loads(original), indent=indent,
                          ensure_ascii=ascii_only) == body:
                open(SOURCES, "w", encoding="utf-8").write(
                    json.dumps(data, indent=indent, ensure_ascii=ascii_only)
                    + (original[len(body):] or "\n"))
                return added
    json.dump(data, open(SOURCES, "w", encoding="utf-8"), indent=1,
              ensure_ascii=False)
    return added


def quality_gate(article: str) -> tuple[list[str], list[str]]:
    """(blockers, warnings) for a generated article.

    Blockers are structural: a page with them would be broken or unindexable,
    so nothing is written. Warnings are editorial targets (tables, figure) —
    they are reported in the run summary but never cost the site a whole day's
    article, which is what a hard failure on a missing figure would do.
    """
    blockers, warnings = [], []
    if len(article) < 8000:
        blockers.append(f"too short ({len(article)} bytes, need 8,000+)")
    if '<script type="application/ld+json"' not in article:
        blockers.append("no JSON-LD block")
    if 'rel="canonical"' not in article:
        blockers.append("no canonical tag")
    if "disclaim" not in article.lower():
        blockers.append("no disclaimer block")
    if article.count("<div") != article.count("</div>"):
        blockers.append(f"unbalanced divs ({article.count('<div')} open, "
                        f"{article.count('</div>')} closed)")
    for block in re.findall(
            r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
            article, re.S):
        try:
            json.loads(block.strip())
        except json.JSONDecodeError as exc:
            blockers.append(f"JSON-LD does not parse: {exc}")
            break
    if article.count("<table") < 2:
        warnings.append(f"only {article.count('<table')} data table(s), "
                        f"the brief asks for 2")
    if "<figure" not in article:
        warnings.append("no inline-SVG figure")
    return blockers, warnings


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY missing")
    queue = json.load(open(QUEUE))
    progress = json.load(open(PROGRESS)) if os.path.exists(PROGRESS) else {
        "completed": [], "lastRun": "", "lastSlug": ""}
    done = set(progress["completed"])
    existing = {f[:-5] for f in os.listdir(PUB) if f.endswith(".html")}

    item = next((q for q in queue
                 if q["slug"] not in done and q["slug"] not in existing), None)
    if item is None:
        print("Queue empty — nothing to generate.")
        with open(SUMMARY, "a", encoding="utf-8") as fh:
            fh.write("### New article\n⚠️ Topic queue is EMPTY — refill "
                     "queue.json.\n\n")
        return

    slug, hub = item["slug"], item.get("hub", "general-guides.html")
    template = item.get("template", DEFAULT_TEMPLATE)
    if not os.path.exists(os.path.join(PUB, template)):
        template = DEFAULT_TEMPLATE
    if not os.path.exists(os.path.join(PUB, hub)):
        hub = "general-guides.html"
    ref = open(os.path.join(PUB, template), encoding="utf-8").read()
    hub_src = open(os.path.join(PUB, hub), encoding="utf-8").read()
    pages_list = "\n".join(sorted(f"/{s}" for s in existing))

    client = anthropic.Anthropic()
    print(f"generating: {slug} (template={template}, hub={hub})")
    # Streaming is REQUIRED here: a 45k-token completion can run past the SDK's
    # 10-minute non-streaming ceiling, which is what killed the 2026-09-09 run
    # ("ValueError: Streaming is required for operations that may take longer
    # than 10 minutes").
    chunks: list[str] = []
    with client.messages.stream(
        model=MODEL,
        max_tokens=45000,
        system=SYSTEM.format(today=datetime.date.today().isoformat(),
                             slug=slug, month_year=month_year()),
        tools=[{"type": "web_search_20260209", "name": "web_search",
                "max_uses": 10}],
        messages=[{"role": "user", "content":
                   f"NEW TOPIC\nslug: {slug}\ntitle: {item['title']}\n"
                   f"target keywords: {item.get('keywords','')}\n"
                   f"angle: {item.get('angle','')}\n\n"
                   f"EXISTING PAGES (for internal links):\n{pages_list}\n\n"
                   f"REFERENCE ARTICLE ({template}):\n{ref}\n\n"
                   f"HUB PAGE ({hub}):\n{hub_src}"}],
    ) as stream:
        for piece in stream.text_stream:
            chunks.append(piece)
    text = "".join(chunks)
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start:end + 1])

    article, hub_html = data["article_html"], data["hub_html"]
    blockers, warnings = quality_gate(article)
    if blockers:
        sys.exit("Quality gate failed — nothing written: "
                 + "; ".join(blockers))
    for w in warnings:
        print(f"warning: {w}")

    open(os.path.join(PUB, f"{slug}.html"), "w", encoding="utf-8").write(article)
    n_sources = register_sources(slug, data.get("sources", []))
    print(f"sources registered: {n_sources}")
    if len(hub_html) > len(hub_src) * 0.9:
        open(os.path.join(PUB, hub), "w", encoding="utf-8").write(hub_html)

    progress["completed"].append(slug)
    progress["lastRun"] = datetime.date.today().isoformat()
    progress["lastSlug"] = slug
    json.dump(progress, open(PROGRESS, "w"), indent=2)

    with open(SUMMARY, "a", encoding="utf-8") as fh:
        fh.write(f"### New article\n- **/{slug}** — {item['title']} "
                 f"(hub: {hub}, {len(article)//1000}KB, "
                 f"{n_sources} sources)\n")
        for w in warnings:
            fh.write(f"  - ⚠️ {w}\n")
        fh.write("\n")
    print(f"done: public/{slug}.html")


if __name__ == "__main__":
    main()
