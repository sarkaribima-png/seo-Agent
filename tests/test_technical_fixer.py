import json, re, pytest
from seo_agent import technical_fixer as T, llm, sb, run as runmod
from seo_agent.static import Static, revert_alt

B = "https://ex.test"
SITE = {"id": "s1", "domain": "ex.test", "banned_terms": ["guarantee"], "competitor_names": ["Acme"], "tech_auto_safe": True}
MED = {**SITE, "profile": "medical"}
REV = {**SITE, "reviewer_required": True}
P1 = "Our clinic explains how a consultation works, what to bring, and how long each visit usually takes for new patients."

def html(title="Gynecomastia Surgery in Delhi: Grades, Recovery and Questions | Ex", meta="", head="", body=None, vp=True, can=None):
    m = f'<meta name="description" content="{meta}">' if meta else ""
    return (f"<html><head><title>{title}</title>{m}" + (VIEWPORT if vp else "") + (f'<link rel="canonical" href="{can}">' if can else "") + head
            + f"</head><body><h1>Main topic here</h1><p>{body or P1}</p></body></html>")
VIEWPORT = '<meta name="viewport" content="width=device-width">'
def ctx_of(h, url=B + "/a/", codes=None):
    class R: status_code = 200; text = h; headers = {}
    c = T.page_ctx(url, lambda u: R)
    if codes: c["codes"] = codes
    return c
def issue(code, url=B + "/a/", i=1, detail="d", sev="Medium"): return {"id": i, "code": code, "url": url, "issue": code, "severity": sev, "effort": "Low", "detail": detail, "fix": "do it", "why": "because", "found_at": "2026-11-01T00:00:00+00:00"}
def no_llm(*a, **k): raise llm.LLMUnavailable("off")


# ---- classification
def test_classification():
    for c in ("title_missing", "title_long", "meta_missing", "meta_long", "alt_missing", "viewport_missing", "canonical_missing", "canonical_relative", "canonical_http", "schema_invalid", "robots_no_sitemap", "mixed_content"):
        assert T.classify(c) == "SAFE", c
    for c in ("redirect_chain", "redirect_temp", "redirect_loop", "http_no_redirect", "host_variants", "noindex", "canonical_other_domain", "canonical_other_url", "canonical_multiple",
              "robots_blocks_all", "robots_blocks_assets", "robots_missing", "status_4xx", "schema_review", "brand_new_code"):
        assert T.classify(c) == "RISKY", c
    assert T.plan("redirect_chain") == ("RISKY", "manual") and T.plan("viewport_missing") == ("SAFE", "viewport_add") and T.plan("cwv_lcp") is None and T.plan("brand_new_code") is None
    assert T.kind_of("title_meta") == "title_meta" and T.kind_of("alt_text") == "alt_text" and T.kind_of("canonical_set") == "tech_fix"


# ---- rule fallback (no Claude)
def gen(code, h, site=SITE, askf=no_llm, codes=None, url=B + "/a/", fetcher=None):
    c = ctx_of(h, url, codes); return T.generate(issue(code, url), c, site, askf, fetcher)

def test_rule_viewport_canonical():
    f, n = gen("viewport_missing", html(vp=False)); assert f["source"] == "rule" and f["confidence"] == "High" and f["after"] == T.VIEWPORT and f["risk"] == "SAFE"
    f, _ = gen("canonical_missing", html()); assert f["after"] == f'<link rel="canonical" href="{B}/a/">' and f["before"] == ""
    f, _ = gen("canonical_relative", html(can="/a/")); assert f["before"] == '<link rel="canonical" href="/a/">' and B + "/a/" in f["after"]
    f, _ = gen("canonical_http", html(can="http://ex.test/a/")); assert 'href="https://ex.test/a/"' in f["after"]
    f, _ = gen("canonical_other_domain", html(can="https://other.test/a/")); assert f["risk"] == "RISKY" and f["after"].count("ex.test/a/") == 1

