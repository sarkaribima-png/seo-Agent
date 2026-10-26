-- Run once in Supabase SQL Editor (after schema.sql). Safe to re-run.
alter table proposals drop constraint if exists proposals_status_check;
alter table proposals add constraint proposals_status_check
  check (status in ('pending','approved','rejected','applied','rolled_back','failed','rollback_requested'));
drop policy if exists "auth decide proposals" on proposals;
create policy "auth decide proposals" on proposals for update to authenticated
  using (status in ('pending','applied'))
  with check (status in ('approved','rejected','rollback_requested'));
create or replace view gsc_by_day with (security_invoker = true) as
  select site_id, date, device, sum(clicks) as clicks, sum(impressions) as impressions,
         sum(position * impressions) / nullif(sum(impressions), 0) as position
  from gsc_daily group by site_id, date, device;
create or replace view page_audits_latest with (security_invoker = true) as
  select distinct on (site_id, page) id, site_id, page, post_id, audited_at, h1, h2, h3, words, images,
         images_no_alt, risky_claims, schema_issues
  from page_audits order by site_id, page, audited_at desc;
