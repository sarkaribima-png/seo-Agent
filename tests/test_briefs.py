import copy, json, pytest
from seo_agent import briefs, run as runmod

MED = {"id": "k", "profile": "medical", "banned_terms": ["guarantee", "FDA"], "competitor_names": ["Acme"], "reviewer_required": True}
GEN = {"id": "g", "profile": "general", "banned_terms": [], "competitor_names": []}
CANDS = [{"url": "https://k.in/gynecomastia-delhi", "topic": "Gynecomastia surgery in Delhi"}, {"url": "https://k.in/non-surgical-gynecomastia", "topic": "Non surgical options"},
         {"url": "https://k.in/recovery-guide", "topic": "Recovery after chest surgery"}]
META = "A plain guide to how long gynecomastia recovery takes, what affects it, and when to ask your surgeon a question."
GOOD = {"target_keyword": "x", "secondary_keywords": ["recovery time", "after surgery"], "intent": "informational", "page_type": "recovery",
        "working_title": "Gynecomastia Recovery Time: What to Expect | Kalosa", "h1": "Gynecomastia recovery time", "meta_description_draft": META,
        "outline": [{"h2": h, "h3": [], "must_cover": "m"} for h in ("The first week", "Weeks two to six", "Going back to work", "Compression garments", "When to call the surgeon")],
        "questions": ["How long is recovery?", "When can I drive?", "When can I exercise?", "Is swelling normal?"], "entities": ["compression vest", "drain", "swelling", "seroma", "follow-up visit"],
        "internal_links": [{"link_to_url": "https://k.in/gynecomastia-delhi", "anchor_text": "gynecomastia surgery", "why": "main service page"}, {"link_to_url": "https://k.in/recovery-guide", "anchor_text": "recovery guide", "why": "related"}],
        "schema_type": "MedicalWebPage", "schema_notes": "match visible text", "eeat": [{"need": "Doctor review", "owner": "doctor", "why": "medical topic"}], "source_types": ["a clinical guideline"],
        "claims_to_confirm": ["return to work timeline"], "avoid": ["guaranteed results"], "length_note": "DATA NOT AVAILABLE", "confidence": "medium", "missing_data": []}


def ask_with(*replies):
    calls = []; seq = list(replies)
    def f(system, user, max_tokens=0): calls.append(json.loads(user)); r = seq.pop(0) if len(seq) > 1 else seq[0]; return r if isinstance(r, str) else json.dumps(r)
    f.calls = calls; return f


def test_make_brief_medical():
    b, err = briefs.make_brief(MED, "gynecomastia recovery time", None, CANDS, ask_with(GOOD), {"facts_and_references": []})
    assert err is None and b["target_keyword"] == "gynecomastia recovery time" and b["recommendation"] == "New page." and b["cannibalization"] == []
    assert b["ymyl"]["flag"] and "return to work timeline" in b["ymyl"]["doctor_to_confirm"] and "risks and side effects" in b["ymyl"]["doctor_to_confirm"]
    assert b["serp_notes"].startswith("DATA NOT AVAILABLE") and len(b["internal_links"]) == 2


def test_general_site_has_no_ymyl_flag():
    b, _ = briefs.make_brief(GEN, "recovery time", None, CANDS, ask_with(GOOD)); assert "ymyl" not in b


def test_cannibalization_points_to_existing_page():
    b, _ = briefs.make_brief(MED, "gynecomastia surgery delhi", None, CANDS, ask_with(GOOD))
    assert b["cannibalization"][0]["url"] == "https://k.in/gynecomastia-delhi" and "Improve that page" in b["recommendation"]


@pytest.mark.parametrize("mut", [lambda b: b.update(intent="fun"), lambda b: b.update(working_title="Best Gynecomastia Surgeon | Kalosa Clinic"), lambda b: b.update(schema_type="Review"),
                                 lambda b: b.update(schema_type="AggregateRating"), lambda b: b.update(outline=b["outline"][:2]), lambda b: b.update(questions=["only one?"]),
                                 lambda b: b.update(entities=["a", "b"]), lambda b: b.update(internal_links=[{"link_to_url": "https://evil.com/x", "anchor_text": "click here now", "why": ""}] * 2),
                                 lambda b: b["outline"][0].update(must_cover="Acme clinic explains this"), lambda b: b["outline"][0].update(h2="Guaranteed recovery in 10 days"),
                                 lambda b: b["outline"][1].update(must_cover="Most people recover in 90% of cases"), lambda b: b.update(meta_description_draft="Short.")])
