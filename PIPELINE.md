# MigrateState Daily Pipeline

Automated daily content + accuracy + indexing pipeline. Approved by Barak, Sept 2026.
Switched to fully-automatic (no PR review gate) on 2026-09-04, at Barak's request —
see "Why the PR gate was removed" below before reinstating it.

## Where it runs (2026-09-04 onward)

The daily run is a **device-bound Claude scheduled task** on Barak's Windows PC
(Claude desktop app, folder `Claude\Projects\migratestate.com\migratestate-site`,
05:30 UTC). Claude itself does the fact-check and writes the article (web research
against official sources), runs `scripts/seo_health.py --fix`, commits and pushes to
`main` with a repo-scoped token stored only in that clone's `.git/config`.
The GitHub Actions workflow below is the **backup path** (disabled at the GitHub
level — Actions tab → enable it; the `ANTHROPIC_API_KEY` secret exists but the
account behind it was out of credits as of 2026-09-14). `indexnow.yml` still fires
on every push.

### Known outage: Cowork device_bash dead since 2026-09-09 (Windows KB5124008)

Windows 11 update **KB5124008** (8 Sep 2026, build 26200.9445) broke Plan9 host
folder shares for HCS-managed VMs. The Cowork VM boots, but every share fails with
`Plan9 mount failed: invalid argument`, so `device_bash` reports "no Plan9 drive
shares mounted" and the daily task cannot clone, run scripts, commit or push.
Microsoft has acknowledged it (it also breaks WSL) and, as of 2026-09-14, has not
shipped a fix; there is no in-app workaround, and restarting the app or the PC does
not help. The only remedy is to remove the update and pause Windows updates —
`.pipeline/fix-plan9-kb5124008.ps1` does it (run elevated, then reboot).

**While device_bash is down, publish from Claude Code / a terminal on the PC**
(plain git works there — it is unaffected). Anything already written during a
failed run is not lost: check the working tree and `git stash list` before
resetting anything.

### Second runner: local Windows scheduled task (2026-09-14 onward)

A backup runner that does not touch Cowork's VM at all, so a Plan9-style
regression cannot stop the site again. It uses the **Claude Code CLI on the
subscription — no API credits.**

- Task: **"MigrateState Daily Pipeline (local)"** in Windows Task Scheduler,
  daily 08:30 local, `StartWhenAvailable` (it catches up if the PC was off).
- It runs `.pipeline\run-daily.ps1`, which pipes `.pipeline\local-task-prompt.md`
  into `claude -p` and tees everything to `.pipeline\runs\local-<date>.log`.
  The task's "Last Run Result" is the script's exit code, so a failure is visible
  without opening the log. `-DryRun` reports what it would do and changes nothing.
- CLI: install it stably with `npm install -g @anthropic-ai/claude-code`
  (`%APPDATA%\npm\claude.cmd`). The copy inside the VS Code extension is only a
  fallback — its path carries the extension version, so it moves on every update.
- Permissions: `--permission-mode acceptEdits` plus an allowlist passed as flags
  (`git *`, `python scripts/*`, WebSearch, WebFetch; force-push, `reset --hard`,
  `clean` and `rm -rf` denied). `.claude/settings.json` mirrors the same rules for
  interactive sessions.
- **The workspace must be trusted or the whole thing silently does nothing.**
  Claude Code ignores permission rules — from settings *and* from `--allowedTools`
  — in an untrusted workspace, so every git/python step is refused and an
  unattended run has nobody to answer the prompt. Trust is recorded per project
  path in `~/.claude.json` (`hasTrustDialogAccepted: true`); note the path can
  appear under both `c:/...` and `C:/...`, and both spellings must say true.
  Accepting the trust dialog once in an interactive `claude` session does it too.


## Additional steps since the Sept 2026 SEO audit (MANDATORY — read before step 2)

The scheduled task's stored prompt predates these; this section adds detail to
its steps 2–4 and MUST be followed on every run. The full updated prompt is
mirrored at `.pipeline/task-prompt.md` in the connected folder.

**Sources block (`scripts/sources.json`).** `seo_health.py --fix` renders a
"Sources & official references" block on every article from
`scripts/sources.json` → `"pages"["<slug>"]` = list of `{"n": "<Authority —
document>", "u": "<url>"}`, plus `"rules"` matched by slug pattern. This is the
site's E-E-A-T signal. Rules:
- Fact-check (step 2): every official URL actually used to verify a page is
  added to that page's list (no duplicates, max 6, drop generic portal
  homepages first). A page must never end a fact-check with fewer sources.
- New article (step 3): register 3–6 official sources for the new slug BEFORE
  running seo_health. Never hand-write the Sources or Related-guides blocks —
  both are generated (markers `<!-- sources:start -->` / `<!-- related:start -->`).

**Figures.** Every article should carry at least one inline-SVG figure:
`<figure class="fig"><svg viewBox="0 0 720 300" role="img" aria-labelledby="…">
<title>…</title>…</svg><figcaption>…</figcaption></figure>` placed after the
section it illustrates (cost-breakdown bars from the page's own cost table, or a
step timeline from its step-by-step section). Palette: navy #0f2a43, gold
#c8a24a, cream #f7f4ee, muted #96a8ba; system font; no external assets; only
numbers already on the page — skip rather than invent.
- Fact-check: if a checked page has no `<figure>`, add one.
- New article: at least TWO data tables and ONE figure. If the queue entry asks
  for an interactive calculator, implement it as inline JS (no external
  scripts, no network calls, labelled defaults).

