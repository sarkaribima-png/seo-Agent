import datetime as dt, os, sys, types
import pytest
os.environ["LLM_DISABLED"] = "1"
from seo_agent import publisher as P, sb, content, run as R

UTC = dt.timezone.utc
def at(y, m, d, h, mi=0): return dt.datetime(y, m, d, h, mi, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))).astimezone(UTC)   # IST wall clock
GOOD = "<h2>Tummy tuck recovery</h2>" + "<p>The cut heals in weeks. Rest is needed. Walk each day. Ask your doctor.</p>" * 40


class Store:
    def __init__(self): self.t = {k: [] for k in ("sites", "content_posts", "content_ideas", "proposals", "publish_log", "publisher_tasks")}; self.n = 0
    def select(self, t, **p):
        rows = self.t[t]
        for k, v in p.items():
            if k in ("select", "order", "limit"): continue
            op, val = v.split(".", 1)
            f = {"eq": lambda a: str(a) == val, "gte": lambda a: a is not None and str(a) >= val, "lt": lambda a: a is not None and str(a) < val}[op]
            rows = [r for r in rows if f(r.get(k))]
        return [dict(r) for r in rows]
    def insert(self, t, rows):
        out = []
        for r in rows:
            self.n += 1; r = {"id": self.n, **r}; self.t[t].append(r); out.append(dict(r))
        return out
    def update(self, t, m, d):
        for r in self.t[t]:
            if all(r.get(k) == v for k, v in m.items()): r.update(d)


@pytest.fixture
def env(monkeypatch):
    s = Store(); published = []
    for n in ("select", "insert", "update"): monkeypatch.setattr(sb, n, getattr(s, n))
    monkeypatch.setattr(sb, "upsert", lambda *a, **k: None)
    def publish_one(site, w, p):
        url = f"https://k.in/{p['slug']}/"; published.append(p["id"])
        s.update("content_posts", {"id": p["id"]}, {"status": "published", "published_at": P.NOW.isoformat(), "publish_url": url}); return url
    monkeypatch.setattr(content, "publish_one", publish_one, raising=False)
    monkeypatch.setattr(content, "TARGET_GRADE", 99.0)
    fake_gsc = types.ModuleType("seo_agent.gsc"); monkeypatch.setitem(sys.modules, "seo_agent.gsc", fake_gsc); monkeypatch.setattr("seo_agent.gsc", fake_gsc, raising=False)
    s.site = {"id": "k", "domain": "k.in", "wp_base": "https://k.in", "mode": "live", "profile": "default", "reviewer_required": False, "posts_per_day": 1, "banned_terms": [], "competitor_names": []}
    s.published = published; return s


def post(s, i, **kw):
    d = {"site_id": "k", "title": f"Post {i}", "slug": f"post-{i}", "html": GOOD, "status": "approved", "approved_at": f"2026-11-0{i}T00:00:00+00:00", "target_words": None, "reviewed_at": None, "issues": []}
    d.update(kw); return s.insert("content_posts", [d])[0]
def go(s, t, get=None):
    P.NOW = t
    sm = "<urlset><url><loc>https://k.in/post-1/</loc></url><url><loc>https://k.in/post-2/</loc></url><url><loc>https://k.in/post-3/</loc></url></urlset>"
    get = get or (lambda u: {"status": 200, "text": sm, "headers": {}})
    P.publish_site(s.site, types.SimpleNamespace(), content, t, get)


def test_cap_one_per_day_and_window(env):
    post(env, 1); post(env, 2)
    go(env, at(2026, 11, 10, 8))                      # before the 9:00 IST window
    assert env.published == []
    go(env, at(2026, 11, 10, 10)); assert env.published == [1]
    go(env, at(2026, 11, 10, 15)); assert env.published == [1]       # cap 1 reached
    go(env, at(2026, 11, 11, 10)); assert env.published == [1, 2]    # next IST day
    go(env, at(2026, 11, 11, 22)); assert env.published == [1, 2]    # after window and capped


