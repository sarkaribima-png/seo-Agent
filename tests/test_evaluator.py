import datetime as dt
import pytest
from seo_agent import evaluator as ev, sb, runlog, llm

TODAY = dt.date(2026, 11, 9)   # Monday, ISO week 46
NA = ev.NA
iso = lambda d: d.isoformat() + "T08:00:00+00:00"


class Store:
    """Fake Supabase. Tables missing from `data` raise like PostgREST for an unknown table; page=eq.x filters gsc_daily."""
    def __init__(self, data): self.data = data; self.saved = []; self.inserted = []
    def select(self, t, **p):
        if t not in self.data: raise RuntimeError(f"404 relation {t} does not exist")
        rows = list(self.data[t])
        if "page" in p and p["page"].startswith("eq."): rows = [r for r in rows if r.get("page") == p["page"][3:]]
        if int(p.get("offset", 0)) > 0: return []
        return rows
    def upsert(self, t, rows, on, chunk=1000): self.saved.append((t, rows, on))
    def insert(self, t, rows): self.inserted.append((t, rows)); return rows


def install(data):
    s = Store(data); sb.select, sb.upsert, sb.insert = s.select, s.upsert, s.insert; return s


def prop(i, status, kind="title_meta", created=30, decided=None, applied=None, reason=None, source=None, page="https://x.in/a", needs=True):
    c = dt.datetime(TODAY.year, TODAY.month, TODAY.day, 8, tzinfo=dt.timezone.utc) - dt.timedelta(days=created)
    return {"id": i, "page": page, "kind": kind, "status": status, "reason": reason, "source": source, "needs_approval": needs, "created_at": c.isoformat(),
            "decided_at": (c + dt.timedelta(hours=decided)).isoformat() if decided is not None else None, "applied_at": iso(TODAY - dt.timedelta(days=applied)) if applied is not None else None}


def gsc_page(page, applied_days_ago, before_pd, after_pd, pos_b=10.0, pos_a=8.0):
    a = TODAY - dt.timedelta(days=applied_days_ago); rows = []
    for i in range(1, 15): rows.append({"page": page, "date": (a - dt.timedelta(days=i)).isoformat(), "clicks": before_pd, "impressions": before_pd * 10, "position": pos_b})
    for i in range(1, 15):
        d = a + dt.timedelta(days=i)
        if d <= TODAY - dt.timedelta(days=2): rows.append({"page": page, "date": d.isoformat(), "clicks": after_pd, "impressions": after_pd * 10, "position": pos_a})
    return rows


def site_days(applied_days_ago, n_before=14):
    a = TODAY - dt.timedelta(days=applied_days_ago)
    return {(a + dt.timedelta(days=i)).isoformat() for i in range(-n_before, 15) if a + dt.timedelta(days=i) <= TODAY - dt.timedelta(days=2)}


# ---------------------------------------------------------------- metrics math
def test_proposal_stats_math():
    ps = [prop(1, "applied", decided=2, applied=5), prop(2, "applied", decided=4, applied=5), prop(3, "rolled_back", decided=1), prop(4, "rejected", decided=6),
          prop(5, "pending"), prop(6, "failed", reason="change was saved but did not show on the live page (cache)", decided=3),
          prop(7, "failed", reason="rollback: boom", decided=3), prop(8, "applied", needs=False, applied=3), prop(9, "failed", reason="404 from plugin", decided=1)]
    s = ev.proposal_stats(ps)
    assert (s["created"], s["pending"], s["rejected"], s["applied"], s["rolled_back"], s["auto_applied"]) == (9, 1, 1, 3, 1, 1)
    assert s["failed"] == 2 and s["verify_failed"] == 1 and s["rollback_failed"] == 1
    assert s["approved"] == 6 and s["decided"] == 7 and s["approval_rate"] == round(6 / 7, 3)
    assert s["applied_ever"] == 4 and s["rollback_rate"] == 0.25
    assert s["tried"] == 6 and s["failed_rate"] == round(2 / 6, 3) and s["verify_failed_rate"] == round(1 / 6, 3)
    assert s["median_hours_to_approve"] == 3.0 and s["tta_n"] == 5