def test_rule_schema_mixed_noindex():
    bad = '{"@context": "https://schema.org", "@type": "Organization", "name": "Ex Clinic", "url": "https://ex.test/",}'
    f, _ = gen("schema_invalid", html(head=f'<script type="application/ld+json">{bad}</script>')); assert f and json.loads(f["after"])["name"] == "Ex Clinic"
    f, n = gen("schema_invalid", html(head='<script type="application/ld+json">{not json at all</script>')); assert f is None and any("no safe rule" in x for x in n)
    f, _ = gen("mixed_content", html(head='<script src="http://ex.test/a.js"></script><img src="http://www.ex.test/i.png"><script src="http://cdn.other/x.js"></script>')); assert f["before"] == "http://ex.test" and f["after"] == "https://ex.test"
    f, _ = gen("noindex", html(head='<meta name="robots" content="noindex,follow">')); assert f["risk"] == "RISKY" and f["after"] == "" and "noindex" in f["before"]
    assert gen("noindex", html())[0] is None

def test_rule_robots():
    class R:
        def __init__(s, t, c=200): s.text, s.status_code, s.headers = t, c, {}
    fe = lambda u: R("<urlset><url><loc>x</loc></url></urlset>") if u.startswith(B + "/sitemap.xml") else R("")
    f, _ = gen("robots_no_sitemap", "User-agent: *\nDisallow:\n", url=B + "/robots.txt", fetcher=fe); assert f["after"] == f"Sitemap: {B}/sitemap.xml"
    assert gen("robots_no_sitemap", "User-agent: *\n", url=B + "/robots.txt", fetcher=lambda u: R("", 404))[0] is None      # no sitemap -> no line
    f, _ = gen("robots_blocks_all", "User-agent: *\nDisallow: /\n", url=B + "/robots.txt"); assert f["before"] == "Disallow: /" and f["risk"] == "RISKY" and f["confidence"] == "Medium"
    new, pairs = T.build_edit("robots_unblock", "User-agent: *\nDisallow: /\nSitemap: x\n", f); assert "Disallow: /" not in new.splitlines() and "# sab:removed Disallow: /" in new and T.verify_text("robots_unblock", f, new)

def test_rule_title_meta():
    long_t = "Gynecomastia Surgery in Delhi: Cost, Grades, Recovery and Risks Explained | Ex Clinic"
    f, _ = gen("title_long", html(title=long_t, meta="m" * 100), codes=["title_long"]); t = json.loads(f["after"])
    assert 15 <= len(t["title"]) <= 60 and t["title"].endswith("| Ex Clinic") and not re.search(r"\b(and|or|of)\s*\|", t["title"])
    assert json.loads(f["after"])["meta"] == "m" * 100 and f["confidence"] == "Medium"        # meta untouched: only the title had an issue
    f, _ = gen("meta_missing", html(), codes=["meta_missing"]); m = json.loads(f["after"])
    assert 70 <= len(m["meta"]) <= 155 and m["meta"].startswith("Our clinic explains") and m["title"] == json.loads(f["before"])["title"]
    f, n = gen("meta_missing", html(body="Short."), codes=["meta_missing"]); assert f is None                  # not enough page text: no invented meta
    f, n = gen("meta_missing", html(body="We guarantee the best result for every single patient who walks into our clinic today."), codes=["meta_missing"]); assert f is None and any("rejected" in x for x in n)
    assert len(T.shorten_title("A very long page title about something useful for people | Brand", 40)) <= 40

def test_title_and_meta_issue_one_fix():
    f, _ = gen("title_long", html(title="T" * 20 + " " + "word " * 20, meta="m " * 120), codes=["title_long", "meta_long"])
    a = json.loads(f["after"]); assert len(a["title"]) <= 60 and len(a["meta"]) <= 155

def test_rule_alt():
    h = '<html><head><title>x</title></head><body><img src="/u/grade-3-chest-diagram-300x200.jpg" alt=""><img src="/u/IMG_2034.jpg"><img src="/u/ok.jpg" alt="Has alt"></body></html>'
    f, _ = gen("alt_missing", h); a = json.loads(f["after"]); assert a == [{"src": "/u/grade-3-chest-diagram-300x200.jpg", "alt": "Grade 3 chest diagram"}] and f["confidence"] == "Low"


# ---- Claude path, validation, parse failure
GOOD = lambda **kw: {"fix_type": "viewport_add", "target_url": B + "/a/", "before": "", "after": T.VIEWPORT, "rationale": "r", "confidence": "High", "evidence": "page head has no viewport", **kw}
def ask_with(reply): return lambda s, u, **k: reply

