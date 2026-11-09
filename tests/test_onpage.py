import json, pytest
from seo_agent import onpage, run as runmod

SITE = {"id": "k", "domain": "kalosa.in", "banned_terms": ["guarantee", "FDA"], "competitor_names": ["Acme"], "profile": "medical", "reviewer_required": True}
LONG = "Gynecomastia Surgery in Delhi: Cost, Grades, Recovery and Risks Explained | Kalosa"   # 81 chars


def page(url, title, meta, h1=("H",), h2=("Causes", "Recovery"), imgs=()):
    return {"url": url, "title": title, "meta": meta, "h1": list(h1), "h2": list(h2), "images": list(imgs)}


def img(f, section="Grades"):
    return {"src": "/u/" + f, "key": f.split(".")[0], "file": f, "section": section, "caption": "", "title_attr": "", "near": "Grade 3 means more tissue."}


GOOD_META = "Learn what gynecomastia is, how doctors grade it, and what recovery looks like after surgery in Delhi."   # 100 -> short, build a valid one below
M1 = "Gynecomastia surgery in Delhi explained: grades, how the operation works, recovery steps and risks to discuss with a surgeon."


class Seq(list): pass


def fake_ask(replies):
    seen = []
    def f(system, user, max_tokens=0):
        seen.append(json.loads(user)); kind = "alt" if system is onpage.ALT_SYS else "tm"
        r = replies[kind]; r = r.pop(0) if isinstance(r, Seq) else r
        return json.dumps(r) if not isinstance(r, str) else r
    f.seen = seen; return f


def test_check_alt():
    ok = lambda a: onpage.check_alt(a, SITE) == []
    assert ok("Chest diagram showing gynecomastia grade three") and ok("Consultation room with a desk and two chairs")
    for bad in ("x", "Image of a doctor", "IMG_2034", "grade3.jpg", "Patient before and after surgery", "Best surgeon in Delhi", "Guaranteed natural look", "95% satisfied",
                "Delhi, Delhi, Delhi, Delhi, Delhi", "word " * 30, "Acme clinic reception", "Results after surgery", "Scarless result diagram"):
        assert not ok(bad), bad


def test_diagnose():
    d = lambda t, m, dup="": onpage.diagnose(page("u", t, m), dup, SITE)
    assert d("Gynecomastia Surgery in Delhi | Kalosa", M1) == []
    assert d(LONG, M1) == ["title_long"] and d("", "") == ["title_missing", "meta_missing"]
    assert "title_short" in d("Gyno", M1) and "meta_short" in d("Gynecomastia Surgery in Delhi | Kalosa", "Short meta") and "meta_long" in d("Gynecomastia Surgery in Delhi | Kalosa", M1 * 2)
    assert "duplicate_title" in d("Gynecomastia Surgery in Delhi | Kalosa", M1, "https://k.in/other")
    r = d("Best Hair Transplant in Delhi | Kalosa", "Starting from Rs 45,000. 100% safe result for you and your hair, book today at the clinic in Delhi.")
    assert "risky_copy" in r
    assert "risky_copy" in d("Microneedling Cost in India | Kalosa", "Price factors and trusted clinics offering safe and affordable microneedling for a youthful look at our clinic.")


def test_h1_finding():
    assert onpage.h1_finding(page("u", "t", "m", h1=["a"])) is None
    assert onpage.h1_finding(page("u", "t", "m", h1=[]))["issue"] == "h1_missing"
    f = onpage.h1_finding(page("u", "t", "m", h1=["a", "b"])); assert f["issue"] == "h1_count_2" and f["found"] == ["a", "b"]


RAW = """<html><head><title>  Gyno &amp; Care | Kalosa </title><meta name="description" content="Meta &amp; more">{extra}</head><body>
<header><img src="/logo.png" alt=""></header><main><h1>Main</h1><h2>Grades</h2>
<img src="/u/grade-3-300x200.jpg" alt=""><img data-src="/u/room.webp" src="data:image/gif;base64,AA">
<img src="/u/has.jpg" alt="Has alt"><img src="/u/grade-3.jpg" alt=""><img src="/u/pixel.gif" width="1" height="1" alt="">
<img src="/u/deco.png" alt="" role="presentation"><figure><img src="/u/fig.jpg"><figcaption>Our consultation room</figcaption></figure>
<h2>Recovery</h2><p>Text</p></main><footer><img src="/u/foot.jpg" alt=""></footer></body></html>"""


