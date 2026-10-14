#!/usr/bin/env python3
"""Quality gate for a page the pipeline just wrote or rewrote.

Why this exists: PIPELINE.md requires a hard gate before a new article or a
flagship expansion is committed (size, JSON-LD parses, canonical tag,
disclaimer, balanced <div>). The daily run can only execute `python scripts/*`,
so the check has to live here rather than in an ad-hoc one-liner.

Usage:
    python scripts/article_gate.py public/some-page.html
    python scripts/article_gate.py public/a.html public/b.html --min-bytes 8000

Exit code 0 = every file passed, 1 = at least one failed.
"""

import argparse
import json
import os
import re
import sys

LD_JSON = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
OPEN_DIV = re.compile(r"<div\b", re.IGNORECASE)
CLOSE_DIV = re.compile(r"</div\s*>", re.IGNORECASE)
OPEN_SVG = re.compile(r"<svg\b", re.IGNORECASE)
CLOSE_SVG = re.compile(r"</svg\s*>", re.IGNORECASE)
FIGURE = re.compile(r"<figure\b", re.IGNORECASE)
TABLE = re.compile(r"<table\b", re.IGNORECASE)
WORDS = re.compile(r"[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ'’-]*")


def strip_markup(html: str) -> str:
    html = re.sub(r"<(script|style|svg)\b.*?</\1\s*>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)
    return re.sub(r"<[^>]+>", " ", html)


def check(path: str, min_bytes: int) -> list:
    failures = []
    if not os.path.isfile(path):
        return [f"file not found: {path}"]

    raw = open(path, "rb").read()
    if len(raw) < min_bytes:
        failures.append(f"{len(raw)} bytes < required {min_bytes}")

    html = raw.decode("utf-8", errors="replace")

    blocks = LD_JSON.findall(html)
    if not blocks:
        failures.append("no <script type=\"application/ld+json\"> block")
    for i, block in enumerate(blocks, 1):
        try:
            json.load(__import__("io").StringIO(block))
        except Exception as exc:
            failures.append(f"JSON-LD block {i} does not parse: {exc}")

    if 'rel="canonical"' not in html and "rel='canonical'" not in html:
        failures.append("no canonical link tag")

    if "disclaim-box" not in html and "class=\"disclaimer\"" not in html:
        failures.append("no disclaimer block")

    opened, closed = len(OPEN_DIV.findall(html)), len(CLOSE_DIV.findall(html))
    if opened != closed:
        failures.append(f"unbalanced <div>: {opened} open vs {closed} close")

    opened, closed = len(OPEN_SVG.findall(html)), len(CLOSE_SVG.findall(html))
    if opened != closed:
        failures.append(f"unbalanced <svg>: {opened} open vs {closed} close")

    words = len(WORDS.findall(strip_markup(html)))
    tables = len(TABLE.findall(html))
    figures = len(FIGURE.findall(html))
    warnings = []
    if words < 2000:
        warnings.append(f"only {words} words (2,000+ expected)")
    if tables < 2:
        warnings.append(f"only {tables} table(s) (2 expected)")
    if figures < 1:
        warnings.append("no <figure>")

    label = "FAIL" if failures else "PASS"
    print(f"{label}  {path}  {len(raw):,} bytes  {words:,} words  "
          f"{tables} table(s)  {figures} figure(s)  {len(blocks)} JSON-LD block(s)")
    for w in warnings:
        print(f"      WARNING: {w}")
    for f in failures:
        print(f"      FAIL: {f}")
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", help="HTML files to check")
    ap.add_argument("--min-bytes", type=int, default=8000)
    args = ap.parse_args()
    bad = sum(1 for p in args.paths if check(p, args.min_bytes))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