def test_llm_accepted_and_json_prompt():
    seen = []
    def a(s, u, **k): seen.append((s, json.loads(u), k)); return GOOD()
    f, _ = gen("viewport_missing", html(vp=False), askf=a)
    assert f["source"] == "llm" and seen[0][2]["agent"] == "technical_fixer" and seen[0][1]["required_fix_type"] == "viewport_add" and "DATA NOT AVAILABLE" in seen[0][0]

@pytest.mark.parametrize("bad", [GOOD(fix_type="redirect_add"), GOOD(target_url=B + "/other/"), GOOD(confidence="Sure"), GOOD(evidence=""), GOOD(after='<meta name="viewport" content="x">'),
                                 {"fix_type": "viewport_add"}, "just text", ["a"]])
def test_llm_invalid_falls_back_to_rule(bad):
    f, n = gen("viewport_missing", html(vp=False), askf=ask_with(bad)); assert f["source"] == "rule" and f["after"] == T.VIEWPORT and n

def test_llm_cannot_make_unsafe_canonical_or_invent_schema():
    f, n = gen("canonical_missing", html(), askf=ask_with(GOOD(fix_type="canonical_set", after='<link rel="canonical" href="https://other.test/">')))
    assert f["source"] == "rule" and "other.test" not in f["after"] and any("own https URL" in x for x in n)
    bad = '{"@context": "https://schema.org", "@type": "Organization", "name": "Ex",}'; h = html(head=f'<script type="application/ld+json">{bad}</script>')
    f, n = gen("schema_invalid", h, askf=ask_with(GOOD(fix_type="schema_repair", before=bad, after='{"@context": "https://schema.org", "@type": "Organization", "name": "Ex", "telephone": "+91 99999"}')))
    assert f["source"] == "rule" and "telephone" not in f["after"] and any("not in the original" in x for x in n)
    f, n = gen("schema_invalid", h, askf=ask_with(GOOD(fix_type="schema_repair", before=bad, after='{"@type": "Product", "name": "Ex", "aggregateRating": {"ratingValue": "5"}}')))
    assert f["source"] == "rule"
    f, n = gen("mixed_content", html(head='<script src="http://ex.test/a.js"></script>'), askf=ask_with(GOOD(fix_type="mixed_https", before="http://other.test", after="https://other.test")))
    assert f["source"] == "rule" and f["before"] == "http://ex.test"

def test_json_parse_failure_uses_fallback(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x"); monkeypatch.delenv("LLM_DISABLED", raising=False)
    monkeypatch.setattr(llm, "ask", lambda *a, **k: "Sorry, here is the fix: no json at all")     # llm.ask_json raises ValueError
    f, n = gen("viewport_missing", html(vp=False), askf=None); assert f["source"] == "rule" and any("unusable" in x for x in n)
    monkeypatch.setattr(llm, "ask", lambda *a, **k: '{"fix_type": "viewport_add", "after": ')     # truncated JSON
    f, n = gen("viewport_missing", html(vp=False), askf=None); assert f["source"] == "rule"
    monkeypatch.setattr(llm, "ask", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("API down")))
    f, n = gen("viewport_missing", html(vp=False), askf=None); assert f["source"] == "rule" and any("failed" in x for x in n)
    monkeypatch.setattr(llm, "ask", lambda *a, **k: (_ for _ in ()).throw(llm.LLMUnavailable("LLM_DISABLED=1"))); f, n = gen("viewport_missing", html(vp=False), askf=None); assert f["source"] == "rule" and any("no Claude" in x for x in n)

def test_json_failure_with_no_rule_gives_nothing():
    f, n = gen("title_missing", html(title=""), codes=["title_missing"], askf=ask_with("not json")); assert f["source"] == "rule" and json.loads(f["after"])["title"] == "Main topic here"      # H1 reused, nothing invented
    f, n = gen("title_missing", html(title="").replace("Main topic here", "Hi"), codes=["title_missing"], askf=ask_with("not json")); assert f is None and any("unusable" in x for x in n)

