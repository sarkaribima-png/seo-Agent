import os, sys, types
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import requests
from seo_agent import tech_audit as T

B = "https://ex.test"
def page(title="T", meta="m" * 100, can=None, h1="<h1>H</h1>", extra="", robots="", vp=True, body="word " * 100):
    return ("<html><head>" + (f"<title>{title}</title>" if title is not None else "") + (f'<meta name="description" content="{meta}">' if meta else "")
            + (f'<link rel="canonical" href="{can}">' if can else "") + ('<meta name="viewport" content="width=device-width">' if vp else "")
            + (f'<meta name="robots" content="{robots}">' if robots else "") + extra + "</head><body>" + h1 + f"<p>{body}</p></body></html>")

class R:
    def __init__(self, status=200, text="", headers=None): self.status_code, self.text, self.headers = status, text, headers or {}

def fake(routes):
    def g(url, timeout=25):
        v = routes.get(url)
        if v is None: return R(404, "nf")
        if isinstance(v, Exception): raise v
        return v
    return g

SM = lambda *u: "<urlset>" + "".join(f"<url><loc>{x}</loc></url>" for x in u) + "</urlset>"

def site_routes():
    home, a, b, c, d = B + "/", B + "/a/", B + "/b/", B + "/c/", B + "/d/"
    return {
     B + "/robots.txt": R(200, "User-agent: *\nDisallow: /wp-content/plugins/\nDisallow: /wp-includes/\nDisallow: /private/\n"),
     B + "/sitemap.xml": R(200, SM(home, a, b, c, d, B + "/gone/", B + "/old/", B + "/private/x/", "https://other.test/z/")),
     "https://ex.test/": R(200, page(title="Home", can=B + "/", extra=f'<a href="{a}">a</a><a href="{b}">b</a><a href="{B}/gone/">g</a><a href="{c}">c</a><script type="application/ld+json">{{bad json</script>')),
     a: R(200, page(title="Same", can="https://main-site.com/a/", extra='<script src="http://cdn.x/a.js"></script>')),
     b: R(200, page(title="Same", meta="", can=B + "/b/", h1="", vp=False)),
     c: R(200, page(title="Cc", can=B + "/other/", robots="noindex")),
     d: R(200, page(title="Same", can=None)),
     B + "/gone/": R(404, "x"),
     B + "/old/": R(301, "", {"Location": B + "/old2/"}), B + "/old2/": R(302, "", {"Location": B + "/new/"}), B + "/new/": R(200, page(title="New", can=B + "/new/")),
     B + "/private/x/": R(200, page(title="P", can=B + "/private/x/")),
     "http://ex.test/": R(200, "no redirect"), "https://www.ex.test/": R(200, page(title="Home")),
    }

def codes(iss, code): return [i for i in iss if i["code"] == code]

def run_audit(monkeypatch, routes=None, **kw):
    monkeypatch.setattr(T, "http_get", fake(routes or site_routes()))
    return T.audit_site(B, {"type": "static"}, **kw)

def test_full_audit(monkeypatch):
    iss, notes = run_audit(monkeypatch)
    urls = lambda code: {i["url"] for i in codes(iss, code)}
    assert urls("canonical_other_domain") == {B + "/a/"} and codes(iss, "canonical_other_domain")[0]["severity"] == "Critical"
    assert urls("canonical_other_url") == {B + "/c/"}
    assert urls("canonical_missing") == {B + "/d/"}
    assert urls("noindex") == {B + "/c/"}
    assert urls("status_4xx") == {B + "/gone/"}
    assert urls("redirect_chain") == {B + "/old/"} and urls("redirect_temp") == set()   # first hop is 301
    assert B + "/old/" in urls("redirect_in_sitemap")
    assert urls("title_duplicate") == {B + "/b/", B + "/d/"}   # /a/ has the same title but canonicals elsewhere, so it is not counted
    assert urls("meta_missing") == {B + "/b/"} and urls("h1_missing") == {B + "/b/"} and urls("viewport_missing") == {B + "/b/"}
    assert urls("mixed_content") == {B + "/a/"}
    assert urls("schema_invalid") == {B + "/"}
    assert urls("broken_link") == {B + "/"}
    assert urls("sitemap_other_host") == {"https://other.test/z/"}
    assert urls("sitemap_blocked") == {B + "/private/x/"}
    assert urls("robots_blocks_assets") and urls("http_no_redirect") and urls("host_variants")
    assert any("PSI_API_KEY" in n for n in notes)                      # CWV skipped gracefully
    assert not codes(iss, "robots_blocks_all")
    # every row carries exact fix text, severity valid
    assert all(i["fix"] and i["severity"] in T.SEV for i in iss)
    assert "https://ex.test/a/" in codes(iss, "canonical_other_domain")[0]["fix"]

