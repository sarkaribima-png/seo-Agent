import datetime as dt, json
import pytest
from seo_agent import strategist as s, sb

TODAY = dt.date(2026, 11, 10)
URL = "https://k.in/page"


def rows(start_off, n, impr, pos, url=URL, clicks=1):
    """gsc rows for n days, beginning start_off days before TODAY (older first)."""
    return [{"site_id": "k", "date": str(TODAY - dt.timedelta(days=start_off - i)), "page": url, "clicks": clicks, "impressions": impr, "position": pos} for i in range(n)]


def fu(age, **kw):
    applied = TODAY - dt.timedelta(days=age)
    d = {"id": 1, "proposal_id": 7, "site_id": "k", "url": URL, "kind": "title_meta", "applied_at": applied.isoformat() + "T08:00:00+00:00", "status": "open", "verdict": "too_early",
         "baseline": {"clicks": 28, "impressions": 140, "position": 12.0, "days": 14, "complete": True, "window": ["a", "b"]}}
    d.update(kw); return d


class Store:
    def __init__(self, data=None): self.data = data or {}; self.inserted = []; self.updated = []
    def select(self, t, **p):
        if t not in self.data: raise RuntimeError(f"404 relation {t}")
        rs = list(self.data[t])
        for k, v in p.items():
            if isinstance(v, str) and v.startswith("eq."): rs = [r for r in rs if str(r.get(k)) == v[3:]]
        return rs
    def insert(self, t, rows):
        out = [{**r, "id": 100 + len(self.inserted) + i} for i, r in enumerate(rows)]; self.inserted.append((t, out)); return out
    def update(self, t, m, d): self.updated.append((t, m, d))


@pytest.fixture
def st(monkeypatch):
    def mk(data):
        x = Store(data); monkeypatch.setattr(sb, "select", x.select); monkeypatch.setattr(sb, "insert", x.insert); monkeypatch.setattr(sb, "update", x.update); return x
    return mk


# ---- baseline capture
def test_baseline_capture_from_gsc_window():
    applied = TODAY - dt.timedelta(days=10)
    r = rows(24, 14, 10, 12.0, clicks=2) + rows(9, 5, 999, 1.0)           # the 14 days before + days after (must be excluded)
    b = s.baseline_for(r, applied, TODAY)
    assert b["impressions"] == 140 and b["clicks"] == 28 and b["position"] == 12.0 and b["complete"] is True and b["days_with_data"] == 14


def test_baseline_missing_is_data_not_available_not_zero():
    b = s.baseline_for([], TODAY - dt.timedelta(days=10), TODAY)
    assert b["impressions"] is None and b["note"] == s.NA and b["complete"] is False


def test_baseline_incomplete_when_change_is_fresh():
    assert s.baseline_for(rows(20, 3, 5, 9.0), TODAY, TODAY)["complete"] is False


def test_sync_new_creates_followup_with_baseline_and_skips_own(st):
    props = [{"id": 7, "site_id": "k", "page": URL, "kind": "title_meta", "status": "applied", "applied_at": (TODAY - dt.timedelta(days=5)).isoformat() + "T01:00:00+00:00"},
             {"id": 8, "site_id": "k", "page": URL, "kind": "strategy_followup", "status": "applied", "applied_at": TODAY.isoformat() + "T01:00:00+00:00"},
             {"id": 9, "site_id": "k", "page": URL, "kind": "schema", "status": "applied", "source": "strategist", "applied_at": TODAY.isoformat() + "T01:00:00+00:00"}]
    x = st({"proposals": props, "change_log_followups": []})
    new = s.sync_new("k", TODAY, rows(19, 14, 10, 12.0, clicks=2, url=URL + "/"))      # trailing slash must still match
    assert [n["proposal_id"] for n in new] == [7] and new[0]["baseline"]["impressions"] == 140
    new2 = s.sync_new("k", TODAY, [])                                          # idempotent only because table has it; simulate
    assert x.inserted[0][0] == "change_log_followups"