def test_llm_title_meta_validated():
    old = html(title="x" * 70, meta="")
    ok = {"fix_type": "title_meta", "target_url": B + "/a/", "before": {}, "after": {"title": "Consultation Guide: What to Expect at Ex Clinic", "meta": "Learn how a consultation works, what to bring and how long each visit takes, with answers to common questions from new patients."},
          "rationale": "r", "confidence": "High", "evidence": "title 70 chars"}
    f, _ = gen("title_long", old, askf=ask_with(ok), codes=["title_long"]); assert f["source"] == "llm" and json.loads(f["after"])["meta"] == ""    # meta had no issue code: kept as is
    bad = json.loads(json.dumps(ok)); bad["after"]["title"] = "Best Clinic: We guarantee results at Ex Clinic today"
    f, n = gen("title_long", old, askf=ask_with(bad), codes=["title_long"]); assert f["source"] == "rule" or f is None
    assert any("rejected" in x for x in n)


# ---- approval rules
def fixd(risk="SAFE", ft="viewport_add", c="High"): return {"fix_type": ft, "risk": risk, "confidence": c, "code": "x", "target_url": B + "/a/", "before": "", "after": T.VIEWPORT, "rationale": "r", "evidence": "e", "source": "rule"}

def test_auto_approval_rules():
    p = T.proposal_for("s1", SITE, fixd(), None, 5); assert p["status"] == "approved" and p["needs_approval"] is False and p["decided_by"] == "auto:technical_fixer" and p["kind"] == "tech_fix"
    assert T.proposal_for("s1", {**SITE, "tech_auto_safe": False}, fixd(), None, 5)["status"] == "pending"        # flag off
    assert T.proposal_for("s1", {k: v for k, v in SITE.items() if k != "tech_auto_safe"}, fixd(), None, 5)["status"] == "pending"
    assert T.proposal_for("s1", SITE, fixd(c="Medium"), None, 5)["status"] == "pending"
    assert T.proposal_for("s1", SITE, fixd(ft="manual"), None, 5)["needs_approval"] is True

def test_risky_never_auto_approved():
    for ft in ("noindex_remove", "canonical_set", "robots_unblock", "manual"):
        p = T.proposal_for("s1", SITE, fixd(risk="RISKY", ft=ft), None, 5)
        assert p["status"] == "pending" and p["needs_approval"] is True and "decided_by" not in p, ft
    assert not T.auto_ok(SITE, "RISKY", "High", "canonical_set")

def test_medical_site_never_auto():
    for site in (MED, REV, {**SITE, "profile": "medical", "reviewer_required": False}):
        for ft in ("viewport_add", "title_meta", "alt_text", "canonical_set"):
            f = fixd(ft=ft)
            if ft == "title_meta": f.update(before=json.dumps({"title": "a", "meta": "b"}), after=json.dumps({"title": "c", "meta": "d"}))
            if ft == "alt_text": f.update(after=json.dumps([{"src": "/a.jpg", "alt": "Chest diagram"}]))
            p = T.proposal_for("s1", site, f, None, 5); assert p["status"] == "pending" and p["needs_approval"] is True, (site, ft)
    p = T.proposal_for("s1", MED, {**fixd(ft="title_meta"), "before": json.dumps({"title": "a", "meta": "b"}), "after": json.dumps({"title": "c", "meta": "d"})}, None, 5)
    assert p["source"].startswith("onpage") and runmod.needs_reviewer({"reviewer_required": True}, {**p, "kind": "title_meta"})            # reviewer gate stays on
    assert T.proposal_for("s1", SITE, {**fixd(ft="title_meta"), "before": json.dumps({"title": "a", "meta": "b"}), "after": json.dumps({"title": "c", "meta": "d"})}, None, 5)["source"] == "tech_fixer"


# ---- edits + revert + live verification (pure)
def roundtrip(ft, text, fix):
    new, pairs = T.build_edit(ft, text, fix); assert new != text and all(p["new"] for p in pairs)
    back, ok = revert_alt(new, pairs); assert ok and back == text, (ft, back); return new

