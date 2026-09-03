#!/usr/bin/env python3
"""
MigrateState — daily fact-check engine.

Checks a rotating batch of PAGES_PER_RUN pages (oldest-checked first) against
OFFICIAL sources only (government portals, official gazettes, IRS/tax
authorities). Applies safe text corrections with a source URL, refreshes the
"Updated <Month Year>" stamp on edited pages, logs every change to
CORRECTIONS.md, and writes a summary for the daily PR body.

State:   factcheck_state.json   {slug: "YYYY-MM-DD last checked"}
Output:  edited public/*.html, CORRECTIONS.md, .pipeline_summary.md (appended)

A claim that looks wrong but cannot be confirmed by an official source is
NEVER edited — it is flagged in the PR body for human review instead.
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
STATE_FILE = os.path.join(ROOT, "factcheck_state.json")
CORRECTIONS = os.path.join(ROOT, "CORRECTIONS.md")
SUMMARY = os.path.join(ROOT, ".pipeline_summary.md")

PAGES_PER_RUN = 5
MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
SKIP = {"privacy.html", "terms.html", "contact.html", "get-started.html",
        "index.html", "ads.txt", "robots.txt", "style.css", "sitemap.xml"}

SYSTEM = """You are the MigrateState Fact-Check Engine, reviewing pages of
migratestate.com — a legal/tax-focused guide for Americans buying property and
obtaining residency abroad. Today is {today}.

Your job: verify every dated, numeric or legal claim on the page (investment
thresholds, visa rules, tax rates, filing thresholds, law citations, program
status) against OFFICIAL sources ONLY: government portals, official gazettes
(Diário da República, BOE, Gazzetta Ufficiale, FEK, DOF...), IRS/FinCEN, and
official immigration authorities. News sites, law-firm blogs and aggregator
sites are NOT acceptable sources for making a correction.

Rules:
- Only report a correction when you CONFIRMED the current fact on an official
  source and the page contradicts it.
- "find" must be an EXACT substring of the page HTML, long enough to be unique.
  "replace" must preserve the surrounding HTML structure and the site's tone.
- If something seems outdated but you cannot confirm it officially, put it in
  "flags" instead — never guess.
- If the page is accurate, return empty lists. Most pages should pass.

Return ONLY a JSON object, no markdown fences:
{{"corrections": [{{"find": "...", "replace": "...", "source": "official URL",
   "reason": "one line"}}],
  "flags": [{{"claim": "...", "issue": "...", "where_to_verify": "..."}}]}}"""


def month_year() -> str:
    return datetime.date.today().strftime("%B %Y")


def pick_pages() -> list[str]:
    state = {}
    if os.path.exists(STATE_FILE):
        state = json.load(open(STATE_FILE))
    pages = [f for f in sorted(os.listdir(PUB))
             if f.endswith(".html") and f not in SKIP]
    pages.sort(key=lambda f: state.get(f, "1970-01-01"))
    return pages[:PAGES_PER_RUN]


def check_page(client: anthropic.Anthropic, fname: str) -> dict:
    html_src = open(os.path.join(PUB, fname), encoding="utf-8").read()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM.format(today=datetime.date.today().isoformat()),
        tools=[{"type": "web_search_20250305", "name": "web_search",
                "max_uses": 8}],
        messages=[{"role": "user", "content":
                   f"Page: https://migratestate.com/{fname[:-5]}\n\n"
                   f"Full HTML follows. Fact-check it.\n\n{html_src[:120000]}"}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1:
        return {"corrections": [], "flags": []}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {"corrections": [], "flags": [
            {"claim": fname, "issue": "fact-check response unparseable",
             "where_to_verify": "manual re-run"}]}


def apply(fname: str, result: dict) -> list[str]:
    path = os.path.join(PUB, fname)
    src = open(path, encoding="utf-8").read()
    applied = []
    for c in result.get("corrections", []):
        find, replace = c.get("find", ""), c.get("replace", "")
        if not find or not c.get("source"):
            continue
        if src.count(find) == 1 and find != replace:
            src = src.replace(find, replace)
            applied.append(c)
    if applied:
        src = re.sub(r"Updated\s+[A-Z][a-z]+\s+\d{4}",
                     f"Updated {month_year()}", src)
        open(path, "w", encoding="utf-8").write(src)
    return applied


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY missing")
    client = anthropic.Anthropic()
    today = datetime.date.today().isoformat()
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    all_applied, all_flags, checked = [], [], []

    for fname in pick_pages():
        print(f"fact-check: {fname}")
        try:
            result = check_page(client, fname)
        except Exception as exc:  # noqa: BLE001 — never kill the run
            print(f"  ERROR {exc}")
            continue
        applied = apply(fname, result)
        for c in applied:
            all_applied.append((fname, c))
            print(f"  fixed: {c['reason']}")
        for f in result.get("flags", []):
            all_flags.append((fname, f))
        checked.append(fname)
        state[fname] = today

    json.dump(state, open(STATE_FILE, "w"), indent=2, sort_keys=True)

    if all_applied:
        with open(CORRECTIONS, "a", encoding="utf-8") as fh:
            fh.write(f"\n## {today}\n")
            for fname, c in all_applied:
                fh.write(f"- **{fname}**: {c['reason']} "
                         f"([source]({c['source']}))\n")

    with open(SUMMARY, "a", encoding="utf-8") as fh:
        fh.write(f"### Fact-check ({today})\n")
        fh.write(f"Checked: {', '.join(checked) or 'none'}\n\n")
        if all_applied:
            fh.write("**Corrections applied (official source confirmed):**\n")
            for fname, c in all_applied:
                fh.write(f"- `{fname}` — {c['reason']} — {c['source']}\n")
        else:
            fh.write("No corrections needed.\n")
        if all_flags:
            fh.write("\n**⚠️ Flagged for human review (not edited):**\n")
            for fname, f in all_flags:
                fh.write(f"- `{fname}` — {f.get('claim','')}: "
                         f"{f.get('issue','')} "
                         f"(verify: {f.get('where_to_verify','')})\n")
        fh.write("\n")


if __name__ == "__main__":
    main()