# ---- day windows
def test_day_windows_which_checks_fire(st):
    st({})
    g = {"autonomy_level": 2}
    up = s.process(fu(2), [], TODAY, g, [], None); assert up == {} or "check_day3" not in up
    up = s.process(fu(3), rows(3, 2, 5, 8.0), TODAY, g, [])
    assert "check_day3" in up and "check_day14" not in up and "check_day28" not in up and up["check_day3"]["status"] == "indexed"
    up = s.process(fu(14), rows(10, 8, 5, 8.0), TODAY, g, [])
    assert "check_day14" in up and "check_day28" not in up and "verdict" not in up
    up = s.process(fu(27), rows(20, 20, 5, 8.0), TODAY, g, [])
    assert "check_day28" not in up
    up = s.process(fu(28), rows(25, 25, 5, 8.0), TODAY, g, [])
    assert "check_day28" in up


def test_day3_prefers_url_inspection_and_falls_back(st):
    st({})
    r = s.check_day3(fu(3), [], TODAY, lambda u, f: {"indexed": True, "last_crawl": (TODAY - dt.timedelta(days=1)).isoformat()})
    assert r["status"] == "indexed" and r["crawled_after_change"] is True and r["source"] == "url_inspection"
    r = s.check_day3(fu(3), [], TODAY, lambda u, f: (_ for _ in ()).throw(RuntimeError("quota")))
    assert r["status"] == "unknown" and s.NA in r["note"]
    assert s.check_day3(fu(3), rows(2, 2, 4, 9.0), TODAY, None)["source"] == "gsc_impressions"


def test_day14_delta_and_zero_baseline_no_crash():
    r = s.check_day14(fu(14), rows(13, 12, 20, 8.0), TODAY)
    assert r["impr_per_day_before"] == 10.0 and r["impr_per_day_after"] == 20.0 and r["impr_delta_pct"] == 100.0
    z = fu(14, baseline={"impressions": 0, "days": 14, "position": None, "clicks": 0})
    r = s.check_day14(z, rows(13, 12, 20, 8.0), TODAY); assert r["impr_delta_pct"] is None and s.NA in r["note"]
    r = s.check_day14(fu(14, baseline={}), [], TODAY); assert r["impr_delta_pct"] is None


# ---- verdicts
def test_too_early_when_below_min_sample():
    r = s.verdict_for(fu(30, baseline={"clicks": 1, "impressions": 14, "position": 3.0, "days": 14, "complete": True}), rows(29, 28, 1, 3.0), [], TODAY)   # 28 impressions < 100, small baseline
    assert r["verdict"] == "too_early" and r["confidence"] == "Low"


def test_improved_flat_worse_and_hedged_text():
    page = lambda imp, pos: rows(29, 28, imp, pos)
    assert s.verdict_for(fu(30), page(10, 8.0), [], TODAY)["verdict"] == "improved"      # 270 impr, pos 12 -> 8
    assert s.verdict_for(fu(30), page(10, 12.2), [], TODAY)["verdict"] == "flat"           # similar rate, pos -0.2
    assert s.verdict_for(fu(30), page(10, 15.0), [], TODAY)["verdict"] == "worse"
    assert s.HEDGE in s.template_action("improved", fu(30), {}) and s.HEDGE in s.template_action("worse", fu(30), {}, action="reoptimize")
    assert "too_early" in s.template_action("too_early", fu(30), {"reason": "x"})


def test_collapse_with_big_baseline_is_worse_not_too_early():
    b = fu(30, baseline={"clicks": 80, "impressions": 2000, "position": 6.0, "days": 14, "complete": True})
    r = s.verdict_for(b, rows(29, 28, 1, 6.0), [], TODAY)               # 27 impr vs ~143/day expected
    assert r["verdict"] == "worse" and r["confidence"] == "Medium"


def test_site_wide_trend_downgrades_confidence():
    applied = TODAY - dt.timedelta(days=30)
    other = rows(44, 14, 100, 9.0, url="https://k.in/o") + rows(29, 28, 200, 9.0, url="https://k.in/o")
    r = s.verdict_for(fu(30), rows(29, 28, 10, 8.0), other, TODAY)
    assert r["site_impr_trend_pct"] is not None and r["confidence"] == "Low" and "season" in r["note"]


