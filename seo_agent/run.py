import sys, json, time, yaml, datetime as dt
from . import db, drops, guard, sb
def sites():
    """Sites from sites.yaml, plus every site added in the dashboard (database wins where it has a value)."""
    out = {s["id"]: dict(s) for s in yaml.safe_load(open("sites.yaml"))["sites"]}
    try:
        for r in sb.select("sites", select="*"):
            if r.get("active") is False: out.pop(r["id"], None); continue
            base = out.get(r["id"]) or {"daily_page_limit": 1, "banned_terms": [], "competitor_names": [], "secrets_prefix": r["id"].upper()}
            out[r["id"]] = {**base, **{k: v for k, v in r.items() if v not in (None, [], "")}}
    except Exception as e: print("note: could not read sites from the database:", str(e)[:80])
    return out
def site_ids(arg, kinds=None):
    s = sites()
    if arg != "all" and arg not in s: raise SystemExit(f"unknown or inactive site: {arg}")
    return [i for i in s if (arg == i or (arg == "all" and s[i].get("mode") != "paused")) and (not kinds or s[i].get("type", "wordpress") in kinds)]
def each(arg, fn, kinds=None):
    """Run fn(site_id) for one site, or for every active site. One site failing does not stop the others."""
    bad = 0
    for sid in site_ids(arg, kinds):
        print(f"--- {sid} ---")
        try: fn(sid)
        except Exception as e: bad += 1; print(f"SITE {sid} FAILED: {e}")
    if bad: sys.exit(1)
def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def ensure_site(site):
    r = sb.select("sites", id=f"eq.{site['id']}")
    if r: return r[0]
    return sb.insert("sites", [{"id": site["id"], "domain": site["domain"], "type": site["type"], "mode": site["mode"],
            "daily_page_limit": site["daily_page_limit"], "gsc_property": site["gsc_property"], "wp_base": site["wp_base"]}])[0]
def pick(c, site):
    seen = set()
    for x in drops.detect(c, site["id"]) + drops.opportunities(c, site["id"]):
        if x["page"] in seen or db.recently_changed(c, site["id"], x["page"]): continue
        seen.add(x["page"]); return x
def applied_last_day(site_id):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
    return len(sb.select("proposals", site_id=f"eq.{site_id}", status="eq.applied", applied_at=f"gte.{since}"))
MODKINDS = {"schema": lambda n: {"json_ld": n["json_ld"]}, "internal_link": lambda n: {"anchor": n["anchor"], "url": n["url"]},
            "content_add": lambda n: {"html": n["html"], "position": n.get("position") or "before_faq"}}
PLUGIN_KIND = {"schema": "schema", "internal_link": "link", "content_add": "section"}
def apply_mod(site, row, w, p):
    """Schema / link / section: add through the plugin, then check it really shows on the live page. If not, undo it."""
    if p["kind"] == "content_add" and row.get("reviewer_required") and not p.get("reviewed_by"):
        return print("waiting for reviewer on proposal", p["id"], "(Mark as reviewed in the dashboard)")
    if not p.get("post_id"): raise RuntimeError("no post id for this page")
    if p["kind"] == "internal_link":
        a = p["new"]["anchor"].strip().lower()
        if a not in w.page_text(p["page"]).lower():
            raise RuntimeError(f"The words '{p['new']['anchor']}' are not in the text of this page, so there is nothing to link. Nothing was changed. Run a new audit to get a link that fits.")
    r = w.add_mod(p["post_id"], PLUGIN_KIND[p["kind"]], MODKINDS[p["kind"]](p["new"])); mid = r["mod_id"]
    if not w.mod_visible(p["page"], mid):
        w.rollback_mod(p["post_id"], mid)
        raise RuntimeError("change was saved but did not show on the live page (page cache, or the words sit inside a heading or an existing link). It was removed again.")
    sb.update("proposals", {"id": p["id"]}, {"status": "applied", "applied_at": now(), "backup_id": mid, "reason": p.get("reason")})
    print("applied", p["kind"], "proposal", p["id"], p["page"])
def apply_approved(site, w):
    row = sb.select("sites", id=f"eq.{site['id']}")[0]
    for p in sb.select("proposals", site_id=f"eq.{site['id']}", status="eq.approved", kind="in.(title_meta,schema,internal_link,content_add)"):
        try:
            if p["kind"] == "title_meta":
                r = w.set_seo(p["post_id"], p["new"]["title"], p["new"]["meta"])
                sb.update("proposals", {"id": p["id"]}, {"status": "applied", "applied_at": now(), "backup_id": r.get("backup_id")})
                print("applied approved proposal", p["id"], p["page"])
            else: apply_mod(site, row, w, p)
        except Exception as e:
            sb.update("proposals", {"id": p["id"]}, {"status": "failed", "reason": str(e)[:300]}); print("FAILED", p["id"], e)
