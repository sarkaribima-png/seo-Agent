# SEO Agent (step 1: kalosa.in only)

Daily flow (GitHub Actions, 09:00 IST): pull Search Console data -> detect drops / near-page-1 opportunities ->
pick ONE page (limit 1/day/site) -> Claude drafts title+meta -> guardrails -> dry-run or live -> log old/new -> rollback possible.

## Setup (once)
1. Create a private GitHub repo, push this folder.
2. Google Cloud: create a service account, enable "Search Console API", download JSON key.
   In Search Console -> Settings -> Users, add the service-account email as *Restricted* user on kalosa.in.
3. WordPress: Users -> Profile -> Application Passwords -> create one for a user that can edit pages.
4. GitHub repo -> Settings -> Secrets -> Actions, add:
   GSC_SERVICE_ACCOUNT_JSON, ANTHROPIC_API_KEY, KALOSA_WP_USER, KALOSA_WP_APP_PASSWORD, SUPABASE_URL, SUPABASE_SERVICE_KEY
   (never paste keys in chat or commit them).
5. Run workflow manually (workflow_dispatch). `mode: dry-run` in sites.yaml only logs proposals.
6. Read data/state.db -> table `changes`. When proposals look right, set `mode: live`.
   The AIOSEO REST call is the one part not yet tested live: check ONE page after the first live run.

## Commands
python -m seo_agent.run daily kalosa
python -m seo_agent.run rollback kalosa <change_id>
python -m pytest -q

## Guardrails
Auto: title, meta description (1 page/day/site, page not touched in last 21 days).
Needs your approval (not implemented as auto): delete, redirect, sitewide settings, content rewrite, schema, noindex.
Banned in copy: guarantees, 100%, FDA/NABH, ranking claims, competitor names (see sites.yaml).

## v0.2
- Needs plugin seo-agent-bridge on the site and supabase/schema.sql run once.
- Mode (dry-run | live | paused) is read from Supabase table `sites` (change it from the dashboard).
- dry-run: proposals are written as `pending` (nothing changes on the site). Set a proposal to `approved` and the next run applies it.
- live: title/meta proposals are applied immediately (1 page/day), with plugin backup.
- Run "audit-all" manually (Actions -> Run workflow -> command: audit-all) to fill page_audits for every page.
