import os, sys, types, tempfile
os.environ["SEO_DB"] = tempfile.mktemp()
fake = {}
for m in ("gsc", "wp", "propose"): fake[m] = types.ModuleType("seo_agent." + m); sys.modules["seo_agent." + m] = fake[m]
from seo_agent import run, db, sb
STORE = {"sites": [], "proposals": [], "gsc_daily": []}; CALLS = []
def setup(mode, approved=None, applied_today=0):
    STORE["sites"] = [{"id": "k", "mode": mode, "daily_page_limit": 1}]
    STORE["proposals"] = list(approved or []); CALLS.clear()
    sb.select = lambda t, **p: [r for r in STORE[t] if all(str(r.get(k)) == v.split(".", 1)[1] for k, v in p.items() if v.startswith("eq."))] if t != "proposals" or "applied_at" not in p else [{}] * applied_today
    sb.insert = lambda t, rows: [dict(r, id=len(STORE[t]) + 1) for r in rows if STORE[t].append(r) is None]
    sb.upsert = lambda *a, **k: None
    sb.update = lambda t, m, d: [r.update(d) for r in STORE[t] if all(r.get(k) == v for k, v in m.items())]
    c = db.conn(); c.execute("DELETE FROM changes"); c.commit()
    run.sites = lambda: {"k": {"id": "k", "domain": "k.in", "type": "wordpress", "mode": mode, "daily_page_limit": 1, "gsc_property": "x",
                               "wp_base": "x", "banned_terms": [], "competitor_names": []}}
    class W:
        def __init__(s, site): pass
        def live(s, u): return {"title": "Old", "meta": "old", "h1": "h", "h2": [], "post_id": "5"}
        def set_seo(s, pid, t, m): CALLS.append((pid, t, m)); return {"backup_id": 7}
    fake["wp"].WP = W; fake["wp"].client = lambda site: W(site)
    fake["gsc"].pull = lambda c, s: []; fake["gsc"].top_queries = lambda s, p: [{"q": "q", "impr": 5, "pos": 7}]
    T = "Tummy Tuck in Gurgaon: Cost, Types & Recovery | Kalosa"; M = "Tummy tuck surgery in Gurgaon for loose skin and stretched muscles. Know the cost, types, recovery and risks. Book."
    fake["propose"].propose = lambda *a, **k: {"title": T, "meta": M, "why": "w"}
    run.pick = lambda c, s: {"page": "https://k.in/p"}
def test_dry_run_creates_pending_only():
    setup("dry-run"); run.daily("k")
    assert CALLS == [] and STORE["proposals"][0]["status"] == "pending"
def test_live_applies():
    setup("live"); run.daily("k")
    assert len(CALLS) == 1 and STORE["proposals"][0]["status"] == "applied" and STORE["proposals"][0]["backup_id"] == 7
def test_paused_does_nothing():
    setup("paused"); run.daily("k"); assert CALLS == [] and STORE["proposals"] == []
def test_approved_proposal_gets_applied():
    ap = {"id": 1, "site_id": "k", "status": "approved", "kind": "title_meta", "post_id": 9, "page": "p", "new": {"title": "T", "meta": "M"}}
    setup("dry-run", [ap]); run.daily("k")
    assert CALLS[0] == (9, "T", "M") and STORE["proposals"][0]["status"] == "applied"

def test_rollback_requested_is_executed():
    rb = {"id": 4, "site_id": "k", "status": "rollback_requested", "kind": "title_meta", "post_id": 9, "page": "p", "backup_id": 3, "new": {}}
    setup("dry-run", [rb]); RB = []
    import seo_agent.run as r
    class W2(fake["wp"].WP):
        def rollback(s, pid, bid): RB.append((pid, bid))
    fake["wp"].WP = W2; fake["wp"].client = lambda site: W2(site); r.daily("k")
    assert RB == [(9, 3)] and STORE["proposals"][0]["status"] == "rolled_back"
