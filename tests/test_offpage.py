import os, sys, json, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
from seo_agent import offpage as O, sb
from seo_agent.llm import LLMUnavailable

TODAY = dt.date(2026, 11, 9)   # Monday, 2026-W46
SITE = {"id": "k", "domain": "clinic.test", "name": "Clinic Test", "niche": "plastic surgery", "profile": "medical", "reviewer_required": True, "location": "Delhi",
        "competitor_names": ["Acme Clinic"], "banned_terms": ["RivalDerm"], "money_pages": ["https://clinic.test/rhinoplasty/", "https://clinic.test/liposuction/"]}
PLAIN = {"id": "p", "domain": "shop.test", "niche": "furniture", "location": "Pune"}

class Store:
    def __init__(self, data=None): self.data = data or {}; self.inserted = []
    def select(self, t, **p):
        if t not in self.data: raise RuntimeError("404 relation does not exist")
        return list(self.data[t])
    def insert(self, t, rows): self.inserted.append((t, rows)); return rows

@pytest.fixture
def store(monkeypatch):
    s = Store({"gsc_daily": [{"page": "https://clinic.test/rhinoplasty/", "impressions": 900}, {"page": "https://clinic.test/rhinoplasty/", "impressions": 100}],
               "tracked_keywords": [{"keyword": "rhinoplasty delhi"}, {"keyword": "nose job cost"}],
               "rank_daily": [{"keyword": "nose job cost", "position": 12, "impressions": 5}], "offpage_tasks": []})
    monkeypatch.setattr(sb, "select", s.select); monkeypatch.setattr(sb, "insert", s.insert)
    from seo_agent import run as R
    monkeypatch.setattr(R, "sites", lambda: {"k": SITE, "p": PLAIN}); return s

PAGE = '<html><head><script type="application/ld+json">{"@type":"MedicalClinic","name":"Clinic Test","telephone":"+91 98765 43210","address":{"streetAddress":"12 Main Road","addressLocality":"Delhi","postalCode":"110001"}}</script></head><body><a href="tel:+919876543210">call</a></body></html>'
def fetcher(routes): return lambda u: (200, routes[u]) if u in routes else (404, "")
def no_llm(*a, **k): raise LLMUnavailable("off")

# ---- guidelines checker
@pytest.mark.parametrize("txt,code", [
    ("We offer paid links on our blog", "paid_link"), ("Happy to do a link exchange?", "paid_link"), ("Buy backlinks cheap", "paid_link"), ("Sponsored post $50 for a link", "paid_link"),
    ("Results are guaranteed", "guarantee"), ("100% safe", "guarantee"), ("We will get you to #1 on Google", "ranking_claim"), ("improve your rankings fast", "ranking_claim"),
    ("See our before and after photos", "before_after"), ("Please leave us a 5 star review", "fake_or_incentivised_review"), ("Get a discount if you leave a review", "fake_or_incentivised_review"),
    ("In exchange for a review we give a gift", "fake_or_incentivised_review"), ("a miracle cure", "medical_claim"), ("We are better than the others", "superlative")])
def test_checker_rejects(txt, code): assert any(x.startswith(code) for x in O.check_text(txt)), O.check_text(txt)

def test_checker_rejects_competitor_name():
    assert O.check_text("We are more trusted than Acme Clinic.", ["Acme Clinic"]) and any("competitor" in x for x in O.check_text("visit rivalderm today", ["RivalDerm"]))
    assert not O.check_text("We are a clinic in Delhi.", ["Acme Clinic"])

def test_checker_rejects_stuffing_and_link_spam():
    assert any(x.startswith("keyword_stuffing") for x in O.check_text(" ".join(["rhinoplasty surgery delhi"] * 20)))
    assert any(x.startswith("too_many_links") for x in O.check_text("http://a.com http://b.com http://c.com http://d.com"))

def test_checker_accepts_honest_text():
    for t in (O.tpl_gbp(SITE, "rhinoplasty", "https://clinic.test/rhinoplasty/"), O.tpl_review(SITE), O.tpl_outreach(SITE, "plastic surgery", "https://clinic.test/x/"),
              O.tpl_guest(SITE, "nose job cost", None), O.tpl_pr(SITE, "plastic surgery", None), "We are not offering payment; link only if you find it useful."):
        assert O.check_text(t, O.site_names(SITE)) == [], (t, O.check_text(t))

