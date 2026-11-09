import os, sys, types
os.environ.setdefault("SEO_DB", "/tmp/kw_test.db")
from seo_agent import keywords as K, sb

SITE = {"id": "k", "domain": "kalosa.in", "banned_terms": ["guarantee"], "competitor_names": ["acme"], "niche": "plastic surgery clinic"}
def R(q, p, c, i, pos): return {"query": q, "page": K.norm_url(p), "clicks": c, "impressions": i, "position": pos}
ROWS = [
    R("gynecomastia surgery in delhi", "https://www.kalosa.in/gynecomastia-delhi/", 2, 400, 11.2),
    R("gynecomastia cost in delhi", "https://www.kalosa.in/gynecomastia-cost/", 1, 300, 9.0),
    R("gynecomastia cost in delhi", "https://www.kalosa.in/gynecomastia-delhi/", 1, 120, 12.0),
    R("what causes gynecomastia", "https://www.kalosa.in/blog/causes/", 0, 80, 14.0),
    R("tummy tuck gurgaon", "https://www.kalosa.in/tummy-tuck/", 30, 500, 3.0),          # page 1: not striking
    R("rare thing", "https://www.kalosa.in/x/", 0, 10, 15.0),                            # too few impressions
    R("kalosa clinic phone number", "https://www.kalosa.in/", 5, 200, 9.5),
]

def test_striking_ranked_by_impr_x_gap():
    s = K.striking_distance(SITE, ROWS)
    qs = [x["query"] for x in s]
    assert "tummy tuck gurgaon" not in qs and "rare thing" not in qs
    assert qs[0] == "gynecomastia cost in delhi"      # 420 impr (two pages), CTR 0.5%: biggest impressions x gap
    assert qs[1] == "gynecomastia surgery in delhi"
    assert all(8 <= x["position"] <= 20 for x in s) and s == sorted(s, key=lambda x: -x["potential_clicks"])

def test_cannibalization_two_pages():
    c = K.cannibalization(ROWS)
    assert [x["query"] for x in c] == ["gynecomastia cost in delhi"]
    assert c[0]["primary"].endswith("/gynecomastia-cost") and len(c[0]["pages"]) == 2

def test_cannibalization_ignores_tiny_second_page():
    rows = [R("q a", "https://s/a", 1, 500, 5), R("q a", "https://s/b", 0, 5, 30)]
    assert K.cannibalization(rows) == []

def test_tracked_mismatch():
    m = K.tracked_mismatch(ROWS, [{"keyword": "gynecomastia cost in delhi", "page_url": "https://www.kalosa.in/gynecomastia-delhi/"},
                                  {"keyword": "gynecomastia surgery in delhi", "page_url": "https://www.kalosa.in/gynecomastia-delhi"}])
    assert len(m) == 1 and m[0]["query"] == "gynecomastia cost in delhi" and m[0]["ranking"].endswith("gynecomastia-cost")

def test_intent_rules():
    b = K.brand_terms(SITE)
    assert K.tag_intent("kalosa clinic phone number", b) == "Navigational"
    assert K.tag_intent("book appointment gynecomastia surgeon", b) == "Transactional"
    assert K.tag_intent("what causes gynecomastia", b) == "Informational"
    assert K.tag_intent("gynecomastia surgery cost in delhi", b) == "Commercial"

def test_clusters_group_same_topic():
    c = K.cluster_keywords(["gynecomastia cost in delhi", "gynecomastia recovery time", "what causes gynecomastia", "tummy tuck cost"])
    assert c["gynecomastia cost in delhi"].startswith("gynecomastia >") and c["gynecomastia recovery time"].startswith("gynecomastia >")
    assert c["gynecomastia cost in delhi"].endswith("cost") and c["gynecomastia recovery time"].endswith("recovery")

def test_difficulty_labels_source():
    assert K.difficulty(10) == ("Low", "estimate") and K.difficulty(19) == ("High", "estimate") and K.difficulty(None, 20) == ("Low", "ahrefs_kd")

def test_build_has_required_fields_and_no_llm_needed():
    opps = K.build(SITE, ROWS, tracked=[], gaps=[{"query": "gynecomastia pills", "volume": 100, "kd": 10}], llm={})
    srcs = {o["source"] for o in opps}
    assert {"striking_distance", "cannibalization", "competitor_gap"} <= srcs
    for o in opps:
        assert o["priority"] in ("High", "Med", "Low") and o["intent"] in K.INTENTS and o["cluster"] and o["difficulty_estimate"] in ("Low", "Med", "High")
    g = [o for o in opps if o["source"] == "competitor_gap"][0]
    assert g["page_exists"] is False and g["volume"] == 100 and g["difficulty_source"] == "ahrefs_kd"

def test_gap_dedupes_against_own_queries_and_banned():
    sb.select = lambda t, **p: [{"keyword": "gynecomastia surgery delhi", "position": 5, "volume": 50, "kd": 5},
                                {"keyword": "gynecomastia pills", "position": 9, "volume": 20, "kd": 5},
                                {"keyword": "acme gynecomastia", "position": 3, "volume": 90, "kd": 5},
                                {"keyword": "guarantee gynecomastia", "position": 3, "volume": 90, "kd": 5}]
    g = K.competitor_gaps("k", ["gynecomastia surgery in delhi"], ["guarantee"], ["acme"])
    assert [x["query"] for x in g] == ["gynecomastia pills"]

def test_feed_ideas_no_duplicates():
    store = {"keyword_opps": [{"id": 1, "keyword": "gynecomastia pills", "intent": "Informational", "volume": 20, "kd": 5},
                              {"id": 2, "keyword": "gynecomastia medicine", "intent": "Informational"},
                              {"id": 3, "keyword": "guarantee result", "intent": "Commercial"}],
             "content_ideas": [{"primary_keyword": "Gynecomastia Medicine"}]}; ins, upd = [], []
    sb.select = lambda t, **p: store[t]
    sb.insert = lambda t, rows: [dict(r, id=99) for r in rows if ins.append((t, r)) is None]
    sb.update = lambda t, m, d: upd.append((m["id"], d))
    assert K.feed_ideas(dict(SITE, niche="plastic surgery clinic")) == 1
    assert [r["primary_keyword"] for _, r in ins] == ["gynecomastia pills"] and "doctor review" in ins[0][1]["why"]
    assert {i for i, _ in upd} == {1, 2, 3} and dict(upd)[1]["status"] == "fed" and dict(upd)[2]["status"] == "fed"

def test_write_does_not_send_status():
    got = []; sb.upsert = lambda t, rows, on, chunk=1000: got.append((t, rows, on))
    K.write([{"site_id": "k", "keyword": "x", "source": "striking_distance", "device": "all"}])
    assert got[0][0] == "keyword_opps" and "status" not in got[0][1][0] and "found_at" not in got[0][1][0] and got[0][2] == "site_id,keyword,source,device"