def test_orphans_only_when_full_crawl(monkeypatch):
    iss, _ = run_audit(monkeypatch)
    assert {i["url"] for i in codes(iss, "orphan")} >= {B + "/d/", B + "/old/"} and B + "/a/" not in {i["url"] for i in codes(iss, "orphan")}
    iss, notes = run_audit(monkeypatch, max_pages=3)
    assert not codes(iss, "orphan") and any("orphan check skipped" in n for n in notes)

def test_robots_blocks_all_and_missing_sitemap(monkeypatch):
    r = {B + "/robots.txt": R(200, "User-agent: *\nDisallow: /\n"), "https://ex.test/": R(200, page(title="H", can=B + "/"))}
    iss, notes = run_audit(monkeypatch, r)
    assert codes(iss, "robots_blocks_all")[0]["severity"] == "Critical" and codes(iss, "sitemap_missing") and codes(iss, "robots_no_sitemap")
    assert any("only the home page" in n for n in notes)

def test_https_down_and_redirect_loop(monkeypatch):
    r = {"https://ex.test/": requests.ConnectionError("ssl")}
    iss, _ = run_audit(monkeypatch, r)
    assert codes(iss, "https_down")[0]["severity"] == "Critical" and len(iss) == 1   # no noise findings when the site is unreachable
    hops, resp, loop, err = (lambda: (monkeypatch.setattr(T, "http_get", fake({"https://l/a": R(301, "", {"Location": "https://l/b"}), "https://l/b": R(301, "", {"Location": "https://l/a"})})), T.follow("https://l/a"))[1])()
    assert loop

def test_wordpress_fallback_when_no_sitemap(monkeypatch):
    monkeypatch.setattr(T, "http_get", fake({"https://ex.test/": R(200, page(title="H", can=B + "/"))}))
    monkeypatch.setattr(T, "wp_fallback_urls", lambda base: [B + "/"])
    iss, notes = T.audit_site(B, {"type": "wordpress"})
    assert any("REST list" in n for n in notes)

def test_psi_thresholds(monkeypatch):
    j = {"loadingExperience": {"metrics": {"LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 4800}, "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 5}, "INTERACTION_TO_NEXT_PAINT": {"percentile": 650}}}}
    monkeypatch.setattr(T.requests, "get", lambda *a, **k: types.SimpleNamespace(ok=True, status_code=200, json=lambda: j))
    out, note = T.psi(B + "/", "k", {})
    d = {i["code"]: i for i in out}
    assert d["cwv_lcp"]["severity"] == "High" and "cwv_cls" not in d and d["cwv_inp"]["severity"] == "High" and note == ""
    monkeypatch.setattr(T.requests, "get", lambda *a, **k: types.SimpleNamespace(ok=False, status_code=429, json=lambda: {}))
    assert T.psi(B + "/", "k", {})[0] == []   # graceful on API error

def test_summarize_and_save(monkeypatch):
    iss, _ = run_audit(monkeypatch)
    s = T.summarize(iss)
    assert s[0]["severity"] == "Critical" and all(len(x["examples"]) <= 5 and x["count"] >= 1 for x in s)
    saved = {"ins": [], "upd": []}
    from seo_agent import run as R_, sb
    monkeypatch.setattr(R_, "sites", lambda: {"s": {"id": "s", "domain": "ex.test", "wp_base": B, "type": "static"}})
    monkeypatch.setattr(sb, "select", lambda t, **k: [{"url": B + "/d/", "issue": T.CAT["canonical_missing"][1]}])   # this one was marked ignored
    monkeypatch.setattr(sb, "update", lambda t, m, d: saved["upd"].append((t, m, d)))
    monkeypatch.setattr(sb, "insert", lambda t, rows: saved["ins"].extend(rows) or rows)
    T.run("s")
    assert saved["upd"] == [("tech_issues", {"site_id": "s", "status": "open"}, {"status": "superseded"})]
    assert saved["ins"] and not any(r["code"] == "canonical_missing" and r["url"] == B + "/d/" for r in saved["ins"])
    assert {"site_id", "url", "issue", "severity", "detail", "fix", "effort", "found_at", "status"} <= set(saved["ins"][0])