def test_time_to_approve_excludes_rollbacks_and_auto():
    ps = [prop(1, "applied", decided=2), prop(2, "applied", decided=6), prop(3, "rolled_back", decided=500), prop(4, "applied", needs=False)]
    s = ev.proposal_stats(ps)
    assert s["tta_n"] == 2 and s["median_hours_to_approve"] == 4.0


def test_agent_attribution():
    assert ev.agent_of({"source": "onpage_batch", "kind": "title_meta"}) == "content_optimizer"
    assert ev.agent_of({"kind": "title_meta"}) == "daily_optimizer"
    assert ev.agent_of({"kind": "alt_text"}) == "content_optimizer"
    assert ev.agent_of({"agent": "technical_fixer", "kind": "schema"}) == "technical_fixer"
    assert ev.agent_of({"kind": "weird"}) == "unknown"


def test_cost_and_run_stats():
    ws, ms = ev.parse_ts("2026-10-12"), ev.parse_ts("2026-11-01")
    u = [{"agent": "a", "cost_usd": 1.0, "created_at": iso(dt.date(2026, 10, 20))}, {"agent": "a", "cost_usd": 0.5, "created_at": iso(dt.date(2026, 11, 3))}, {"agent": "b", "cost_usd": 9, "created_at": iso(dt.date(2026, 11, 3))}]
    c = ev.cost_stats(u, ms, ws, "a", 3)
    assert c["cost_usd_window"] == 1.5 and c["cost_usd_month"] == 0.5 and c["cost_per_applied_change"] == 0.5 and c["calls_window"] == 2
    runs = [{"agent": "tech_audit", "ok": True, "seconds": 10, "created_at": iso(dt.date(2026, 11, 2))}, {"agent": "technical_auditor", "ok": False, "seconds": 30, "detail": "boom", "created_at": iso(dt.date(2026, 11, 3))}]
    r = ev.run_stats(runs, "technical_auditor", ws)
    assert r["runs"] == 2 and r["run_success_rate"] == 0.5 and r["median_seconds"] == 20.0 and r["last_failure"] == "boom"


# ---------------------------------------------------------------- zero division / empty
def test_zero_division_everywhere():
    s = ev.proposal_stats([])
    assert s["approval_rate"] is None and s["rollback_rate"] is None and s["failed_rate"] is None and s["median_hours_to_approve"] is None
    assert ev.cost_stats([], ev.parse_ts("2026-11-01"), ev.parse_ts("2026-10-12"), "a", 0)["cost_per_applied_change"] is None
    assert ev.run_stats([], "a", ev.parse_ts("2026-10-12"))["run_success_rate"] is None
    assert ev.lift_stats([])["win_rate"] is None and ev.kw_stats([])["conversion_rate"] is None
    assert ev.pct_change(5, 0) is None and ev.safe_div(1, 0) is None and ev.median([]) is None
    assert ev.site_click_stats([], TODAY) is None and ev.index_stats([], [])["median_days_to_index"] is None


def test_lift_with_zero_clicks_before():
    p = {"id": 1, "page": "u", "kind": "schema", "applied_at": iso(TODAY - dt.timedelta(days=20))}
    c = ev.measure_change(p, gsc_page("u", 20, 0, 3), site_days(20), TODAY)
    assert c["status"] == "measured" and c["pct_clicks"] is None and c["verdict"] == "win"