def test_build_edit_roundtrips_and_verify():
    h = html(vp=False)
    new = roundtrip("viewport_add", h, {"after": T.VIEWPORT}); assert T.verify_text("viewport_add", {}, new) and not T.verify_text("viewport_add", {}, h)
    h = html(can="/a/") .replace("</head>", '<link rel="canonical" href="https://ex.test/old/"></head>')
    fx = {"after": f'<link rel="canonical" href="{B}/a/">'}; new = roundtrip("canonical_set", h, fx); assert T.verify_text("canonical_set", fx, new) and not T.verify_text("canonical_set", fx, h)
    h = html(head='<meta name="robots" content="noindex">'); new = roundtrip("noindex_remove", h, {}); assert T.verify_text("noindex_remove", {}, new) and not T.verify_text("noindex_remove", {}, h)
    assert not T.verify_text("noindex_remove", {}, new, {"X-Robots-Tag": "noindex"})
    h = html(head='<img src="http://ex.test/i.png"><a href="http://www.ex.test/p/">p</a><img src="http://cdn.other/i.png">'); fx = {"after": "https://ex.test"}
    new = roundtrip("mixed_https", h, fx); assert "http://cdn.other" in new and "https://www.ex.test/p/" in new and T.verify_text("mixed_https", fx, new) and not T.verify_text("mixed_https", fx, h)
    bad = '{"a": 1,}'; h = html(head=f'<script type="application/ld+json">{bad}</script>'); fx = {"before": bad, "after": '{"a": 1}'}
    new = roundtrip("schema_repair", h, fx); assert T.verify_text("schema_repair", fx, new) and not T.verify_text("schema_repair", fx, h)
    t = "User-agent: *\nDisallow:"; fx = {"after": f"Sitemap: {B}/sitemap.xml"}
    new = roundtrip("robots_sitemap_line", t, fx); assert new.endswith("Sitemap: https://ex.test/sitemap.xml\n") and T.verify_text("robots_sitemap_line", fx, new)
    with pytest.raises(RuntimeError): T.build_edit("robots_sitemap_line", new, fx)         # already there
    with pytest.raises(RuntimeError): T.build_edit("viewport_add", html(), {"after": T.VIEWPORT})
    with pytest.raises(RuntimeError): T.build_edit("schema_repair", html(), fx)             # block not in this file
    with pytest.raises(RuntimeError): T.build_edit("noindex_remove", html(), {})
    with pytest.raises(RuntimeError): T.build_edit("nonsense", "x", {})

def test_verify_title_meta_and_alt():
    fx = {"new": {"title": "Short Title For Page", "meta": "m" * 90}}
    assert T.verify_text("title_meta", fx, html(title="Short  Title For Page", meta="m" * 90)) and not T.verify_text("title_meta", fx, html(title="Old", meta="m" * 90))
    fa = {"new": {"images": [{"src": "/u/grade-3.jpg", "alt": "Grade 3 chest diagram"}]}}
    assert T.verify_text("alt_text", fa, '<img src="/u/grade-3-300x200.jpg" alt="Grade 3 chest diagram">') and not T.verify_text("alt_text", fa, '<img src="/u/grade-3.jpg" alt="">')
    assert not T.verify_text("alt_text", fa, "<p>no images</p>")


# ---- fakes for the full flow
class DB:
    def __init__(s): s.t = {}; s.n = {}
    def rows(s, t): return s.t.setdefault(t, [])
    def match(s, r, k, v):
        x = r.get(k)
        if v.startswith("eq."): return str(x) == v[3:] or (v[3:] in ("true", "false") and str(x).lower() == v[3:])
        if v.startswith("neq."): return str(x) != v[4:]
        if v.startswith("in."): return str(x) in v[4:-1].split(",")
        if v.startswith("gt."): return x is not None and float(x) > float(v[3:])
        return True
    def select(s, t, **p):
        out = [dict(r) for r in s.rows(t) if all(s.match(r, k, v) for k, v in p.items() if k not in ("select", "order", "limit", "offset"))]
        if p.get("order", "").endswith(".desc"): out.sort(key=lambda r: -r.get("id", 0))
        return out[:int(p["limit"])] if p.get("limit") else out
    def insert(s, t, rows):
        out = []
        for r in rows: s.n[t] = s.n.get(t, 0) + 1; r = {"id": s.n[t], **r}; s.rows(t).append(r); out.append(dict(r))
        return out
    def update(s, t, m, d):
        for r in s.rows(t):
            if all(str(r.get(k)) == str(v) for k, v in m.items()): r.update(d)