def apply_rollbacks(site, w):
    for p in sb.select("proposals", site_id=f"eq.{site['id']}", status="eq.rollback_requested"):
        try:
            if p["kind"] == "title_meta": w.rollback(p["post_id"], p["backup_id"])
            else: w.rollback_mod(p["post_id"], p["backup_id"])
            sb.update("proposals", {"id": p["id"]}, {"status": "rolled_back"})
            print("rolled back proposal", p["id"], p["page"])
        except Exception as e:
            sb.update("proposals", {"id": p["id"]}, {"status": "failed", "reason": "rollback: " + str(e)[:250]}); print("ROLLBACK FAILED", p["id"], e)
def daily(site_id):
    from . import gsc, wp, propose
    site = sites()[site_id]; row = ensure_site(site); mode = row["mode"]
    if mode == "paused": return print("site paused from dashboard")
    c = db.conn(); w = wp.client(site)
    rows = gsc.pull(c, site); sb.upsert("gsc_daily", rows, "site_id,date,page,device"); print("gsc rows:", len(rows))
    apply_rollbacks(site, w); apply_approved(site, w)
    if applied_last_day(site_id) >= row["daily_page_limit"]: return print("daily limit reached")
    t = pick(c, site)
    if not t: return print("no candidate page")
    live = w.live(t["page"]); q = gsc.top_queries(site, t["page"]); err = ""
    for _ in range(3):
        p = propose.propose(t["page"], live, q, err); e = guard.check_seo(p["title"], p["meta"], site)
        if not e: break
        err = "; ".join(e)
    else:
        db.log_change(c, site_id, t["page"], "title", live, p, "rejected", err); return print("rejected by guardrails:", err)
    if (p["title"], p["meta"]) == (live["title"], live["meta"]): return print("no change needed", t["page"])
    pid = int(live["post_id"]) if live["post_id"] else None
    prop = {"site_id": site_id, "page": t["page"], "post_id": pid, "kind": "title_meta", "needs_approval": False,
            "old": {"title": live["title"], "meta": live["meta"]}, "new": {"title": p["title"], "meta": p["meta"]},
            "reason": p["why"], "status": "pending"}
    if mode == "live" and pid:
        r = w.set_seo(pid, p["title"], p["meta"]); prop.update(status="applied", applied_at=now(), backup_id=r.get("backup_id"))
    ins = sb.insert("proposals", [prop])[0]
    db.log_change(c, site_id, t["page"], "title", prop["old"], prop["new"], "applied" if prop["status"] == "applied" else "dry-run", p["why"])
    print(prop["status"], t["page"], "-> proposal", ins.get("id"))
def audit_all(site_id):
    from . import wp
    site = sites()[site_id]; ensure_site(site); w = wp.client(site); n = 0
    for pid in w.all_ids():
        try: a = w.audit(pid)
        except Exception as e: print("skip", pid, e); continue
        issues = [("invalid_json_ld" if not s["valid"] else "aggregate_rating") for s in a["schema"] if (not s["valid"] or s["has_aggregate_rating"])]
        if a["h1"] != 1: issues.append(f"h1_count_{a['h1']}")
        sb.insert("page_audits", [{"site_id": site_id, "page": a["url"], "post_id": pid, "h1": a["h1"], "h2": a["h2"], "h3": a["h3"],
                  "words": a["words"], "images": a["images"], "images_no_alt": a["images_no_alt"],
                  "risky_claims": a["risky_claims"], "schema_issues": issues, "raw": a}]); n += 1
    print("audited pages:", n)
def rollback(_ignored, proposal_id):
    from . import wp
    p = sb.select("proposals", id=f"eq.{proposal_id}")[0]; site_id = p["site_id"]; w = wp.client(sites()[site_id])
    (w.rollback if p["kind"] == "title_meta" else w.rollback_mod)(p["post_id"], p["backup_id"]); sb.update("proposals", {"id": proposal_id}, {"status": "rolled_back"}); print("rolled back", proposal_id)
if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "rollback": rollback(None, int(sys.argv[3]))
    else: each(sys.argv[2], {"daily": daily, "audit-all": audit_all}[cmd])
