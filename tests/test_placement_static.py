import json, pytest
from seo_agent import placement, static, run
from seo_agent.static import Static, add_section, add_alt, revert_alt, img_key

PAGE = ('<html><head><title>T</title></head><body><main><div class="w"><h2>Overview</h2></div><p>intro</p>'
        '<div class="w"><div class="x"><h2 class="t">Recovery &amp; care</h2></div></div><p>rec</p><h3>Day one</h3><p>d1</p>'
        '<div class="w"><h2>Frequently asked questions</h2></div><h3>Q1</h3><p>a</p>'
        '<div class="w"><div class="x"><h2>Contact</h2></div></div></main><footer><h2>Footer links</h2></footer></body></html>')


def test_normalize():
    assert placement.normalize(None) == "before_faq" and placement.normalize("bogus") == "before_faq" and placement.normalize("end") == "end"
    assert placement.normalize({"after_heading": "  Recovery   care "}) == {"after_heading": "Recovery care"}
    assert placement.normalize({"after_heading": "A", "occurrence": 2}) == {"after_heading": "A", "occurrence": 2}
    for bad in ({"after_heading": " "}, {"after_heading": "x" * 300}, {"after_heading": "x", "occurrence": 99}, {"after_heading": "x", "occurrence": "a"}, 5):
        with pytest.raises(ValueError): placement.normalize(bad)


def test_after_heading_steps_out_of_wrappers_and_skips_subheadings():
    o = add_section(PAGE, 5, "<h2>New</h2>", {"after_heading": "overview"})
    assert 'intro</p>\n<!--sab:5--><div data-sab="5"><h2>New</h2></div><!--/sab:5-->\n<div class="w"><div class="x"><h2 class="t">Recovery' in o
    o = add_section(PAGE, 5, "<h2>New</h2>", {"after_heading": "Recovery & Care"})      # h3 'Day one' belongs to this block
    assert '<p>d1</p>\n<!--sab:5-->' in o and o.index("sab:5") < o.index("Frequently")


def test_after_heading_last_block_stays_before_footer():
    o = add_section(PAGE, 5, "<h2>New</h2>", {"after_heading": "Contact"})
    assert o.index("sab:5") < o.index("<footer") and o.index("sab:5") > o.index("Contact")


def test_after_heading_not_found_or_ambiguous():
    with pytest.raises(RuntimeError, match="not found"): add_section(PAGE, 5, "x", {"after_heading": "Nope"})
    with pytest.raises(RuntimeError): add_section("<h2>Tips a</h2><h2>Tips b</h2>", 5, "x", {"after_heading": "Tips"})
    o = add_section("<main><h2>Tips</h2><p>a</p><h2>Tips</h2><p>b</p><h2>End</h2></main>", 5, "N", {"after_heading": "Tips", "occurrence": 2})
    assert o.index("<p>b</p>") < o.index("sab:5") < o.index("<h2>End")


def test_old_positions_unchanged():
    assert add_section(PAGE, 5, "N", "before_faq").index("sab:5") < PAGE.index("Frequently") + 20
    o = add_section(PAGE, 5, "N", "after_faq"); assert o.index("Frequently") < o.index("sab:5") < o.index("Contact")
    o = add_section(PAGE, 5, "N", "end"); assert o.index("sab:5") > o.index("Contact") and o.index("sab:5") < o.index("<footer")
    assert "sab:5" in add_section(PAGE, 5, "N", None)


def test_img_key():
    assert img_key("https://x.in/wp-content/uploads/2025/Hero-Img-300x200.JPG?x=1") == "hero-img" and img_key("/a/b/clinic.webp") == "clinic" and img_key("") == ""