@pytest.fixture
def db(monkeypatch):
    d = DB(); monkeypatch.setattr(sb, "select", d.select); monkeypatch.setattr(sb, "insert", d.insert); monkeypatch.setattr(sb, "update", d.update)
    monkeypatch.setattr(runmod, "sites", lambda: {"s1": dict(SITE)}); monkeypatch.setenv("FIXER_VERIFY_WAIT", "0"); monkeypatch.setenv("LLM_DISABLED", "1")
    d.insert("sites", [{"id": "s1", "mode": "live"}]); return d

class Rem:
    def __init__(s, files): s.files = files
    def read(s, p): return s.files.get(p)
    def write(s, p, data): s.files[p] = data

def static_site(files):
    st = object.__new__(Static); st.site = SITE; st.base = B; st.host = "ex.test"; st.root = "/public_html"; st.urls = {}; st.blog = "blog"; st.r = Rem(files); st._on = True; return st

class Live:
    """Serves the 'live' pages from the fake server files. stale=True keeps serving the old text (cache / change not visible)."""
    def __init__(s, files, stale=False): s.files, s.stale, s.old = files, stale, dict(files)
    def __call__(s, url):
        f = s.old if s.stale else s.files; path = {"/": "index.html"}.get(url.replace(B, ""), url.replace(B, "").strip("/"))
        key = "/public_html/" + (path if "." in path.split("/")[-1] else path + "/index.html")
        class R: status_code = 200; headers = {}
        R.text = (f.get(key) or b"").decode() if key in f else ""; R.status_code = 200 if key in f else 404; return R

def seed(db, code, url=B + "/a/", sev="High"):
    return db.insert("tech_issues", [{"site_id": "s1", "url": url, "code": code, "issue": code, "severity": sev, "effort": "Low", "detail": "d", "fix": "f", "why": "w", "status": "open", "found_at": "2026-11-01T00:00:00+00:00"}])[0]

def test_full_flow_auto_apply_verify(db):
    files = {"/public_html/a/index.html": html(vp=False).encode()}; st = static_site(files); live = Live(files)
    iss = seed(db, "viewport_missing"); T.run("s1", client=st, fetcher=live, askf=no_llm, sleep=lambda x: None)
    p = db.rows("proposals")[0]; f = db.rows("tech_fixes")[0]
    assert p["kind"] == "tech_fix" and p["status"] == "applied" and p["decided_by"] == "auto:technical_fixer" and p["backup_id"]
    assert f["status"] == "verified" and f["issue_id"] == iss["id"] and f["risk"] == "SAFE" and f["proposal_id"] == p["id"] and f["verified_at"]
    assert db.rows("tech_issues")[0]["status"] == "fixed" and b"viewport" in files["/public_html/a/index.html"]
    assert any(k.endswith(f"{p['backup_id']}.bak") for k in files)                      # file backup kept on the server
    # rollback through the normal request path
    sb.update("proposals", {"id": p["id"]}, {"status": "rollback_requested"}); T.run("s1", client=st, fetcher=live, askf=no_llm, sleep=lambda x: None)
    assert b"viewport" not in files["/public_html/a/index.html"] and db.rows("proposals")[0]["status"] == "rolled_back" and db.rows("tech_fixes")[0]["status"] == "rolled_back" and db.rows("tech_issues")[0]["status"] == "open"

def test_verification_failure_keeps_issue_open(db):
    files = {"/public_html/a/index.html": html(vp=False).encode()}; st = static_site(files); live = Live(files, stale=True)
    seed(db, "viewport_missing"); T.run("s1", client=st, fetcher=live, askf=no_llm, sleep=lambda x: None)
    p = db.rows("proposals")[0]; f = db.rows("tech_fixes")[0]
    assert p["status"] == "failed" and "did not show" in p["reason"] and f["status"] == "failed" and db.rows("tech_issues")[0]["status"] == "open"
    assert b"viewport" not in files["/public_html/a/index.html"]                         # change was removed again