# ---------------------------------------------------------------- lift / measure_change
def test_measure_change_verdicts_and_values():
    mk = lambda b, a: ev.measure_change({"id": 1, "page": "u", "kind": "schema", "applied_at": iso(TODAY - dt.timedelta(days=25))}, gsc_page("u", 25, b, a), site_days(25), TODAY)
    w, l, f = mk(10, 14), mk(10, 6), mk(10, 10.5)
    assert w["verdict"] == "win" and w["delta_clicks_pd"] == 4.0 and w["pct_clicks"] == 0.4 and w["position_gain"] == 2.0
    assert l["verdict"] == "loss" and f["verdict"] == "flat"
    assert w["before"]["days"] == 14 and w["after"]["days"] == 14 and w["confidence"] == "High"     # 140+196 clicks


def test_measure_change_too_early_and_no_coverage():
    p = {"id": 1, "page": "u", "kind": "schema", "applied_at": iso(TODAY - dt.timedelta(days=5))}
    assert ev.measure_change(p, gsc_page("u", 5, 5, 6), site_days(5), TODAY)["status"] == "too_early"
    p["applied_at"] = iso(TODAY - dt.timedelta(days=25))
    assert ev.measure_change(p, gsc_page("u", 25, 5, 6), set(), TODAY)["status"] == "no_gsc_coverage"


def test_tiny_lift_sample_is_low_confidence():
    c = ev.measure_change({"id": 1, "page": "u", "kind": "schema", "applied_at": iso(TODAY - dt.timedelta(days=25))}, gsc_page("u", 25, 0.1, 0.4), site_days(25), TODAY)
    assert c["confidence"] == "Low"


def test_confounded_changes_not_ranked():
    a = {"id": 1, "page": "u", "kind": "schema", "applied_at": iso(TODAY - dt.timedelta(days=25)), "status": "applied"}
    b = {"id": 2, "page": "u", "kind": "title_meta", "applied_at": iso(TODAY - dt.timedelta(days=22)), "status": "applied"}
    c = {"id": 3, "page": "other", "kind": "title_meta", "applied_at": iso(TODAY - dt.timedelta(days=25)), "status": "applied"}
    ch = [ev.measure_change(x, gsc_page(x["page"], 25 if x["id"] != 2 else 22, 10, 14), site_days(25) | site_days(22), TODAY) for x in (a, b, c)]
    ev.mark_confounded(ch, [a, b, c])
    assert [x["confounded"] for x in ch] == [True, True, False]
    ls = ev.lift_stats(ch); wins, worst = ev.top_changes(ch)
    assert ls["measured"] == 1 and ls["confounded"] == 2 and [w["id"] for w in wins] == [3] and worst == []


# ---------------------------------------------------------------- grades
@pytest.mark.parametrize("v,exp", [(0.95, "A"), (0.9499, "B"), (0.85, "B"), (0.70, "C"), (0.69, "D"), (None, None)])
def test_grade_value_hi(v, exp): assert ev.grade_value(v, ev.CFG["grade_rules"]["run_success_rate"]) == exp

@pytest.mark.parametrize("v,exp", [(0.0, "A"), (0.03, "A"), (0.0301, "B"), (0.10, "B"), (0.20, "C"), (0.21, "D")])
def test_grade_value_lo(v, exp): assert ev.grade_value(v, ev.CFG["grade_rules"]["rollback_rate"]) == exp

def test_grade_value_negative_thresholds():
    r = ev.CFG["grade_rules"]["tech_open_change"]
    assert [ev.grade_value(v, r) for v in (-0.3, -0.2, -0.1, 0.0, 0.2, 0.5)] == ["A", "A", "B", "B", "C", "D"]

def test_grade_card_mean_and_cutoffs():
    names = ev.CFG["agent_metrics"]
    g, letters, conf = ev.grade_card({"run_success_rate": (1.0, 40), "failed_rate": (0.0, 40), "rollback_rate": (0.0, 40), "approval_rate": (0.9, 40), "win_rate": (0.9, 40)}, names)
    assert g == "A" and conf == "High" and len(letters) == 5
    g2, _, _ = ev.grade_card({"run_success_rate": (1.0, 40), "failed_rate": (0.5, 40)}, names)       # A(4) + D(1) = 2.5 -> B
    assert g2 == "B"
    g3, _, _ = ev.grade_card({"run_success_rate": (0.6, 40), "failed_rate": (0.5, 40)}, names)
    assert g3 == "D"