def test_no_division_by_zero_baseline_zero_or_missing():
    for base in ({"impressions": 0, "days": 14, "position": None, "clicks": 0, "complete": True}, {}, {"impressions": None, "days": 0}, {"impressions": 50, "days": 0, "position": 9}):
        r = s.verdict_for(fu(30, baseline=base), rows(29, 28, 10, 8.0), [], TODAY)
        assert r["verdict"] in ("improved", "flat", "worse", "too_early")
        r = s.verdict_for(fu(30, baseline=base), [], [], TODAY)
        assert r["verdict"] in ("too_early", s.NA, "worse")
        s.check_day14(fu(14, baseline=base), [], TODAY)
    assert s.verdict_for(fu(30, baseline={}), [], [], TODAY)["verdict"] == s.NA
    assert s.sdiv(5, 0) is None and s.sdiv(None, 3) is None and s.pct_change(5, 0) is None


def test_new_visibility_from_zero_baseline_is_improved_with_note():
    r = s.verdict_for(fu(30, baseline={"impressions": 0, "days": 14, "position": None, "clicks": 0, "complete": True}), rows(29, 28, 10, 8.0), [], TODAY)
    assert r["verdict"] == "improved" and s.NA in r["note"]


# ---- worse -> follow-up, flat -> lever, improved -> winner
def test_worse_creates_pending_followup_proposal(st):
    x = st({})
    props = [{"id": 7, "page": URL, "kind": "title_meta", "status": "applied", "backup_id": "b1", "post_id": 55}]
    up = s.process(fu(30), rows(29, 28, 10, 15.0), TODAY, {"autonomy_level": 2}, props, None, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no llm")))
    assert up["verdict"] == "worse" and up["followup_proposal_id"] and up["status"] == "closed" and up["meta"]["action"] == "rollback_suggestion" and up["meta"]["text_source"] == "template"
    t, rws = x.inserted[0]; assert t == "proposals" and rws[0]["status"] == "pending" and rws[0]["needs_approval"] is True and rws[0]["kind"] == "strategy_followup" and rws[0]["post_id"] == 55
    assert s.HEDGE in up["next_action"]


def test_worse_level1_observe_only_and_no_backup_means_reoptimize(st):
    x = st({})
    up = s.process(fu(30), rows(29, 28, 10, 15.0), TODAY, {"autonomy_level": 1}, [{"id": 7, "page": URL, "kind": "title_meta", "status": "applied"}], None, None)
    assert "followup_proposal_id" not in up and not x.inserted and up["meta"]["action"] == "reoptimize"


def test_flat_suggests_untried_lever_and_improved_logs_winner(st):
    st({})
    props = [{"id": 7, "page": URL, "kind": "title_meta", "status": "applied"}, {"id": 8, "page": URL, "kind": "internal_link", "status": "applied"}]
    up = s.process(fu(30), rows(29, 28, 10, 12.2), TODAY, {"autonomy_level": 2}, props, None, None)
    assert up["verdict"] == "flat" and up["meta"]["lever"] == "content_add"
    up = s.process(fu(30), rows(29, 28, 10, 8.0), TODAY, {"autonomy_level": 2}, props, None, None)
    assert up["verdict"] == "improved" and up["winner_pattern"]["kind"] == "title_meta" and "causation" in up["winner_pattern"]["note"]


def test_too_early_stays_open_until_day45(st):
    st({})
    small = {"clicks": 1, "impressions": 14, "position": 8.0, "days": 14, "complete": True}
    up = s.process(fu(30, baseline=small), rows(29, 28, 1, 8.0), TODAY, {}, [], None, None)
    assert up["verdict"] == "too_early" and "status" not in up
    up = s.process(fu(46, baseline=small), rows(29, 28, 1, 8.0), TODAY, {}, [], None, None); assert up["status"] == "closed"


# ---- LLM guard
def test_llm_text_accepted_only_if_safe():
    r = {"verdict": "improved", "confidence": "Medium", "post_impressions": 270, "base_impressions": 140, "pos_delta": 4.0, "impr_delta_pct": 100.0, "post_days": 27}
    f = fu(30)
    ok = lambda *a, **k: {"next_action": "Position 4.0 behtar dikha, ye change kaam kar gaya ho sakta hai. Isi tarah dusre pages par proposal banao."}
    assert s.llm_action(f, r, None, None, ok).startswith("Position")
    for bad in ("Position 7.0 behtar, ho sakta hai.", "Guaranteed ranking, ho sakta hai.", "Ye change ki wajah se hua. Position 4.0.", "Position 4.0 behtar dikha.",   # new number / guarantee / causal / no hedge
                "Isi wajah se improvement ho sakta hai"):
        assert s.llm_action(f, r, None, None, lambda *a, **k: {"next_action": bad}) is None
    assert s.llm_action(f, r, None, None, lambda *a, **k: "not a dict") is None
    assert s.llm_action(f, r, None, None, lambda *a, **k: (_ for _ in ()).throw(s.__dict__["json"].JSONDecodeError("x", "y", 0))) is None


def test_run_end_to_end_missing_table_and_paused(st):
    st({"sites": [{"id": "k", "mode": "paused"}]}); assert s.run("k", TODAY, inspect=None)["skipped"] == "paused"
    st({"sites": [{"id": "k", "mode": "dry-run"}]}); assert "schema_v24" in s.run("k", TODAY, inspect=None)["error"]


def test_run_end_to_end_day28(st):
    applied = (TODAY - dt.timedelta(days=30)).isoformat() + "T01:00:00+00:00"
    data = {"sites": [{"id": "k", "mode": "dry-run"}], "site_goals": [], "change_log_followups": [], "gsc_daily": rows(44, 14, 10, 12.0, clicks=2) + rows(29, 28, 10, 8.0),
            "proposals": [{"id": 7, "site_id": "k", "page": URL, "kind": "title_meta", "status": "applied", "applied_at": applied, "backup_id": "b"}]}
    x = st(data)
    orig = x.select
    state = {"n": 0}
    def sel(t, **p):                                                    # after sync_new inserts, the followup table must show the new row
        if t == "change_log_followups" and x.inserted: return [{**r, "status": "open", "verdict": "too_early"} for tt, rs in x.inserted if tt == t for r in rs]
        return orig(t, **p)
    sb.select = sel
    out = s.run("k", TODAY, inspect=None, ask_json=lambda *a, **k: {"next_action": "x"})
    assert out["new_baselines"] == 1 and out["verdicts"].get("improved") == 1


# ---- goals prioritizer
def T(task, agent="Content & On-Page Optimizer", impact="med", effort="low"): return {"task": task, "agent": agent, "impact": impact, "effort": effort, "needs_approval": False, "_n": 1}


def test_goal_boosts_money_page_keyword_and_post_cap():
    g = {"target_keywords": ["Tummy Tuck Delhi"], "money_pages": ["https://k.in/tummy-tuck"], "monthly_post_cap": 4, "goal_text": "more consultations"}
    ts = [T("Fix title on https://k.in/tummy-tuck"), T("Improve tummy tuck delhi FAQ"), T("Fix alt text on gallery"), T("Write new blog post about botox")]
    kept, notes = s.goal_boosts(ts, g, {"internal_link": 2}, posts_this_month=4)
    assert [t["_boost"] for t in kept] == [4, 2, 0] and any("cap" in n for n in notes) and not any("botox" in t["task"] for t in kept)
    kept, _ = s.goal_boosts([T("Write new blog post about botox")], g, None, posts_this_month=3); assert len(kept) == 1
    kept, _ = s.goal_boosts([T("Add internal link to services")], g, {"internal_link": 2}); assert kept[0]["_boost"] == 1
    kept, notes = s.goal_boosts(ts, None, None); assert len(kept) == 4 and all(t["_boost"] == 0 for t in kept)


def test_apply_goals_fails_open(st):
    st({})                                                               # site_goals table missing -> defaults, no crash
    ts = [T("Fix h1")]; assert s.apply_goals(ts, "k", posts_this_month=0) == ts
    monkey = s.load_goals; s.load_goals = lambda *_: (_ for _ in ()).throw(RuntimeError("boom"))
    try: assert s.apply_goals(ts, "k") == ts
    finally: s.load_goals = monkey