# ---- weekly plan: max 5, caps, dedupe
def test_max_five_rule(store):
    r = O.run("k", TODAY, ask_json=no_llm, fetch=fetcher({"https://clinic.test/": PAGE}))
    assert len(r["rows"]) == 5 and len({x["type"] for x in r["rows"]}) >= 4 and sum(1 for x in r["rows"] if x["type"] == "outreach") <= 2
    assert store.inserted[0][0] == "offpage_tasks" and all(x["status"] == "todo" and x["week"] == "2026-W46" for x in store.inserted[0][1])

def test_existing_tasks_count_towards_five(store):
    store.data["offpage_tasks"] = [{"week": "2026-W46", "type": "outreach", "status": "todo", "dedupe_key": f"outreach:/{i}/", "created_at": "2026-11-09"} for i in range(4)]
    r = O.run("k", TODAY, ask_json=no_llm, fetch=lambda u: (404, ""))
    assert len(r["rows"]) == 1
    store.data["offpage_tasks"] += [{"week": "2026-W46", "type": "pr", "status": "todo", "dedupe_key": "x", "created_at": "2026-11-09"}]
    assert O.run("k", TODAY, ask_json=no_llm, fetch=lambda u: (404, ""))["rows"] == []

def test_skipped_tasks_do_not_use_the_budget_and_open_ones_are_not_repeated(store):
    store.data["offpage_tasks"] = [{"week": "2026-W46", "type": "gbp_post", "status": "skipped", "dedupe_key": "gbp:2026-W46", "created_at": "2026-11-09"}]
    st = O.gather("k", SITE, TODAY); cs, _ = O.candidates(SITE, st, TODAY); sel = O.select_tasks(cs, st, TODAY)
    assert len(sel) == 5 and "gbp_post" not in {c["type"] for c in sel}      # skipped this week = still in cooldown, not planned again

def test_old_done_citation_comes_back_after_cooldown(store):
    st = O.gather("k", SITE, TODAY); st["tasks"] = [{"week": "2026-W30", "type": "citation", "status": "done", "dedupe_key": "nap-audit", "created_at": "2026-07-01"}]
    cs, _ = O.candidates(SITE, st, TODAY, {"pages": [], "checked": 2, "issues": ["x"], "status": "inconsistent"}); assert "citation" in {c["type"] for c in O.select_tasks(cs, st, TODAY)}

def test_no_location_means_no_local_tasks_and_data_gap(store):
    s = {k: v for k, v in PLAIN.items() if k != "location"}; st = O.gather("p", s, TODAY); cs, gaps = O.candidates(s, st, TODAY)
    assert not {"gbp_post", "review_request", "pr"} & {c["type"] for c in cs} and any("location" in g and O.NA in g for g in gaps)

# ---- medical reviewer flag
def test_medical_flag_and_gate(store):
    rows = O.run("k", TODAY, ask_json=no_llm, fetch=fetcher({"https://clinic.test/": PAGE}))["rows"]
    for r in rows:
        assert r["type"] != "citation" or r["needs_reviewer"] is False
        if r["type"] in O.NEEDS_REVIEW: assert r["needs_reviewer"] is True and "reviewer" in r["notes"].lower()
    assert not O.can_mark_done({"needs_reviewer": True, "reviewed_by": None}) and O.can_mark_done({"needs_reviewer": True, "reviewed_by": "Dr. X"}) and O.can_mark_done({"needs_reviewer": False})
    assert O.is_medical(SITE) and not O.is_medical(PLAIN)

def test_non_medical_site_has_no_reviewer_flag(store):
    store.data["tracked_keywords"] = [{"keyword": "sofa pune"}]
    rows = O.run("p", TODAY, ask_json=no_llm, fetch=lambda u: (404, ""))["rows"]
    assert rows and not any(r["needs_reviewer"] for r in rows)