**Step 3b — flagship expansion (`expansions.json`), one per run, BEFORE the new
article.** Pick the first item with `"status": "open"`. Rewrite the page in place
to the brief's `target_words` (±10%): keep URL, `<head>`, H1, schema types,
answer box, byline (refresh "Updated <Month Year>"), CTA boxes, disclaimer,
nav/footer and the FAQ (update, extend to 8 items, mirror in FAQPage JSON-LD).
Add the tables and figure the brief asks for; cite every threshold to an
official source and add those URLs to `sources.json`. Quality gate: file grows
by ≥ 8 KB, `<div>` balanced, JSON-LD parses — otherwise discard and report the
step FAILED (old page stays live). On success set `"status": "done <TODAY>"` and
write `### Expansion` to the summary. If the run is short on time the new
article may be skipped (WARNING), the expansion may not. Status line gains
`expansion: /<slug>|none`.

**Queue.** `queue.json` was re-prioritised on 2026-09-05: 25 audit-driven topics
first (fideicomiso + calculator, retirement/income-visa cluster, Golden Visa
Index, comparisons, city guides), then the original topics.

## What runs, when

**`daily-pipeline.yml`** — every day 05:30 UTC (08:30 Israel summer / 07:30 winter), or manually from the Actions tab:

> **The Python engines were upgraded on 2026-09-14** so this workflow produces the
> same shape of work as the Cowork task: `fact_check.py` now appends uncertain items
> to `FLAGS.md` as `**OPEN**` (they used to reach only `.pipeline_summary.md`, which
> is overwritten every run — so they were silently lost) and registers the official
> URLs it used in `scripts/sources.json`; `content_engine.py` registers 3–6 sources
> for the new slug, asks for two tables and an inline-SVG figure, and runs a quality
> gate (size, JSON-LD parses, canonical, disclaimer, balanced divs — missing
> tables/figure are warnings in the summary, not a blocked article). It also streams
> the completion: the non-streaming call died with "Streaming is required for
> operations that may take longer than 10 minutes" on 2026-09-09. Default model:
> `claude-sonnet-5`.

1. **Fact-check** (`scripts/fact_check.py`) — 5 pages per day, oldest-checked first, so every page is re-verified roughly weekly. Claims are checked with live web search against **official sources only** (government portals, official gazettes, IRS/FinCEN). Confirmed corrections are applied with the source URL logged in `CORRECTIONS.md`; anything uncertain is flagged in the commit message for later human review, never guessed.
2. **New article** (`scripts/content_engine.py`) — one per day from `queue.json`, generated in the site's exact existing template (schema, Key Facts, FAQ, internal links, disclaimer), researched with web search against official sources. Updates the relevant hub page and `progress.json`.
3. **SEO health** (`scripts/seo_health.py --fix`) — OG images, meta tags, breadcrumbs, hub indexes, `sitemap.xml` rebuild.
4. **Direct push to `main`** — no PR, no review step. Push → Cloudflare Workers Builds deploys automatically. The commit message carries the fact-check/flag/article summary, so the history is the audit trail.
5. **Verification step (safety net)** — after the push, a final step checks that both the fact-check and article steps actually produced real output today (a `factcheck_state.json` entry dated today, and a `### New article` line in `.pipeline_summary.md`). If either is missing — meaning that step silently crashed — this step fails the whole job, which makes GitHub send its default "workflow run failed" email to the repo owner. This is the only automatic signal of breakage now that nothing is gated on human review, so don't remove it, and check the two step logs above it for the real Python traceback when it fires.

**`indexnow.yml`** — on every push to `main` that touches `public/*.html`: submits the changed URLs to IndexNow (Bing, Yandex, Seznam, Naver + partners) using the existing site key. Google indexing rides on `sitemap.xml` lastmod, rebuilt every run; optional future upgrade: Google Search Console API submission via a service account.

## Why the PR gate was removed

The original design (this file's first version) deliberately kept every new article behind a PR that Barak had to merge by hand — auto-publishing unreviewed legal/tax content to a live site carries real accuracy risk even with the official-sources-only fact-check rule (see `CORRECTIONS.md` and any "flagged, unverifiable" notes in commit messages — the fact-check step already has known blind spots on claims it can't confirm against a primary source). On 2026-09-04 Barak explicitly asked for zero-touch automation instead, so the PR step was replaced with a direct push plus the verification/notification safety net above. If article accuracy issues start showing up live, the fix is to reinstate the PR step (restore `pull-requests: write` permission and the branch+PR block that was here before) rather than trying to make the fact-check step itself stricter — it already declines to guess.

## Setup (one-time)

1. Repo → Settings → Secrets and variables → Actions → **New repository secret**: `ANTHROPIC_API_KEY`. If runs start silently producing nothing (check for the failure email from the verification step above), re-check this secret is present and the key still has quota/credits — that's the most likely cause of a fully-empty run.
2. Commit these files. The first run can be triggered manually (Actions → Daily Content Pipeline → Run workflow).
3. Keep the repo active: GitHub disables cron on repos with 60 days of no activity — daily pushes prevent this.

## Operating rules

- Empty queue → the run says so in `.pipeline_summary.md` (committed); refill `queue.json`.
- A generated article under 8KB is discarded automatically (quality guard) — content_engine.py exits nonzero in that case, which the verification step catches.
- Unverifiable/flagged fact-check claims are never auto-corrected — they're only ever noted, in the commit message, for a human to check later. Reinstating the PR gate for new articles specifically (see above) is the natural next safeguard if fully-blind publishing turns out to be too risky in practice.
