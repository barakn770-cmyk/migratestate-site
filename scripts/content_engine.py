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

MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
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

For the hub page: insert one guide card for the new article, identical in
markup to the existing cards, in a sensible position. Change nothing else.

Return ONLY a JSON object (no markdown fences):
{{"article_html": "<!doctype html>...", "hub_html": "<!doctype html>..."}}"""


def month_year() -> str:
    return datetime.date.today().strftime("%B %Y")


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
    resp = client.messages.create(
        model=MODEL,
        max_tokens=45000,
        system=SYSTEM.format(today=datetime.date.today().isoformat(),
                             slug=slug, month_year=month_year()),
        tools=[{"type": "web_search_20250305", "name": "web_search",
                "max_uses": 10}],
        messages=[{"role": "user", "content":
                   f"NEW TOPIC\nslug: {slug}\ntitle: {item['title']}\n"
                   f"target keywords: {item.get('keywords','')}\n"
                   f"angle: {item.get('angle','')}\n\n"
                   f"EXISTING PAGES (for internal links):\n{pages_list}\n\n"
                   f"REFERENCE ARTICLE ({template}):\n{ref}\n\n"
                   f"HUB PAGE ({hub}):\n{hub_src}"}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start:end + 1])

    article, hub_html = data["article_html"], data["hub_html"]
    if len(article) < 8000:
        sys.exit(f"Generated article suspiciously short "
                 f"({len(article)} chars) — aborting, nothing written.")

    open(os.path.join(PUB, f"{slug}.html"), "w", encoding="utf-8").write(article)
    if len(hub_html) > len(hub_src) * 0.9:
        open(os.path.join(PUB, hub), "w", encoding="utf-8").write(hub_html)

    progress["completed"].append(slug)
    progress["lastRun"] = datetime.date.today().isoformat()
    progress["lastSlug"] = slug
    json.dump(progress, open(PROGRESS, "w"), indent=2)

    with open(SUMMARY, "a", encoding="utf-8") as fh:
        fh.write(f"### New article\n- **/{slug}** — {item['title']} "
                 f"(hub: {hub}, {len(article)//1000}KB)\n\n")
    print(f"done: public/{slug}.html")


if __name__ == "__main__":
    main()