def test_parse_page():
    p = onpage.parse_page(RAW.format(extra=""), "https://k.in/a")
    assert p["title"] == "Gyno & Care | Kalosa" and p["meta"] == "Meta & more" and p["h1"] == ["Main"] and p["h2"] == ["Grades", "Recovery"]
    assert [i["key"] for i in p["images"]] == ["grade-3", "room", "fig"]          # header/footer, tracking pixel, decorative, has-alt, duplicate, data: placeholder skipped
    assert p["images"][0]["section"] == "Grades" and p["images"][2]["caption"] == "Our consultation room"
    assert onpage.parse_page(RAW.format(extra='<meta name="robots" content="noindex,follow">'), "https://k.in/a") is None
    assert onpage.parse_page(RAW.format(extra='<link rel="canonical" href="https://k.in/other">'), "https://k.in/a") is None
    assert onpage.parse_page(RAW.format(extra='<link rel="canonical" href="https://www.k.in/a/">'), "https://k.in/a")


def audits(*urls): return [{"page": u, "post_id": i + 1, "h1": 1, "images_no_alt": 0} for i, u in enumerate(urls)]


def run_build(pages, ask, limit=20, existing=(), imp=None, site=SITE):
    return onpage.build("k", site, audits(*pages), lambda u: pages[u], ask, imp or {}, existing, limit)


NEW_TM = {"title": "Gynecomastia Surgery in Delhi | Kalosa", "meta": M1, "why": "Shorter title; meta lists what the h2s cover."}
NEW_ALT = [{"i": 0, "alt": "Chest diagram showing gynecomastia grade three"}, {"i": 1, "alt": "", "skip": True}]


def test_build_makes_pending_onpage_proposals():
    pages = {"https://k.in/a": page("https://k.in/a", LONG, M1, imgs=[img("g3.jpg"), img("x.jpg")]), "https://k.in/ok": page("https://k.in/ok", "Fine Title For A Page | Kalosa", M1)}
    ask = fake_ask({"tm": NEW_TM, "alt": NEW_ALT}); props, finds, notes = run_build(pages, ask)
    assert [(p["kind"], p["page"]) for p in props] == [("title_meta", "https://k.in/a"), ("alt_text", "https://k.in/a")] and finds == []
    tm, alt = props
    for p in props: assert p["status"] == "pending" and p["source"] == "onpage" and p["needs_approval"] and p["post_id"] == 1 and "DOCTOR TO CONFIRM" in p["reason"] and p["new"]["review"] == "DOCTOR TO CONFIRM"
    assert tm["old"] == {"title": LONG, "meta": M1} and tm["new"]["title"] == NEW_TM["title"] and tm["new"]["issues"] == ["title_long"]
    assert alt["new"]["images"] == [{"src": "/u/g3.jpg", "alt": "Chest diagram showing gynecomastia grade three", "section": "Grades"}] and "not looked at" in alt["new"]["basis"]
    assert alt["old"]["images"] == [{"src": "/u/g3.jpg", "alt": ""}] and "1 of 2" in alt["reason"]
    assert ask.seen[0]["medical_site"] is True and ask.seen[0]["brand"] == "Kalosa"


def test_non_medical_site_has_no_review_flag():
    s = {**SITE, "profile": "general", "reviewer_required": False}
    props, *_ = run_build({"https://k.in/a": page("https://k.in/a", LONG, M1)}, fake_ask({"tm": NEW_TM}), site=s)
    assert "review" not in props[0]["new"] and "DOCTOR" not in props[0]["reason"]