def test_cap_respects_ist_midnight_not_utc(env):
    # 2026-11-10 23:50 IST = 18:20 UTC (same UTC day as 00:10 IST on 11-11 = 18:40 UTC on 11-10). A UTC-day cap would block the second one.
    env.site.update(publish_window_start=0, publish_window_end=24)
    post(env, 1); post(env, 2)
    go(env, at(2026, 11, 10, 23, 50)); assert env.published == [1]
    go(env, at(2026, 11, 11, 0, 10)); assert env.published == [1, 2]       # new IST day although only 20 min later and same UTC date
    assert [r["cap_day"] for r in env.t["publish_log"]] == ["2026-11-10", "2026-11-11"]
    post(env, 3); go(env, at(2026, 11, 11, 0, 40)); assert env.published == [1, 2]   # cap of the 11th is used


def test_cap_is_clamped_and_spaced(env, monkeypatch):
    monkeypatch.setattr(P, "MAX_CAP", 2); env.site["posts_per_day"] = 10
    assert P.cap_for(env.site) == 2 and P.cap_for({"posts_per_day": 0}) == 0
    for i in (1, 2, 3): post(env, i)
    go(env, at(2026, 11, 10, 9, 30)); go(env, at(2026, 11, 10, 10, 30))   # gap = 12h/2 = 6h
    assert env.published == [1]
    go(env, at(2026, 11, 10, 15, 40)); assert env.published == [1, 2]
    go(env, at(2026, 11, 10, 20)); assert env.published == [1, 2]       # cap 2


def test_medical_needs_reviewer(env):
    env.site.update(profile="medical", reviewer_required=True)
    p = post(env, 1)
    go(env, at(2026, 11, 10, 10)); assert env.published == []
    assert env.t["content_posts"][0]["status"] == "approved"             # waits, not sent back
    env.update("content_posts", {"id": p["id"]}, {"reviewed_at": "2026-11-10T01:00:00+00:00"})
    go(env, at(2026, 11, 10, 11)); assert env.published == [1]
    # a non-medical site without reviewer_required does not need one
    assert P.gates_for({"profile": "default"}, {"status": "approved", "html": GOOD, "title": "t"}, content)["reviewer"]["ok"]


def test_medical_flag_alone_needs_reviewer(env):
    env.site.update(profile="medical", reviewer_required=False); post(env, 1)
    go(env, at(2026, 11, 10, 10)); assert env.published == []


def test_gate_failure_blocks_and_sends_back(env):
    post(env, 1, html=GOOD + "<p>It is crucial to rest.</p>"); post(env, 2, status="draft")
    go(env, at(2026, 11, 10, 10))
    assert env.published == []
    r = env.t["content_posts"][0]; assert r["status"] == "draft" and any("publisher gate humanizer_lint" in i for i in r["issues"])
    n = len(env.t["content_posts"][0]["issues"]); go(env, at(2026, 11, 10, 11)); assert len(env.t["content_posts"][0]["issues"]) == n   # draft is not touched again


def test_gate_failure_then_next_good_post_publishes(env):
    post(env, 1, html="<p>too short</p>", target_words=1200); post(env, 2)
    go(env, at(2026, 11, 10, 10)); assert env.published == [2]
    assert env.t["content_posts"][0]["status"] == "draft"


def test_word_range_competitor_and_ranking_gates(env):
    env.site["competitor_names"] = ["Acme Clinic"]
    g = P.gates_for(env.site, {"status": "approved", "title": "t", "html": GOOD + "<p>Better than Acme Clinic.</p>"}, content); assert not g["claims"]["ok"]
    g = P.gates_for(env.site, {"status": "approved", "title": "t", "html": GOOD + "<p>We will rank number one.</p>"}, content); assert not g["claims"]["ok"]
    g = P.gates_for(env.site, {"status": "approved", "title": "t", "html": GOOD, "target_words": 100}, content); assert not g["word_count"]["ok"]
    assert P.failed(P.gates_for(env.site, {"status": "approved", "title": "t", "html": GOOD}, content)) == {}


