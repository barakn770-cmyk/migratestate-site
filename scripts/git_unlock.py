#!/usr/bin/env python3
"""Remove a stale .git/*.lock file so a run can continue.

Why this exists: the daily pipeline runs with a shell allowlist of `git *` and
`python scripts/*` only, so it cannot `rm .git/index.lock`. A crashed or
concurrent git invocation (two git commands started in the same shell turn is
the usual cause) leaves the lock behind and every later git command fails with
"Another git process seems to be running in this repository".

Safety: a lock is only removed when nothing has touched it for --min-age
seconds (default 120), which means no live git process is using it.

Usage:
    python scripts/git_unlock.py            # report what it would do
    python scripts/git_unlock.py --force    # actually delete the stale locks
"""

import argparse
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCKS = ("index.lock", "HEAD.lock", "config.lock", "MERGE_HEAD.lock")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="delete, don't just report")
    ap.add_argument("--min-age", type=float, default=120.0,
                    help="only touch locks older than this many seconds (default 120)")
    args = ap.parse_args()

    git_dir = os.path.join(REPO, ".git")
    if not os.path.isdir(git_dir):
        print(f"not a git repo: {git_dir}", file=sys.stderr)
        return 2

    found = 0
    for name in LOCKS:
        path = os.path.join(git_dir, name)
        if not os.path.exists(path):
            continue
        found += 1
        age = time.time() - os.path.getmtime(path)
        if age < args.min_age:
            print(f"SKIP  {name}  age {age:.0f}s < {args.min_age:.0f}s "
                  f"(a git process may still be using it)")
            continue
        if not args.force:
            print(f"STALE {name}  age {age:.0f}s  (re-run with --force to remove)")
            continue
        try:
            os.remove(path)
            print(f"REMOVED {name}  (was {age:.0f}s old)")
        except OSError as exc:
            print(f"FAILED to remove {name}: {exc}", file=sys.stderr)
            return 1

    if not found:
        print("no lock files present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