def test_limit_caps_proposals_and_most_visited_page_goes_first():
    pages = {f"https://k.in/p{i}": page(f"https://k.in/p{i}", LONG + str(i), M1) for i in range(5)}
    def ask(system, user, max_tokens=0):
        n = json.loads(user)["url"][-1]; return json.dumps({"title": f"Gynecomastia Surgery Delhi Guide {n} | Kalosa", "meta": M1, "why": "w"})
    props, *_ = run_build(pages, ask, limit=2, imp={"https://k.in/p3": 900, "https://k.in/p1": 500})
    assert [p["page"] for p in props] == ["https://k.in/p3", "https://k.in/p1"]
    assert len(run_build(pages, ask, limit=500)[0]) == 5 and len(run_build(pages, ask, limit=0)[0]) == 5   # limit 0 means default; hard cap exists
    assert onpage.HARD_LIMIT == 60


def test_duplicate_titles_leader_keeps_title():
    t = "Gynecomastia Surgery in Delhi | Kalosa"
    pages = {"https://k.in/a": page("https://k.in/a", t, M1), "https://k.in/b": page("https://k.in/b", t, M1)}
    ask = fake_ask({"tm": {"title": "Gynecomastia Grades in Delhi Explained | Kalosa", "meta": M1, "why": "w"}})
    props, *_ = run_build(pages, ask, imp={"https://k.in/b": 50})
    assert [p["page"] for p in props] == ["https://k.in/a"] and props[0]["new"]["issues"] == ["duplicate_title"]   # b has the traffic, so b keeps the title


def test_bad_claude_output_is_retried_then_dropped():
    pages = {"https://k.in/a": page("https://k.in/a", LONG, M1)}
    bad = {"title": "Best Gynecomastia Surgery in Delhi | Kalosa", "meta": M1, "why": "w"}
    ask = fake_ask({"tm": Seq([bad, bad, bad])}); props, _, notes = run_build(pages, ask)
    assert props == [] and "no safe title/meta" in notes[0] and len(ask.seen) == 3 and "best" in ask.seen[1]["previous_errors"].lower()
    ask = fake_ask({"tm": Seq([bad, NEW_TM])}); props, *_ = run_build(pages, ask); assert len(props) == 1 and len(ask.seen) == 2
    for evil in ({"title": "Gynecomastia Surgery in Gurgaon | Kalosa", "meta": M1}, {"title": "Gynecomastia Surgery | Kalosa Clinic", "meta": M1},
                 {"title": "Gynecomastia Surgery in Delhi | Acme", "meta": M1}, {"title": "Gynecomastia Surgery in Delhi | Kalosa", "meta": M1 + " Guaranteed."},
                 {"title": "Gynecomastia Surgery in Delhi | Kalosa", "meta": "Too short."}):
        assert run_build(pages, fake_ask({"tm": Seq([{**evil, "why": "w"}] * 3)}))[0] == [], evil


def test_alt_rejects_bad_or_duplicate_alts_and_asks_again():
    pages = {"https://k.in/a": page("https://k.in/a", "Fine Title For A Page | Kalosa", M1, imgs=[img("a.jpg"), img("b.jpg"), img("c.jpg")])}
    r1 = [{"i": 0, "alt": "Doctor and patient"}, {"i": 1, "alt": "Doctor and patient"}, {"i": 2, "alt": "Patient before and after surgery"}]
    r2 = [{"i": 1, "alt": "Surgeon explaining a diagram"}, {"i": 2, "alt": "Waiting area with sofa"}]
    ask = fake_ask({"alt": Seq([r1, r2])}); props, *_ = run_build(pages, ask)
    got = {i["src"]: i["alt"] for i in props[0]["new"]["images"]}
    assert got == {"/u/a.jpg": "Doctor and patient", "/u/b.jpg": "Surgeon explaining a diagram", "/u/c.jpg": "Waiting area with sofa"}
    assert "same alt" in ask.seen[1]["problems_in_last_try"] and [x["i"] for x in ask.seen[1]["images"]] == [1, 2]


def test_existing_proposals_are_not_repeated_and_h1_findings():
    pages = {"https://k.in/a": page("https://k.in/a", LONG, M1, h1=[], imgs=[img("a.jpg")]), "https://k.in/b": page("https://k.in/b", "Fine Title For A Page | Kalosa", M1, h1=["x", "y"])}
    ask = fake_ask({"tm": NEW_TM, "alt": [{"i": 0, "alt": "Waiting area with sofa"}]})
    props, finds, _ = run_build(pages, ask, existing=[{"page": "https://www.k.in/a/", "kind": "title_meta"}])
    assert [p["kind"] for p in props] == ["alt_text"]
    assert [(f["page"], f["issue"], f["status"]) for f in finds] == [("https://k.in/a", "h1_missing", "open"), ("https://k.in/b", "h1_count_2", "open")]


