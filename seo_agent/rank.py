"""Rank a page: keyword + your page + competitor page(s) -> built-in audit -> proposals for the dashboard.
Run: python -m seo_agent.rank <site_id>"""
import os, re, sys, json, time, datetime as dt
from urllib.parse import urlparse
from . import sb, audit as auditmod
from .humanize import RULES, lint, plain

MODEL = os.environ.get("CONTENT_MODEL", "claude-sonnet-5-5")
JOBS_PER_RUN = int(os.environ.get("RANK_JOBS_PER_RUN", "2"))
NEVER_SCHEMA = {"aggregaterating", "review", "rating", "offer", "aggregateoffer"}   # claims we cannot prove


def norm(u): return (u or "").split("#")[0].rstrip("/").lower()
def now(): return dt.datetime.now(dt.timezone.utc).isoformat()


def ask(system, user, max_tokens=4000):
    import anthropic
    r = anthropic.Anthropic().messages.create(model=MODEL, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")


def top_pages(cfg, own, n=12):
    """Our own best pages (by Google impressions) that the audit may suggest linking to."""
    from . import gsc, wp
    s = gsc.service(); end = dt.date.today() - dt.timedelta(days=2); start = end - dt.timedelta(days=28)
    rows = s.searchanalytics().query(siteUrl=cfg["gsc_property"], body={"startDate": str(start), "endDate": str(end), "dimensions": ["page"], "rowLimit": 60}).execute().get("rows", [])
    rows.sort(key=lambda r: -r["impressions"]); w = wp.WP(cfg); out = []
    for r in rows:
        u = r["keys"][0]
        if norm(u) == norm(own) or urlparse(u).path.strip("/") == "" or "?" in u: continue
        try:
            live = w.live(u); topic = (live["h1"] or live["title"] or "").strip()
        except Exception:
            continue
        if topic: out.append({"url": u, "topic": topic[:150]})
        if len(out) >= n: break
    return out


def candidates(cfg, own):
    try: c = top_pages(cfg, own)
    except Exception as e:
        print("Search Console not readable, using the site's own page list instead:", str(e)[:100]); c = []
    return c or auditmod.wp_pages(cfg["wp_base"], own)


def do_audit(job, cfg):
    return auditmod.audit(job["keyword"], job["page_url"], job.get("competitor_urls") or [], candidates(cfg, job["page_url"]), cfg.get("niche") or "healthcare", ask)


# ---------- proposal builders ----------
TM_SYS = """You are an SEO editor. Write ONE title and ONE meta description for a page that must rank for the target keyword.
Rules: title 30-60 chars and starts with or clearly contains the target keyword; meta 110-160 chars; use only facts present in the input;
never claim rankings, guarantees, percentages, 'best', FDA/NABH or competitor names; keep a city name if the current title has one.
Use the audit insights to add a concrete reason to click, but never copy competitor wording. Reply JSON only: {"title":"","meta":"","why":""}"""


def title_meta(job, live, insights, site_cfg):
    from . import guard
    fb = ""
    for _ in range(3):
        msg = {"keyword": job["keyword"], "current_title": live["title"], "current_meta": live["meta"], "h1": live["h1"], "h2": live.get("h2", []),
               "audit_insights": insights, "previous_errors": fb}
        t = ask(TM_SYS, json.dumps(msg), 1500)
        try: d = json.loads(t[t.index("{"): t.rindex("}") + 1])
        except ValueError: fb = "return JSON only"; continue
        errs = guard.check_seo(d.get("title", ""), d.get("meta", ""), site_cfg)
        if not errs: return d
        fb = "; ".join(errs)
    return None


SEC_SYS = """You write ONE new section for an existing web page, from a content brief. Reader: a normal adult, not an expert.
Reading level: US grade 5-6, short sentences, plain words, explain any medical term in the same sentence. Length 120-220 words.
Use ONLY facts from 'brief' and 'brain', or very general well known knowledge stated carefully. Never invent numbers, prices, timelines, studies, awards or patient stories.
Never promise results, never name competitors, no call-to-action, no contact details. HTML only: one h2, then p, ul, li (h3 allowed). No bold, no inline styles.
""" + RULES + """
Reply JSON only: {"heading":"","html":"<h2>..</h2><p>..</p>","why":""}"""


def section(brief, brain, site):
    from .content import grade
    banned = site.get("banned_terms") or []; fb = ""
    for _ in range(2):
        t = ask(SEC_SYS, json.dumps({"brief": brief, "brain": brain, "problems": fb}), 3000)
        try: d = json.loads(t[t.index("{"): t.rindex("}") + 1])
        except ValueError: fb = "return JSON only"; continue
        html = d.get("html", ""); issues = lint(html, banned, True); g = grade(html)
        if g > 6.5: issues.append(f"reading grade {g}, must be 6 or lower: shorter sentences, simpler words")
        if not issues: d["grade"] = g; return d
        fb = "; ".join(issues)
    return None


def build(job, rep, own, live, site_cfg, site_row, pid):
    audit = rep.get("audit") or {}; own_n = norm(own); out = []
    recs = [r for r in (rep.get("recommendations") or []) if norm(r.get("page_url")) == own_n][:8]
    insights = {"hypothesis": [h.get("statement") for h in (rep.get("ranking_hypothesis") or [])][:4],
                "recommendations": [{"priority": r["priority"], "action": r["action"]} for r in recs],
                "content_gaps": [g.get("topic") for g in (audit.get("page_comparison") or {}).get("content_gaps", []) if own_n in [norm(x) for x in g.get("missing_on", [])]][:8]}
    base = {"site_id": site_row["id"], "page": own, "post_id": pid, "needs_approval": True, "status": "pending", "rank_job_id": job["id"], "source": f"rank:{job['keyword']}"}
    # 1 title + meta
    d = title_meta(job, live, insights, site_cfg)
    if d and (d["title"], d["meta"]) != (live["title"], live["meta"]):
        out.append({**base, "kind": "title_meta", "old": {"title": live["title"], "meta": live["meta"]}, "new": {"title": d["title"], "meta": d["meta"]},
                    "reason": f"For '{job['keyword']}': " + (d.get("why") or "")})
    # 2 schema (only types the page does not already have, never rating/review claims)
    present = json.dumps([x.get("schema") for x in audit.get("schema_analysis", []) if norm(x.get("url")) == own_n]).lower()
    for blk in audit.get("schema_recommendations", []):
        if norm(blk.get("url")) != own_n: continue
        for sr in blk.get("recommendations", [])[:4]:
            typ = str(sr.get("schema_type", "")); ld = sr.get("json_ld") or {}; txt = json.dumps(ld).lower()
            if not typ or typ.lower() in NEVER_SCHEMA or typ.lower() in present or "@type" not in ld: continue
            if any(n in txt for n in NEVER_SCHEMA if n not in ("offer",)) or any((b or "").lower() in txt for b in site_row.get("banned_terms") or []): continue
            ld.setdefault("@context", "https://schema.org")
            out.append({**base, "kind": "schema", "old": {}, "new": {"type": typ, "json_ld": ld}, "reason": sr.get("rationale", "")[:300]})
    # 3 internal links
    for blk in audit.get("internal_link_suggestions", []):
        if norm(blk.get("url")) != own_n: continue
        for s in (blk.get("suggestions") or [])[:5]:
            tgt = s.get("link_to_url", "")
            if urlparse(tgt).netloc.replace("www.", "") != urlparse(own).netloc.replace("www.", "") or norm(tgt) == own_n: continue
            out.append({**base, "kind": "internal_link", "old": {}, "new": {"anchor": s.get("anchor_text", "")[:80], "url": tgt, "placement": s.get("placement_context", "")[:240]},
                        "reason": "Internal link suggested by the audit"})
    # 4 new sections from briefs
    from .content import brain_context
    brain = brain_context(site_row["id"], site_row.get("profile"))
    for b in [b for b in (rep.get("content_briefs") or []) if own_n in [norm(x) for x in b.get("target_page_urls", [])]][:2]:
        sec = section(b, brain, site_row)
        if sec: out.append({**base, "kind": "content_add", "old": {}, "new": {"heading": sec.get("heading", ""), "html": sec["html"], "position": "before_faq", "grade": sec["grade"]},
                            "reason": f"Competitors cover '{b.get('topic')}'. " + (sec.get("why") or "")[:200]})
    return out


def summary(rep):
    return {"scores": rep.get("scores"), "hypothesis": [h.get("statement") for h in (rep.get("ranking_hypothesis") or [])][:5],
            "recommendations": [r.get("action") for r in (rep.get("recommendations") or [])][:8],
            "confidence": rep.get("confidence"), "missing_data": rep.get("missing_data")}


def run(site_id):
    from . import run as runmod, wp
    cfg = runmod.sites()[site_id]; site = runmod.ensure_site(cfg)
    if site["mode"] == "paused": return print("site paused")
    w = wp.WP(cfg)
    jobs = sb.select("rank_jobs", site_id=f"eq.{site_id}", status="in.(queued,auditing)", order="created_at.asc", limit=str(JOBS_PER_RUN))
    if not jobs: return print("no rank jobs waiting")
    for job in jobs:
        try:
            sb.update("rank_jobs", {"id": job["id"]}, {"status": "auditing", "started_at": now(), "updated_at": now()})
            rep = do_audit(job, {**cfg, **site})
            live = w.live(job["page_url"]); pid = int(live["post_id"]) if live.get("post_id") else None
            props = build(job, rep, job["page_url"], live, cfg, {**cfg, **site}, pid)
            if props: sb.insert("proposals", props)
            sb.update("rank_jobs", {"id": job["id"]}, {"status": "ready", "report": summary(rep), "proposals_made": len(props), "error": None, "updated_at": now()})
            print("rank job done:", job["keyword"], "| proposals:", len(props))
        except Exception as e:
            sb.update("rank_jobs", {"id": job["id"]}, {"status": "failed", "error": str(e)[:400], "updated_at": now()}); print("FAILED", job["keyword"], e)


if __name__ == "__main__":
    run(sys.argv[1])