def test_grade_card_not_enough_data():
    assert ev.grade_card({}, ev.CFG["agent_metrics"])[0] == NA
    assert ev.grade_card({"failed_rate": (0.9, 2)}, ev.CFG["agent_metrics"])[0] == NA     # n below min_graded_n: shown, not graded
    assert ev.grade_card({"failed_rate": (None, 50)}, ev.CFG["agent_metrics"])[0] == NA

def test_tiny_sample_marks_low_confidence():
    g, _, conf = ev.grade_card({"failed_rate": (0.0, 4)}, ev.CFG["agent_metrics"])
    assert g == "A" and conf == "Low"
    assert ev.confidence(10) == "Medium" and ev.confidence(20) == "High" and ev.confidence(9) == "Low" and ev.confidence(0) == "Low" and ev.confidence(None) == "Low"

def test_config_is_editable_not_hardcoded():
    cfg = {**ev.CFG, "grade_rules": {**ev.CFG["grade_rules"], "failed_rate": ("lo", 0.5, 0.6, 0.7)}}
    assert ev.grade_card({"failed_rate": (0.4, 10)}, ("failed_rate",), cfg)[0] == "A"
    assert ev.grade_card({"failed_rate": (0.4, 10)}, ("failed_rate",))[0] == "D"


# ---------------------------------------------------------------- other measurements
def test_index_stats():
    posts = [{"id": 1, "published_at": "2026-10-01T00:00:00Z"}, {"id": 2, "published_at": "2026-10-01T00:00:00Z"}, {"id": 3, "published_at": "2026-10-05T00:00:00Z"}, {"id": 4, "published_at": "2026-10-05"}]
    perf = [{"post_id": 1, "day": "2026-10-03", "indexed": False}, {"post_id": 1, "day": "2026-10-06", "indexed": True}, {"post_id": 1, "day": "2026-10-09", "indexed": True},
            {"post_id": 2, "day": "2026-10-11", "indexed": True}, {"post_id": 3, "day": "2026-10-20", "indexed": False}]
    s = ev.index_stats(posts, perf)
    assert s["indexed"] == 2 and s["median_days_to_index"] == 7.5 and s["published_not_indexed_yet"] == 1 and s["never_checked"] == 1
    assert ev.index_stats(None, perf) is None

def test_tech_trend_and_stats():
    rows = [{"status": "open", "severity": "High", "found_at": iso(TODAY - dt.timedelta(days=1))}, {"status": "open", "severity": "Low", "found_at": iso(TODAY - dt.timedelta(days=30))}, {"status": "fixed"}, {"status": "ignored"}]
    t = ev.tech_stats(rows, TODAY)
    assert t == {"open": 2, "fixed": 1, "ignored": 1, "superseded": 0, "open_critical_high": 1, "found_last_7d": 1} and ev.tech_stats(None, TODAY) is None
    prior = [{"week": f"2026-W{w}", "metrics": {"tech": {"open": o}}} for w, o in ((43, 10), (44, 8), (45, 8))]
    tr = ev.tech_trend(prior, 6)
    assert tr["open_change"] == -0.4 and tr["since_week"] == "2026-W43"
    assert ev.tech_trend([], 6)["open_change"] is None and ev.tech_trend(prior, None)["open_change"] is None

def test_keyword_conversion():
    k = ev.kw_stats([{"status": s} for s in ("new", "new", "approved", "fed", "rejected", "rejected")])
    assert k["converted"] == 2 and k["conversion_rate"] == 0.5 and k["decided"] == 4