def test_idempotent_rerun(env):
    post(env, 1); t = at(2026, 11, 10, 10)
    go(env, t); go(env, t); go(env, t)
    assert env.published == [1] and len(env.t["publish_log"]) == 1
    n = len(env.t["publisher_tasks"]); P.reconcile(env.site, content, t, lambda u: {"status": 200, "text": "<loc>https://k.in/post-1/</loc>", "headers": {}}, lambda u: None)
    assert len(env.t["publish_log"]) == 1 and len(env.t["publisher_tasks"]) == n


def test_reconcile_logs_unlogged_publish(env):
    p = post(env, 1, status="published", published_at=at(2026, 11, 10, 3).isoformat(), publish_url="https://k.in/post-1/")
    P.reconcile(env.site, content, at(2026, 11, 10, 10), lambda u: {"status": 200, "text": "<loc>https://k.in/post-1/</loc>", "headers": {}}, lambda u: None)
    P.reconcile(env.site, content, at(2026, 11, 10, 10), lambda u: {"status": 200, "text": "", "headers": {}}, lambda u: None)
    assert len(env.t["publish_log"]) == 1 and env.t["publish_log"][0]["cap_day"] == "2026-11-10"


def test_after_publish_tasks_and_sitemap(env):
    post(env, 1); go(env, at(2026, 11, 10, 10))
    row = env.t["publish_log"][0]; assert row["sitemap"]["listed"] is True and row["index_status"] == "pending"
    kinds = {t["kind"] for t in env.t["publisher_tasks"]}; assert kinds == {"request_indexing"}      # gsc has no submit_sitemap
    assert env.t["publisher_tasks"][0]["assignee"] == "owner"
    post(env, 4, slug="post-4"); go(env, at(2026, 11, 11, 10))                                        # post-4 is not in the sitemap fixture
    assert "sitemap_missing" in {t["kind"] for t in env.t["publisher_tasks"]}


def test_gsc_submit_used_when_available(env, monkeypatch):
    sent = []; sys.modules["seo_agent.gsc"].submit_sitemap = lambda site, u: sent.append(u)
    post(env, 1); go(env, at(2026, 11, 10, 10))
    assert sent and not any(t["kind"] == "request_indexing" for t in env.t["publisher_tasks"])


def test_internal_links_in_and_out(env):
    env.insert("content_ideas", [{"id": 1, "site_id": "k", "primary_keyword": "tummy tuck recovery"}, {"id": 2, "site_id": "k", "primary_keyword": "liposuction cost"}])
    old = post(env, 3, status="published", slug="old", idea_id=2, publish_url="https://k.in/old/", wp_post_id=77, published_at="2026-11-01T00:00:00+00:00",
               html="<p>Many ask about tummy tuck recovery after surgery.</p>")
    post(env, 1, idea_id=1, html=GOOD + "<p>Compare the liposuction cost with other options.</p>")
    go(env, at(2026, 11, 10, 10))
    props = env.t["proposals"]; pairs = {(x["page"], x["new"]["url"], x["new"]["anchor"]) for x in props}
    assert ("https://k.in/old/", "https://k.in/post-1/", "tummy tuck recovery") in pairs      # in
    assert ("https://k.in/post-1/", "https://k.in/old/", "liposuction cost") in pairs            # out
    assert all(x["status"] == "pending" and x["needs_approval"] and x["source"] == "publisher" for x in props)
    n = len(props); P.link_proposals(env.site, env.t["content_posts"][1] if env.t["content_posts"][1]["id"] != old["id"] else env.t["content_posts"][0], "https://k.in/post-1/")
    assert len(env.t["proposals"]) == n                                                           # no duplicates


def test_link_pairs_never_invents_and_skips_existing():
    assert P.link_pairs("nothing here", "u", [{"url": "t", "phrases": ["tummy tuck"]}], set()) == []
    assert P.link_pairs("a tummy tuck plan", "u", [{"url": "t", "phrases": ["tummy tuck"]}], {"t"}) == []
    assert P.link_pairs("a tummy tucks plan", "u", [{"url": "t", "phrases": ["tummy tuck"]}], set()) == []   # word boundary
    assert P.internal_links('<a href="/x">a</a><a href="https://www.k.in/y">b</a><a href="https://z.com">c</a>', "https://k.in/p/") == ["/x", "https://www.k.in/y"]