def test_run_applied_fix_unverified_stays_applied_then_fails(db):
    seed(db, "title_long"); sb.insert("proposals", [{"site_id": "s1", "page": B + "/a/", "kind": "title_meta", "status": "applied", "new": {"title": "New Title Here For Page", "meta": "m" * 90}, "applied_at": "x", "backup_id": 7}])
    sb.insert("tech_fixes", [{"issue_id": 1, "site_id": "s1", "url": B + "/a/", "code": "title_long", "fix_type": "title_meta", "risk": "SAFE", "status": "approved", "manual": False, "before": "{}", "after": "{}", "proposal_id": 1}])
    files = {"/public_html/a/index.html": html(title="Old").encode()}; live = Live(files)
    T.sync("s1", live, lambda x: None); f = db.rows("tech_fixes")[0]; assert f["status"] == "applied" and f["verify_tries"] == 1 and db.rows("tech_issues")[0]["status"] == "open"
    T.sync("s1", live, lambda x: None); T.sync("s1", live, lambda x: None); f = db.rows("tech_fixes")[0]; assert f["status"] == "failed" and db.rows("tech_issues")[0]["status"] == "open"
    sb.update("tech_fixes", {"id": 1}, {"status": "applied", "verify_tries": 0}); files["/public_html/a/index.html"] = html(title="New Title Here For Page", meta="m" * 90).encode()
    T.sync("s1", live, lambda x: None); assert db.rows("tech_fixes")[0]["status"] == "verified" and db.rows("tech_issues")[0]["status"] == "fixed"

def test_risky_fix_waits_for_approval_then_applies(db):
    files = {"/public_html/a/index.html": html(head='<meta name="robots" content="noindex">').encode()}; st = static_site(files); live = Live(files)
    seed(db, "noindex", sev="Critical"); T.run("s1", client=st, fetcher=live, askf=no_llm, sleep=lambda x: None)
    p = db.rows("proposals")[0]; assert p["status"] == "pending" and p["needs_approval"] is True and db.rows("tech_fixes")[0]["status"] == "proposed" and db.rows("tech_fixes")[0]["risk"] == "RISKY"
    assert b"noindex" in files["/public_html/a/index.html"]                              # nothing written before approval
    sb.update("proposals", {"id": p["id"]}, {"status": "approved", "decided_by": "me@x.com"}); T.run("s1", client=st, fetcher=live, askf=no_llm, sleep=lambda x: None)
    assert db.rows("tech_fixes")[0]["status"] == "verified" and db.rows("tech_issues")[0]["status"] == "fixed" and not re.search(rb"<meta[^>]*robots", files["/public_html/a/index.html"])

def test_rejected_and_not_proposed_again(db):
    files = {"/public_html/a/index.html": html(vp=False).encode()}; st = static_site(files); live = Live(files); db.rows("sites")[0].update(tech_auto_safe=False)
    runmod.sites = lambda: {"s1": {**SITE, "tech_auto_safe": False}}
    seed(db, "viewport_missing"); T.run("s1", client=st, fetcher=live, askf=no_llm)
    sb.update("proposals", {"id": 1}, {"status": "rejected"}); T.run("s1", client=st, fetcher=live, askf=no_llm)
    assert db.rows("tech_fixes")[0]["status"] == "rejected" and len(db.rows("proposals")) == 1
    T.run("s1", client=st, fetcher=live, askf=no_llm); assert len(db.rows("proposals")) == 1 and len(db.rows("tech_fixes")) == 1

def test_refuses_auto_approved_risky_at_apply_time(db):
    st = static_site({"/public_html/a/index.html": html().encode()})
    p = {"id": 1, "page": B + "/a/", "post_id": 1, "decided_by": "auto:technical_fixer", "new": {"fix_type": "noindex_remove", "risk": "RISKY", "after": "", "before": ""}}
    with pytest.raises(RuntimeError, match="RISKY"): T.apply_tech_fix(SITE, st, p)

def test_manual_risky_fix_is_only_instructions(db):
    st = static_site({}); seed(db, "redirect_chain"); T.run("s1", client=st, fetcher=Live({}), askf=no_llm)
    f = db.rows("tech_fixes")[0]; assert f["manual"] is True and f["fix_type"] == "manual" and f["risk"] == "RISKY" and f["status"] == "proposed" and not db.rows("proposals")

