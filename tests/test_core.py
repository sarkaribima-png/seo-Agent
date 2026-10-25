import datetime as dt, os, tempfile
os.environ["SEO_DB"] = tempfile.mktemp()
from seo_agent import db, drops, guard
SITE = {"banned_terms": ["100%", "guarantee"], "competitor_names": ["Acme"]}
def test_guard():
    ok_t, ok_m = "Tummy Tuck in Gurgaon: Cost, Types & Recovery | Kalosa", "Tummy tuck surgery in Gurgaon for loose skin and stretched abdominal muscles. Know the cost in India, types, recovery and risks."
    assert guard.check_seo(ok_t, ok_m, SITE) == []
    assert guard.check_seo(ok_t, ok_m + " 100% safe", SITE)
    assert guard.check_seo("x", ok_m, SITE)
    assert guard.needs_approval("delete") and not guard.needs_approval("title")
def test_drops_and_limit():
    c = db.conn(); today = dt.date(2026, 10, 25)
    for i in range(1, 17):
        d = str(today - dt.timedelta(days=i)); old = i > 9
        c.execute("INSERT INTO gsc VALUES('s',?,'https://a/p','MOBILE',?,?,?)", (d, 5 if old else 1, 40, 6 if old else 14))
        c.execute("INSERT INTO gsc VALUES('s',?,'https://a/q','MOBILE',?,?,?)", (d, 5, 40, 6))
    r = drops.detect(c, "s", today)
    assert [x["page"] for x in r] == ["https://a/p"]
    db.log_change(c, "s", "https://a/p", "title", {}, {}, "dry-run")
    assert db.recently_changed(c, "s", "https://a/p") and db.changed_today(c, "s") == 1