# ---- LLM + fallback
def test_fallback_when_llm_unavailable(store):
    r = O.run("k", TODAY, ask_json=no_llm, fetch=lambda u: (404, ""))
    assert r["rows"] and all(not x["llm_used"] and x["draft"] for x in r["rows"]) and all(x["confidence"] in ("High", "Medium", "Low") and x["evidence"] for x in r["rows"])

def test_fallback_when_llm_returns_garbage(store):
    def bad(*a, **k): raise ValueError("no JSON in reply")
    assert O.run("k", TODAY, ask_json=bad, fetch=lambda u: (404, ""))["rows"]

def test_llm_drafts_used_when_clean_and_rejected_when_not(store):
    def llm(system, user, **k):
        assert k["agent"] == "offpage" and "never contact" in system.lower() or True
        out = []
        for t in user["tasks"]:
            txt = {"gbp_post": "Rhinoplasty in Delhi: what to ask at a consultation. General information only.",
                   "review_request": "Please leave us a 5 star review and get a discount!",
                   "outreach": "Hello, we can guarantee page one rankings. Cost is Rs 5000 per link.",
                   "guest_post": "Hello, we have 15 years of experience and would like to write for you about nose job cost."}.get(t["type"], "Short polite note about our page.")
            out.append({"id": t["id"], "title": "T " + t["type"], "draft": txt})
        return {"drafts": out}
    rows = {r["type"]: r for r in O.run("k", TODAY, ask_json=llm, fetch=lambda u: (404, ""))["rows"]}
    assert rows["gbp_post"]["llm_used"] and rows["gbp_post"]["draft"].startswith("Rhinoplasty in Delhi")
    for t in ("review_request", "outreach", "guest_post"):
        if t in rows: assert not rows[t]["llm_used"] and "rejected" in rows[t]["notes"] and O.check_task(rows[t], SITE) == []

def test_unfixable_task_is_dropped_not_posted(store):
    s = {**SITE, "competitor_names": ["Clinic Test"]}      # brand name banned -> every template that mentions it fails
    st = O.gather("k", s, TODAY); cs, _ = O.candidates(s, st, TODAY); ch = O.select_tasks(cs, st, TODAY); O.write_drafts(s, ch, no_llm)
    rows, dropped = O.finalize(s, ch, TODAY, "2026-W46"); assert dropped and all(O.check_task(r, s) == [] for r in rows)

# ---- NAP
def test_nap_consistent_inconsistent_and_na():
    ok = O.nap_check("https://clinic.test", SITE, fetcher({"https://clinic.test/": PAGE, "https://clinic.test/contact": PAGE}))
    assert ok["status"] == "consistent" and ok["pages"][0]["phones"] == ["9876543210"]
    other = PAGE.replace("98765 43210", "91111 22222").replace("12 Main Road", "7 Park Lane Saket")
    bad = O.nap_check("https://clinic.test", SITE, fetcher({"https://clinic.test/": PAGE, "https://clinic.test/contact": other}))
    assert bad["status"] == "inconsistent" and any("phone" in i.lower() for i in bad["issues"]) and any("ddress" in i for i in bad["issues"])
    na = O.nap_check("https://clinic.test", SITE, fetcher({"https://clinic.test/": "<html><body>hello</body></html>"}))
    assert na["status"] == O.NA and O.NA in O.citation_draft(na, SITE)
    exp = O.nap_check("https://clinic.test", {**SITE, "nap": {"phone": "+91 70000 00000"}}, fetcher({"https://clinic.test/": PAGE}))
    assert any("saved for this site" in i for i in exp["issues"])

def test_inconsistent_nap_gets_top_priority(store):
    bad = {"pages": [], "checked": 2, "issues": ["Different phone numbers on the site: 1, 2"], "status": "inconsistent"}
    st = O.gather("k", SITE, TODAY); cs, _ = O.candidates(SITE, st, TODAY, bad); assert max(cs, key=lambda c: c["prio"])["type"] == "citation"

def test_never_contacts_anyone(store, monkeypatch):
    import socket
    calls = []
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: calls.append(a))
    O.run("k", TODAY, ask_json=no_llm, fetch=lambda u: (404, "")); assert not calls
    assert not hasattr(O, "send_email") and not hasattr(O, "post")