def test_bad_briefs_are_rejected(mut):
    bad = copy.deepcopy(GOOD); mut(bad)
    ask = ask_with(bad); b, err = briefs.make_brief(MED, "gynecomastia recovery time", None, CANDS, ask)
    assert b is None and err.startswith("brief failed checks") and len(ask.calls) == 3 and ask.calls[1]["previous_errors"]


def test_retry_recovers_and_unknown_links_are_dropped():
    bad = copy.deepcopy(GOOD); bad["questions"] = []
    ask = ask_with("not json", bad, GOOD); b, err = briefs.make_brief(MED, "k", None, CANDS, ask); assert b and len(ask.calls) == 3
    odd = copy.deepcopy(GOOD); odd["internal_links"].append({"link_to_url": "https://other.com/p", "anchor_text": "other page", "why": ""})
    b, _ = briefs.make_brief(MED, "k", None, CANDS, ask_with(odd)); assert [l["link_to_url"] for l in b["internal_links"]] == ["https://k.in/gynecomastia-delhi", "https://k.in/recovery-guide"]


def test_avoid_list_may_name_risky_words():
    b, err = briefs.make_brief(MED, "k", None, CANDS, ask_with(GOOD)); assert b                      # 'avoid' contains 'guaranteed results' and is allowed
    assert briefs.make_brief(MED, "", None, CANDS, ask_with(GOOD)) == (None, "no keyword")


def test_run_stores_draft_briefs_for_ideas_without_one(monkeypatch):
    ins = []
    def select(t, **p):
        if t == "sites": return [{"id": "k", "mode": "dry-run", "profile": "medical"}]
        if t == "page_audits": return [{"page": c["url"], "raw": {"title": c["topic"]}} for c in CANDS] * 2
        if t == "content_ideas": return [{"id": 1, "primary_keyword": "gynecomastia recovery time", "status": "approved"}, {"id": 2, "primary_keyword": "already done", "status": "suggested"},
                                         {"id": 3, "primary_keyword": "gynecomastia causes", "status": "suggested"}]
        if t == "content_briefs": return [{"idea_id": 2}]
        return []
    monkeypatch.setattr(briefs.sb, "select", select); monkeypatch.setattr(briefs.sb, "insert", lambda t, rows: ins.append((t, rows)) or rows)
    monkeypatch.setattr(runmod, "sites", lambda: {"k": {"id": "k", "banned_terms": ["FDA"], "competitor_names": []}}); monkeypatch.setattr(briefs, "ask", ask_with(GOOD))
    import seo_agent.content as c; monkeypatch.setattr(c, "brain_context", lambda *a: {})
    briefs.run("k", 5)
    rows = [r for t, rr in ins if t == "content_briefs" for r in rr]
    assert [r["idea_id"] for r in rows] == [1, 3] and all(r["status"] == "draft" and r["ymyl"] and r["schema_type"] == "MedicalWebPage" for r in rows)
    ins.clear(); briefs.run("k", 5, "typed keyword")
    assert ins[0][1][0]["idea_id"] is None and ins[0][1][0]["keyword"] == "typed keyword"


def test_candidates_use_title_or_heading_or_url(monkeypatch):
    rows = [{"page": "https://k.in/", "raw": {"title": "Home"}}, {"page": "https://k.in/a-b/", "raw": {"title": "", "headings": [{"level": "h1", "text": "Heading A"}]}},
            {"page": "https://k.in/c-d", "raw": {}}, {"page": "https://k.in/c-d/", "raw": {}}]
    monkeypatch.setattr(briefs.sb, "select", lambda t, **p: rows)
    assert briefs.candidates("k") == [{"url": "https://k.in/a-b/", "topic": "Heading A"}, {"url": "https://k.in/c-d", "topic": "c d"}]