ALT_HTML = ('<main><img src="/u/gyno-grade-3-300x200.jpg" alt="" width="300"><img data-src="/u/clinic-room.webp" src="data:image/gif;base64,AA" alt>'
            '<img src="/u/kept.jpg" alt="Already written"><img src="/u/other.jpg"><img src="/u/x.png" alt="" />'
            '<script>var s=\'<img src="/u/gyno-grade-3.jpg" alt="">\'</script><!-- <img src="/u/gyno-grade-3.jpg" alt=""> --></main>')
ITEMS = [{"src": "https://x.in/u/gyno-grade-3.jpg", "alt": 'Chest diagram "grade 3"'}, {"src": "/u/clinic-room.webp", "alt": "Consultation room with two chairs"},
         {"src": "/u/kept.jpg", "alt": "Must not overwrite"}, {"src": "/u/x.png", "alt": "Chart of the steps"}]


def test_add_alt_and_revert():
    new, pairs = add_alt(ALT_HTML, 7, ITEMS)
    assert len(pairs) == 3 and new.count('data-sab="7"') == 3
    assert 'alt="Chest diagram &quot;grade 3&quot;" data-sab="7">' in new
    assert 'src="data:image/gif;base64,AA" alt="Consultation room with two chairs" data-sab="7">' in new
    assert 'alt="Chart of the steps" data-sab="7" />' in new
    assert "Must not overwrite" not in new and '<img src="/u/other.jpg">' in new
    assert new.count("gyno-grade-3.jpg") == 2          # script and comment copies were not touched
    back, ok = revert_alt(new, pairs); assert ok and back == ALT_HTML
    with pytest.raises(RuntimeError): add_alt(ALT_HTML, 7, [{"src": "/nothing.jpg", "alt": "Something here"}])
    with pytest.raises(RuntimeError): add_alt(ALT_HTML, 7, [{"src": "/u/other.jpg", "alt": ""}])


class FakeRemote:
    def __init__(self, files): self.files = files
    def read(self, p): return self.files.get(p)
    def write(self, p, d): self.files[p] = d


def make_static(html):
    s = object.__new__(Static)
    s.site = {"banned_terms": ["guarantee"], "profile": "medical"}; s.base = "https://k.in"; s.host = "k.in"; s.root = "/www"; s._on = True
    s.r = FakeRemote({"/www/p/index.html": html.encode()}); s.urls = {}; pid = s._reg("https://k.in/p/"); return s, pid


def test_static_alt_end_to_end_and_rollback():
    s, pid = make_static("<html><head></head><body>" + ALT_HTML + "</body></html>")
    r = s.add_mod(pid, "alt", {"items": ITEMS}); mid = r["mod_id"]
    assert r["changed"] == 3 and b'alt="Consultation room with two chairs"' in s.r.files["/www/p/index.html"]
    assert ("/www/.sab/%d.bak" % mid) in s.r.files and ("/www/.sab/%d.alt.json" % mid) in s.r.files
    s.rollback_mod(pid, mid)
    assert s.r.files["/www/p/index.html"].decode() == "<html><head></head><body>" + ALT_HTML + "</body></html>"


def test_static_alt_rejects_claims_before_writing():
    s, pid = make_static("<html><head></head><body>" + ALT_HTML + "</body></html>"); before = dict(s.r.files)
    with pytest.raises(RuntimeError, match="alt text rejected"): s.add_mod(pid, "alt", {"items": [{"src": "/u/other.jpg", "alt": "Guaranteed results after surgery"}]})
    assert s.r.files == before


def test_static_section_after_heading():
    s, pid = make_static(PAGE)
    mid = s.add_mod(pid, "section", {"html": "<h2>Pain and anaesthesia</h2><p>Your surgeon explains this at the visit.</p>", "position": {"after_heading": "Overview"}})["mod_id"]
    t = s.r.files["/www/p/index.html"].decode(); assert t.index("intro</p>") < t.index("Pain and anaesthesia") < t.index("Recovery &amp; care")
    s.rollback_mod(pid, mid); assert s.r.files["/www/p/index.html"].decode().replace("\n", "") == PAGE   # rollback leaves the two padding newlines (existing behaviour)
    with pytest.raises(RuntimeError, match="not found"): s.add_mod(pid, "section", {"html": "<h2>X</h2><p>Plain words here.</p>", "position": {"after_heading": "Missing"}})


