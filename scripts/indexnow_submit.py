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


def _commit_exists(rev: str) -> bool:
    if not rev:
        return False
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
        capture_output=True,
    ).returncode == 0


def _resolve_before() -> str:
    """Return BEFORE when it is in the clone, else "" (diff the latest commit).

    A depth-2 checkout only contains HEAD and its parent. On a multi-commit
    push, github.event.before is then missing and `git diff BEFORE..AFTER`
    exits 128 (IndexNow run #22). Deepen a shallow clone so the real push
    range still works; if the commit is gone anyway, fall back instead of
    crashing.
    """
    if not BEFORE or BEFORE.startswith("0000000"):
        return ""
    if _commit_exists(BEFORE):
        return BEFORE
    shallow = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        capture_output=True, text=True,
    ).stdout.strip() == "true"
    if shallow:
        print(
            f"BEFORE commit {BEFORE} is not in this checkout; fetching full history.",
            file=sys.stderr,
        )
        fetched = subprocess.run(
            ["git", "fetch", "--no-tags", "--unshallow", "origin"],
            capture_output=True, text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if fetched.returncode != 0:
            err = (fetched.stderr or fetched.stdout).strip()
            print(f"git fetch --unshallow failed: {err}", file=sys.stderr)
        elif _commit_exists(BEFORE):
            return BEFORE
    print(
        f"BEFORE commit {BEFORE} is unavailable; falling back to the latest commit.",
        file=sys.stderr,
    )
    return ""


def changed_files() -> list[str]:
    before = _resolve_before()
    if before:
        rng = f"{before}..{AFTER}"
    else:  # first push, force push, or BEFORE still not in the clone
        rng = f"{AFTER}~1..{AFTER}"
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AM", rng],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.exit(
            f"git diff {rng} failed ({proc.returncode}): {(proc.stderr or '').strip()}"
        )
    return [f for f in proc.stdout.splitlines()
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