def mk_log(env, days_ago, now, status="pending", ck=None, url="https://k.in/post-1/"):
    env.insert("publish_log", [{"site_id": "k", "url": url, "post_id": 1, "published_at": (now - dt.timedelta(days=days_ago, hours=1)).isoformat(), "cap_day": "2026-11-01", "gates": {}, "index_status": status, "checked_at": ck or {}}])


OK = lambda u: {"status": 200, "text": "<link rel='canonical' href='https://k.in/post-1/'>", "headers": {}}
def test_index_check_no_gsc_is_data_not_available(env):
    now = at(2026, 11, 10, 10); mk_log(env, 1, now)
    P.check_index(env.site, now, OK, lambda s, u: None)
    r = env.t["publish_log"][0]; assert r["index_status"] == "DATA NOT AVAILABLE" and list(r["checked_at"]) == ["d1"] and r["checked_at"]["d1"]["gsc"] == "DATA NOT AVAILABLE"
    P.check_index(env.site, now, OK, lambda s, u: None); assert env.t["publisher_tasks"] == []       # idempotent, no task before day 7


def test_day7_not_indexed_creates_strategist_task_once(env):
    now = at(2026, 11, 10, 10); mk_log(env, 7, now, ck={"d1": {}, "d3": {}})
    ins = lambda s, u: {"verdict": "NEUTRAL", "coverage": "Crawled - currently not indexed"}
    P.check_index(env.site, now, OK, ins); P.check_index(env.site, now, OK, ins)
    r = env.t["publish_log"][0]; assert r["index_status"] == "not_indexed" and "d7" in r["checked_at"]
    t = env.t["publisher_tasks"]; assert len(t) == 1 and t[0]["kind"] == "not_indexed" and t[0]["assignee"] == "strategist" and t[0]["detail"]


def test_indexed_stops_checks_and_noindex_is_blocked(env):
    now = at(2026, 11, 10, 10); mk_log(env, 3, now)
    P.check_index(env.site, now, OK, lambda s, u: {"verdict": "PASS", "coverage": "Submitted and indexed"})
    assert env.t["publish_log"][0]["index_status"] == "indexed" and set(env.t["publish_log"][0]["checked_at"]) == {"d1", "d3"}
    env.t["publish_log"].clear(); mk_log(env, 3, now, url="https://k.in/b/")
    P.check_index(env.site, now, lambda u: {"status": 200, "text": "<meta name='robots' content='noindex,follow'>", "headers": {}}, lambda s, u: None)
    assert env.t["publish_log"][0]["index_status"] == "blocked" and env.t["publisher_tasks"][0]["kind"] == "blocked"


def test_day7_unknown_makes_owner_task_and_gsc_error_is_not_data(env):
    now = at(2026, 11, 10, 10); mk_log(env, 8, now)
    P.check_index(env.site, now, OK, lambda s, u: {"error": "403"})
    assert env.t["publish_log"][0]["index_status"] == "DATA NOT AVAILABLE" and env.t["publisher_tasks"][0]["assignee"] == "owner"


def test_sitemap_lists_follows_index(env):
    pages = {"https://k.in/sitemap.xml": "<loc>https://k.in/wp-sitemap-posts.xml</loc>", "https://k.in/wp-sitemap-posts.xml": "<loc>https://k.in/a/</loc>"}
    g = lambda u: {"status": 200, "text": pages[u], "headers": {}} if u in pages else {"status": 404, "text": ""}
    assert P.sitemap_lists("https://k.in", "https://k.in/a", g)["listed"] is True
    assert P.sitemap_lists("https://k.in", "https://k.in/zzz", g)["listed"] is False
    assert P.sitemap_lists("https://k.in", "https://k.in/a", lambda u: {"status": 404, "text": ""})["listed"] is None


def test_advice_uses_llm_when_available_else_fallback(env, monkeypatch):
    assert "GSC" in P.advice(env.site, "u", "DATA NOT AVAILABLE", {})
    from seo_agent import llm; monkeypatch.setattr(llm, "ask", lambda *a, **k: "1. Add links.")
    assert P.advice(env.site, "u", "not_indexed", {}) == "1. Add links."
