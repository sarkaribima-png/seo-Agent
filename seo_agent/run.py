import sys, json, yaml
from . import db, drops, guard
def sites(): return {s["id"]: s for s in yaml.safe_load(open("sites.yaml"))["sites"]}
def pick(c, site):
    d = drops.detect(c, site["id"]); o = drops.opportunities(c, site["id"])
    seen = set()
    for x in d + o:                                   # drops first, then easy wins
        if x["page"] in seen or db.recently_changed(c, site["id"], x["page"]): continue
        seen.add(x["page"]); return x
def daily(site_id):
    from . import gsc, wp, propose
    site = sites()[site_id]; c = db.conn()
    print("pulled rows:", gsc.pull(c, site))
    if db.changed_today(c, site_id) >= site["daily_page_limit"]: return print("daily limit reached")
    t = pick(c, site)
    if not t: return print("no candidate page")
    w = wp.WP(site); live = w.live(t["page"]); q = gsc.top_queries(site, t["page"])
    err = ""
    for _ in range(3):
        p = propose.propose(t["page"], live, q, err); e = guard.check_seo(p["title"], p["meta"], site)
        if not e: break
        err = "; ".join(e)
    else:
        return db.log_change(c, site_id, t["page"], "title", live, p, "rejected", err)
    old = {"title": live["title"], "meta": live["meta"]}
    if site["mode"] == "live" and live["post_id"]:
        w.set_seo(live["post_id"], p["title"], p["meta"]); st = "applied"
    else: st = "dry-run"
    db.log_change(c, site_id, t["page"], "title", old, {"title": p["title"], "meta": p["meta"], "post_id": live["post_id"]}, st, p["why"])
    print(st, t["page"]); print("OLD title:", old["title"]); print("OLD meta :", old["meta"])
    print("NEW title:", p["title"]); print("NEW meta :", p["meta"]); print("WHY:", p["why"]); print("QUERIES:", q[:5])
def rollback(site_id, change_id):
    from . import wp
    site = sites()[site_id]; c = db.conn()
    r = c.execute("SELECT * FROM changes WHERE id=? AND site=?", (change_id, site_id)).fetchone()
    old, new = json.loads(r["old"]), json.loads(r["new"])
    wp.WP(site).set_seo(new["post_id"], old["title"], old["meta"])
    db.log_change(c, site_id, r["page"], "title", new, old, "rolled_back", f"rollback of {change_id}")
if __name__ == "__main__":
    cmd = sys.argv[1]
    daily(sys.argv[2]) if cmd == "daily" else rollback(sys.argv[2], int(sys.argv[3]))
