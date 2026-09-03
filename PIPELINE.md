# MigrateState Daily Pipeline

Automated daily content + accuracy + indexing pipeline. Approved by Barak, Sept 2026.

## What runs, when

**`daily-pipeline.yml`** — every day 05:30 UTC (08:30 Israel summer / 07:30 winter), or manually from the Actions tab:

1. **Fact-check** (`scripts/fact_check.py`) — 5 pages per day, oldest-checked first, so every page is re-verified roughly weekly. Claims are checked with live web search against **official sources only** (government portals, official gazettes, IRS/FinCEN). Confirmed corrections are applied with the source URL logged in `CORRECTIONS.md`; anything uncertain is flagged in the PR for human review, never guessed.
2. **New article** (`scripts/content_engine.py`) — one per day from `queue.json`, generated in the site's exact existing template (schema, Key Facts, FAQ, internal links, disclaimer), researched with web search against official sources. Updates the relevant hub page and `progress.json`.
3. **SEO health** (`scripts/seo_health.py --fix`) — OG images, meta tags, breadcrumbs, hub indexes, `sitemap.xml` rebuild.
4. **PR gate** — everything lands in a `daily/YYYY-MM-DD` branch with a summary PR. **Nothing goes live until Barak merges.** Merge → Cloudflare Workers Builds deploys automatically.

**`indexnow.yml`** — on every merge to `main` that touches `public/*.html`: submits the changed URLs to IndexNow (Bing, Yandex, Seznam, Naver + partners) using the existing site key. Google indexing rides on `sitemap.xml` lastmod, rebuilt every run; optional future upgrade: Google Search Console API submission via a service account.

## Setup (one-time)

1. Repo → Settings → Secrets and variables → Actions → **New repository secret**: `ANTHROPIC_API_KEY`.
2. Commit these files. The first run can be triggered manually (Actions → Daily Content Pipeline → Run workflow).
3. Keep the repo active: GitHub disables cron on repos with 60 days of no activity — daily merges prevent this.

## Operating rules

- Empty queue → the run says so in the PR; refill `queue.json`.
- A generated article under 8KB is discarded automatically (quality guard).
- After ~1 month of clean runs, consider auto-merging fact-check-only PRs (corrections always carry an official source); new articles keep the human gate.