def test_site_click_stats_needs_enough_days_and_clicks():
    end = TODAY - dt.timedelta(days=2)
    rows = [{"date": (end - dt.timedelta(days=i)).isoformat(), "clicks": 5 if i < 28 else 4, "impressions": 100, "position": 8} for i in range(56)]
    s = ev.site_click_stats(rows, TODAY)
    assert s["clicks_change_28d"] == 0.25 and s["position_change_28d"] == 0.0
    assert ev.site_click_stats(rows[:30], TODAY)["clicks_change_28d"] is None
    small = [{**r, "clicks": 0.5} for r in rows]
    s2 = ev.site_click_stats(small, TODAY)
    assert s2["clicks_change_28d"] is None and "not graded" in s2["note"]


# ---------------------------------------------------------------- run log helper
def test_log_run_never_raises_and_logs():
    s = install({}); assert runlog.log_run("a", "k", True, 1.234, "ok") is True
    row = s.inserted[0][1][0]; assert row == {"agent": "a", "site_id": "k", "ok": True, "seconds": 1.23, "detail": "ok"}
    def boom(*a, **k): raise RuntimeError("table agent_runs missing")
    sb.insert = boom; assert runlog.log_run("a", None, False, None) is False

def test_logged_context_records_failure_and_reraises():
    s = install({})
    with runlog.logged("a", "k"): pass
    with pytest.raises(ValueError):
        with runlog.logged("a", "k"): raise ValueError("bad")
    oks = [r[1][0]["ok"] for r in s.inserted]; assert oks == [True, False] and "ValueError: bad" in s.inserted[1][1][0]["detail"]


# ---------------------------------------------------------------- summary
def _site_with_data():
    ps = [prop(i, "applied", decided=3, applied=25, page=f"https://x.in/p{i}", created=28) for i in range(1, 5)] + [prop(5, "rejected", decided=1), prop(6, "rolled_back", decided=1)]
    gsc_d = []
    for i in range(1, 5): gsc_d += gsc_page(f"https://x.in/p{i}", 25, 10, 14 if i < 4 else 6)
    end = TODAY - dt.timedelta(days=2)
    return {"sites": [{"id": "k"}], "proposals": ps, "gsc_daily": gsc_d, "gsc_by_day": [{"date": (end - dt.timedelta(days=i)).isoformat(), "clicks": 5, "impressions": 100, "position": 8} for i in range(80)],
            "tech_issues": [{"status": "open", "severity": "High", "found_at": iso(TODAY)}], "keyword_opps": [{"status": "new"}], "reports": [{"created_at": iso(TODAY), "period": "week"}],
            "llm_usage": [{"agent": "content_optimizer", "cost_usd": 0.4, "created_at": iso(TODAY - dt.timedelta(days=2))}],
            "agent_runs": [{"agent": "onpage", "ok": True, "seconds": 5, "created_at": iso(TODAY - dt.timedelta(days=3))}] * 3,
            "content_posts": [], "post_perf": [], "agent_scorecards": []}

def test_template_summary_is_five_lines_no_llm():
    s = install(_site_with_data()); rows, ch, cards, sc = ev.evaluate_site("k", TODAY, None)
    text = [r for r in rows if r["agent"] == "site_outcomes"][0]["metrics"]["owner_summary"]
    assert len(text.splitlines()) == 5 and "correlation" in text and "$0.40" in text
    assert sc["metrics"]["lift"]["measured"] == 4 and sc["metrics"]["lift"]["wins"] == 3 and sc["metrics"]["lift"]["losses"] == 1