# ---- run.py wiring
class W:
    def __init__(self, text="Overview text. Recovery care"): self.calls = []; self.text = text; self.rolled = []
    def page_text(self, u): return self.text
    def add_mod(self, pid, kind, data): self.calls.append((pid, kind, data)); return {"mod_id": 11}
    def mod_visible(self, u, m): return True
    def rollback_mod(self, pid, m): self.rolled.append(m)


@pytest.fixture
def updates(monkeypatch):
    got = []; monkeypatch.setattr(run.sb, "update", lambda t, m, d: got.append((t, m, d))); return got


def test_modkinds_and_plugin_kind():
    assert run.PLUGIN_KIND["alt_text"] == "alt"
    n = {"images": [{"src": "/a.jpg", "alt": "Clinic room", "section": "x"}]}
    assert run.MODKINDS["alt_text"](n) == {"items": [{"src": "/a.jpg", "alt": "Clinic room"}]}
    assert run.MODKINDS["content_add"]({"html": "h", "position": {"after_heading": " A  b "}})["position"] == {"after_heading": "A b"}
    assert run.MODKINDS["content_add"]({"html": "h"})["position"] == "before_faq"


def test_apply_after_heading_checks_heading_on_page(updates):
    w = W(); p = {"id": 1, "kind": "content_add", "page": "u", "post_id": 5, "reason": "r", "new": {"html": "<h2>x</h2>", "position": {"after_heading": "Recovery care"}}}
    run.apply_mod({}, {}, w, p); assert w.calls[0][2]["position"] == {"after_heading": "Recovery care"} and updates[0][2]["status"] == "applied"
    p["new"]["position"] = {"after_heading": "Not on the page"}
    with pytest.raises(RuntimeError, match="not on this page"): run.apply_mod({}, {}, w, p)
    assert len(w.calls) == 1


def test_reviewer_gate(updates):
    row = {"reviewer_required": True}; w = W()
    alt = {"id": 2, "kind": "alt_text", "page": "u", "post_id": 5, "new": {"images": [{"src": "/a.jpg", "alt": "Clinic room"}]}}
    run.apply_mod({}, row, w, alt); assert w.calls == [] and updates == []                 # waits
    run.apply_mod({}, row, w, {**alt, "reviewed_by": "Dr X"}); assert w.calls[0][1] == "alt" and updates[0][2]["status"] == "applied"
    assert run.needs_reviewer(row, {"kind": "title_meta", "source": "onpage"}) and not run.needs_reviewer(row, {"kind": "title_meta", "source": "rank:kw"})
    assert not run.needs_reviewer({}, {"kind": "alt_text"}) and not run.needs_reviewer(row, {"kind": "schema"})


def test_apply_approved_holds_onpage_title_meta_until_reviewed(monkeypatch, updates):
    rows = [{"id": 3, "kind": "title_meta", "source": "onpage", "post_id": 5, "page": "u", "new": {"title": "T", "meta": "M"}},
            {"id": 4, "kind": "title_meta", "source": "onpage", "reviewed_by": "Dr X", "post_id": 6, "page": "v", "new": {"title": "T2", "meta": "M2"}}]
    monkeypatch.setattr(run.sb, "select", lambda t, **p: [{"id": "k", "reviewer_required": True}] if t == "sites" else rows)
    class W2:
        def __init__(s): s.seo = []
        def set_seo(s, pid, t, m): s.seo.append((pid, t, m)); return {"backup_id": 1}
    w = W2(); run.apply_approved({"id": "k"}, w)
    assert w.seo == [(6, "T2", "M2")] and [u[1]["id"] for u in updates] == [4]