def test_unusable_pages_are_skipped():
    a = audits("https://k.in/a", "https://k.in/x.pdf", "https://k.in/s?q=1", "https://k.in/gone")
    seen = []
    def fetcher(u): seen.append(u); return None if u.endswith("gone") else page(u, "Fine Title For A Page | Kalosa", M1)
    props, _, notes = onpage.build("k", SITE, a, fetcher, fake_ask({}), {}, (), 5)
    assert seen == ["https://k.in/a", "https://k.in/gone"] and props == [] and "gone" in notes[0]


# ---- run() wiring: nothing is applied, only pending rows are written
def test_run_writes_pending_rows_only(monkeypatch):
    ins, upd = [], []
    audits_rows = [{"page": "https://k.in/a", "post_id": 7, "audited_at": "2026-11-02", "h1": 0, "images_no_alt": 1}, {"page": "https://k.in/a/", "post_id": 7, "audited_at": "2026-10-01", "h1": 1, "images_no_alt": 0}]
    def select(t, **p):
        if t == "sites": return [{"id": "k", "mode": "dry-run", "profile": "medical", "reviewer_required": True}]
        if t == "page_audits": return audits_rows
        if t == "gsc_daily": return [{"page": "https://k.in/a", "impressions": 10}]
        return []
    monkeypatch.setattr(onpage.sb, "select", select); monkeypatch.setattr(onpage.sb, "insert", lambda t, rows: ins.append((t, rows)) or rows)
    monkeypatch.setattr(onpage.sb, "update", lambda *a: upd.append(a))
    monkeypatch.setattr(runmod, "sites", lambda: {"k": {"id": "k", "domain": "k.in", "banned_terms": [], "competitor_names": []}})
    monkeypatch.setattr(onpage, "fetch_page", lambda u: page(u, LONG, M1, h1=[], imgs=[img("g3.jpg")])); monkeypatch.setattr(onpage, "FETCH_PAUSE", 0)
    monkeypatch.setattr(onpage, "ask", fake_ask({"tm": NEW_TM, "alt": [{"i": 0, "alt": "Waiting area with sofa"}]}))
    monkeypatch.delenv("GSC_SERVICE_ACCOUNT_JSON", raising=False)
    onpage.run("k", 5)
    props = [r for t, rows in ins if t == "proposals" for r in rows]; finds = [r for t, rows in ins if t == "onpage_findings" for r in rows]
    assert [p["kind"] for p in props] == ["title_meta", "alt_text"] and all(p["status"] == "pending" and p["source"] == "onpage" for p in props)
    assert [f["issue"] for f in finds] == ["h1_missing"] and upd == []                  # latest audit per page only; nothing applied


def test_run_paused_or_no_audits(monkeypatch, capsys):
    monkeypatch.setattr(runmod, "sites", lambda: {"k": {"id": "k"}}); monkeypatch.setattr(onpage.sb, "insert", lambda *a: pytest.fail("must not write"))
    monkeypatch.setattr(onpage.sb, "select", lambda t, **p: [{"mode": "paused"}] if t == "sites" else [])
    onpage.run("k"); assert "paused" in capsys.readouterr().out
    monkeypatch.setattr(onpage.sb, "select", lambda t, **p: [{"mode": "dry-run"}] if t == "sites" else [])
    onpage.run("k"); assert "audit-all" in capsys.readouterr().out


def test_select_all_pages_through_results(monkeypatch):
    data = list(range(2300)); monkeypatch.setattr(onpage.sb, "select", lambda t, **p: data[int(p["offset"]): int(p["offset"]) + int(p["limit"])])
    assert onpage.select_all("x") == data and len(onpage.select_all("x", max_rows=1500)) == 2000   # whole pages of 1000, stops once max_rows reached