def test_llm_summary_accepted_only_if_no_new_numbers_and_5_lines():
    base = "\n".join(ev.template_summary("k", "2026-W46", {"grade": "B", "metrics": {"confidence": "High", "window_days": 28, "proposals": NA, "lift": NA, "tech": NA, "gsc": NA, "cost_month_usd": 1.5, "tech_trend": {"open_change": None}}}, {}))
    good = "\n".join(f"line {w}" for w in "abcde")
    sc = {"grade": "B", "metrics": {"confidence": "High", "window_days": 28, "proposals": NA, "lift": NA, "tech": NA, "gsc": NA, "cost_month_usd": 1.5, "tech_trend": {"open_change": None}}}
    t, used = ev.owner_summary("k", "2026-W46", sc, {}, lambda s, u: good); assert used and t == good
    t, used = ev.owner_summary("k", "2026-W46", sc, {}, lambda s, u: good.replace("line a", "line 99")); assert not used and t == base      # invented number
    t, used = ev.owner_summary("k", "2026-W46", sc, {}, lambda s, u: "one line only"); assert not used and t == base
    t, used = ev.owner_summary("k", "2026-W46", sc, {}, lambda s, u: good.replace("line b", "100% guarantee")); assert not used
    def down(s, u): raise llm.LLMUnavailable("no key")
    t, used = ev.owner_summary("k", "2026-W46", sc, {}, down); assert not used and t == base

def test_default_ask_falls_back_without_key(monkeypatch):
    monkeypatch.setenv("LLM_DISABLED", "1"); install(_site_with_data())
    rows = ev.run(["k"], TODAY, write=False, ask=ev.default_ask)
    assert [r for r in rows if r["agent"] == "site_outcomes"][0]["metrics"]["summary_llm"] is False


# ---------------------------------------------------------------- missing tables / end to end
def test_missing_tables_never_crash_and_say_not_available():
    s = install({"sites": [{"id": "k"}]})        # every other table missing
    rows = ev.run(["k"], TODAY, write=True, ask=None)
    assert s.saved and s.saved[0][0] == "agent_scorecards" and s.saved[0][2] == "week,scope,agent"
    site = [r for r in rows if r["agent"] == "site_outcomes"][0]
    assert site["grade"] == NA and site["metrics"]["tech"] == NA and site["metrics"]["gsc"] == NA and site["metrics"]["indexing"] == NA and site["metrics"]["cost_month_usd"] == NA
    assert "agent_runs table missing" in [r for r in rows if r["agent"] == "technical_auditor"][0]["notes"]
    assert all(r["grade"] == NA for r in rows)
    assert {r["scope"] for r in rows} == {"k", "ALL"} and all(r["week"] == "2026-W46" for r in rows)
    assert len(site["metrics"]["owner_summary"].splitlines()) == 5

def test_all_sites_row_has_null_site_id_and_agent_grades():
    d = _site_with_data(); d["proposals"] = d["proposals"] + [prop(20 + i, "applied", decided=2, applied=10, page=f"https://x.in/q{i}", kind="schema", source="onpage") for i in range(12)]
    install(d); rows = ev.run(["k"], TODAY, write=False, ask=None)
    g = {(r["scope"], r["agent"]): r for r in rows}
    assert g[("ALL", "content_optimizer")]["site_id"] is None and g[("k", "content_optimizer")]["site_id"] == "k"
    co = g[("k", "content_optimizer")]["metrics"]
    assert co["proposals"]["applied"] == 12 and co["cost"]["cost_usd_month"] == 0.4 and co["cost"]["cost_per_applied_change"] == round(0.4 / 12, 4)
    assert co["runs"]["runs"] == 3 and co["runs"]["run_success_rate"] == 1.0 and g[("k", "content_optimizer")]["grade"] in "ABCD"
    assert g[("k", "rank_reporter")]["metrics"]["last_report_age_days"] == 0
    assert g[("k", "keyword_researcher")]["metrics"]["keyword_opps"]["new"] == 1

def test_one_failing_site_does_not_stop_others():
    install(_site_with_data()); calls = []
    real = ev.evaluate_site
    def flaky(sid, today, ask=None, cfg=ev.CFG):
        calls.append(sid)
        if sid == "bad": raise RuntimeError("boom")
        return real(sid, today, ask, cfg)
    ev.evaluate_site = flaky
    try:
        with pytest.raises(RuntimeError, match="bad"): ev.run(["bad", "k"], TODAY, write=True, ask=None)
    finally: ev.evaluate_site = real
    assert calls == ["bad", "k"]
