#!/usr/bin/env python3
"""
MigrateState — IndexNow submitter.

Runs on every push to main. Collects public/*.html files changed in the push,
maps them to live URLs and submits them to api.indexnow.org (which fans out to
Bing, Yandex, Seznam, Naver and other IndexNow partners).

Google does not use IndexNow: it discovers updates via sitemap.xml, whose
lastmod values seo_health rebuilds on every pipeline run.
"""
from __future__ import annotations

import os
import subprocess
import sys

import requests

HOST = os.environ.get("SITE_HOST", "migratestate.com")
KEY = os.environ["INDEXNOW_KEY"]
BEFORE = os.environ.get("BEFORE_SHA", "")
AFTER = os.environ.get("AFTER_SHA", "HEAD")


def changed_files() -> list[str]:
    if BEFORE and not BEFORE.startswith("0000000"):
        rng = f"{BEFORE}..{AFTER}"
    else:  # first push / force push — fall back to last commit
        rng = f"{AFTER}~1..{AFTER}"
    out = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AM", rng],
        capture_output=True, text=True, check=True).stdout
    return [f for f in out.splitlines()
            if f.startswith("public/") and f.endswith(".html")]


def to_url(path: str) -> str:
    name = path[len("public/"):-len(".html")]
    return f"https://{HOST}/" if name == "index" else f"https://{HOST}/{name}"


def main() -> None:
    urls = sorted({to_url(f) for f in changed_files()})
    if not urls:
        print("No changed HTML pages — nothing to submit.")
        return
    payload = {
        "host": HOST,
        "key": KEY,
        "keyLocation": f"https://{HOST}/{KEY}.txt",
        "urlList": urls[:10000],
    }
    print(f"Submitting {len(urls)} URL(s) to IndexNow:")
    for u in urls:
        print(f"  {u}")
    r = requests.post("https://api.indexnow.org/indexnow",
                      json=payload, timeout=30)
    print(f"IndexNow response: {r.status_code}")
    if r.status_code not in (200, 202):
        sys.exit(f"IndexNow submission failed: {r.status_code} {r.text[:300]}")


if __name__ == "__main__":
    main()
