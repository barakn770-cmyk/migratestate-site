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
The GitHub Actions workflow below is kept as a **dormant manual backup** (its cron is
disabled; it needs `ANTHROPIC_API_KEY`). `indexnow.yml` still fires on every push.

## What runs, when

**`daily-pipeline.yml`** — every day 05:30 UTC (08:30 Israel summer / 07:30 winter), or manually from the Actions tab:

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