def test_wordpress_cannot_apply_tag_fix_without_plugin_kind(db):
    class WPc:
        static = False
        def live(s, u): return {"post_id": "9"}
    h = html(vp=False); live = lambda u: type("R", (), {"status_code": 200, "text": h, "headers": {}})
    seed(db, "viewport_missing"); T.run("s1", client=WPc(), fetcher=live, askf=no_llm)
    f = db.rows("tech_fixes")[0]; assert f["manual"] is True and not db.rows("proposals")
    runmod.sites = lambda: {"s1": {**SITE, "tech_plugin_kind": True}}; db.rows("tech_fixes").clear(); db.rows("tech_issues")[0]["status"] = "open"
    T.run("s1", client=WPc(), fetcher=live, askf=no_llm, dry=False)
    # not auto-applied because add_mod is missing on this fake: proposal is created and fails in apply, never crashes the run
    assert db.rows("proposals")[0]["post_id"] == 9

def test_fixer_max_default_and_env(db, monkeypatch):
    monkeypatch.delenv("FIXER_MAX", raising=False); assert T.fixer_max() == 10
    monkeypatch.setenv("FIXER_MAX", "3"); assert T.fixer_max() == 3 and T.fixer_max(5) == 5
    monkeypatch.setenv("FIXER_MAX", "abc"); assert T.fixer_max() == 10
    files = {f"/public_html/p{i}/index.html": html(vp=False).encode() for i in range(15)}; st = static_site(files); live = Live(files)
    runmod.sites = lambda: {"s1": {**SITE, "tech_auto_safe": False}}
    for i in range(15): seed(db, "viewport_missing", url=f"{B}/p{i}/")
    monkeypatch.delenv("FIXER_MAX", raising=False); T.run("s1", client=st, fetcher=live, askf=no_llm); assert len(db.rows("proposals")) == 10 and len(db.rows("tech_fixes")) == 10
    T.run("s1", maxn=3, client=st, fetcher=live, askf=no_llm); assert len(db.rows("proposals")) == 13

def test_select_issues_order_and_reproposal_after_audit(db):
    seed(db, "title_long", sev="Low"); seed(db, "noindex", url=B + "/n/", sev="Critical"); seed(db, "cwv_lcp", url=B + "/c/"); seed(db, "viewport_missing", url=B + "/v/", sev="High")
    assert [i["code"] for i in T.select_issues("s1")] == ["noindex", "viewport_missing", "title_long"]                # cwv has no fix path; worst first
    sb.insert("tech_fixes", [{"site_id": "s1", "url": B + "/v/", "code": "viewport_missing", "status": "verified", "verified_at": "2026-11-05T00:00:00+00:00"}])
    assert "viewport_missing" not in [i["code"] for i in T.select_issues("s1")]                                         # found before it was verified: wait for next audit
    db.rows("tech_issues")[3]["found_at"] = "2026-11-09T00:00:00+00:00"
    assert "viewport_missing" in [i["code"] for i in T.select_issues("s1")]                                              # audit found it again after the fix: try again
    for _ in range(2): sb.insert("tech_fixes", [{"site_id": "s1", "url": B + "/n/", "code": "noindex", "status": "failed"}])
    assert "noindex" not in [i["code"] for i in T.select_issues("s1")]

def test_llm_cost_link(db):
    db.insert("llm_usage", [{"agent": "technical_fixer", "cost_usd": 0.5}]); m = T.usage_mark(); assert m == 1
    db.insert("llm_usage", [{"agent": "technical_fixer", "cost_usd": 0.0012}, {"agent": "technical_fixer", "cost_usd": 0.0008}])
    assert T.usage_since(m) == (3, 0.002) and T.usage_since(3) == (None, 0)
    rows = T.fix_rows("s1", [issue("viewport_missing", i=4)], {**fixd(), "target_url": B + "/a/"}, {"id": 9, "status": "pending"}, (3, 0.002))
    assert rows[0]["llm_usage_id"] == 3 and rows[0]["llm_cost_usd"] == 0.002 and rows[0]["proposal_id"] == 9 and rows[0]["issue_id"] == 4
    monkey = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no table")); sb.select = monkey
    assert T.usage_mark() is None and T.usage_since(None) == (None, None)

def test_dry_run_writes_nothing(db, capsys):
    files = {"/public_html/a/index.html": html(vp=False).encode()}; st = static_site(files); seed(db, "viewport_missing")
    T.run("s1", dry=True, client=st, fetcher=Live(files), askf=no_llm); assert not db.rows("proposals") and not db.rows("tech_fixes") and "viewport_add" in capsys.readouterr().out
