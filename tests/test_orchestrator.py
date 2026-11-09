import datetime as dt, json, os, sys
import pytest
from seo_agent import orchestrator as o, sb

TODAY = dt.date(2026, 11, 9)   # a Monday, ISO week 46
END = TODAY - dt.timedelta(days=2)


def day(n): return (END - dt.timedelta(days=n)).isoformat()


def gsc_rows(recent, prev):
    rows = [{"date": day(i), "clicks": recent / 7, "impressions": 100} for i in range(7)]
    rows += [{"date": day(i), "clicks": prev / 7, "impressions": 100} for i in range(7, 14)]
    return rows


class Store:
    """Fake Supabase: tables missing from `data` raise like PostgREST does for an unknown table."""
    def __init__(self, data): self.data = data; self.saved = []
    def select(self, t, **p):
        if t not in self.data: raise RuntimeError(f"404 relation {t} does not exist")
        return list(self.data[t])
    def upsert(self, t, rows, on, chunk=1000): self.saved.append((t, rows, on))


def base(**kw):
    d = {"sites": [{"id": "k", "mode": "dry-run", "profile": "medical", "reviewer_required": True}],
         "page_audits_latest": [], "proposals": [], "gsc_by_day": gsc_rows(100, 100), "tracked_keywords": [{"keyword": "a"}],
         "rank_daily": [], "content_posts": []}
    d.update(kw); return d


def plan(data, ask=None, monkeypatch=None):
    s = Store(data); sb.select = s.select; sb.upsert = s.upsert
    return o.run("k", TODAY, write=True, ask=ask), s


def test_week_label():
    assert o.week_of(TODAY) == "2026-W46"


def test_format_keys_and_graceful_when_sibling_tables_missing(capsys):
    p, s = plan(base())
    assert set(p) >= {"site", "week", "priority_tasks", "risks", "summary_for_owner"}
    assert p["priority_tasks"] == [] or all(set(t) >= {"task", "agent", "impact", "effort", "needs_approval"} for t in p["priority_tasks"])
    assert any("DATA NOT AVAILABLE" in r and "tech_issues" in r for r in p["risks"])
    assert any("keyword_opps" in r for r in p["risks"]) and any("reports" in r for r in p["risks"])
    assert p["confidence"] == "Medium"
    assert len(p["summary_for_owner"].splitlines()) == 5
    assert s.saved[0][0] == "weekly_plans" and s.saved[0][2] == "site_id,week"


def test_max_five_and_high_impact_low_effort_first():
    audits = [{"h1": 0, "images_no_alt": 2, "risky_claims": ["cure"], "schema_issues": ["invalid_json_ld"], "audited_at": "2026-09-01T00:00:00Z"}] * 4
    props = [{"id": i, "page": f"p{i}", "kind": "title_meta", "status": "pending", "created_at": "2026-11-01T00:00:00Z"} for i in range(6)]
    props += [{"id": 99, "page": "x", "kind": "schema", "status": "failed", "reason": "boom", "created_at": "2026-11-05T00:00:00Z"}]
    kw = [{"source": "striking_distance", "priority": "High"}] * 3 + [{"source": "cannibalization"}]
    p, _ = plan(base(page_audits_latest=audits, proposals=props, keyword_opps=kw, tech_issues=[{"severity": "Critical", "status": "open"}],
                     content_posts=[{"id": 1, "status": "draft", "issues": []}]))
    t = p["priority_tasks"]; assert len(t) == 5
    score = lambda x: o.IMPACT[x["impact"]] * o.EASE[x["effort"]]
    assert [score(x) for x in t] == sorted([score(x) for x in t], reverse=True)
    assert score(t[0]) == 9                                  # high impact + low effort leads
    assert any("more task(s) were left" in r for r in p["risks"])
    assert all(x["deadline"] >= TODAY.isoformat() for x in t)


def test_risky_and_content_changes_need_approval():
    assert o.T("Set up a redirect from /a to /b", o.TA, "high", "low", False)["needs_approval"] is True
    assert o.T("Add noindex to tag pages", o.TA, "high", "low", False)["needs_approval"] is True
    assert o.T("Delete thin pages", o.CO, "med", "low", False)["needs_approval"] is True
    assert o.T("Run audit", o.TA, "med", "low", False)["needs_approval"] is False
    audits = [{"h1": 1, "images_no_alt": 0, "risky_claims": ["guarantee"], "schema_issues": [], "audited_at": "2026-11-01T00:00:00Z"}]
    p, _ = plan(base(page_audits_latest=audits))
    assert [x for x in p["priority_tasks"] if "risky claims" in x["task"]][0]["needs_approval"] is True


def test_click_drop_alert_needs_enough_data():
    p, _ = plan(base(gsc_by_day=gsc_rows(60, 100)))                       # -40%, 100 prior clicks
    assert any("click drop of 40%" in t["task"] and t["agent"] == o.RR for t in p["priority_tasks"])
    p, _ = plan(base(gsc_by_day=gsc_rows(5, 14)))                         # tiny sample: no alert
    assert not any("click drop" in t["task"] for t in p["priority_tasks"])
    assert any("sample too small" in r for r in p["risks"])
    p, _ = plan(base(gsc_by_day=[]))
    assert any("DATA NOT AVAILABLE" in r for r in p["risks"]) or any("stale" in t["task"] for t in p["priority_tasks"])


def test_rank_drop_alert():
    rk = [{"keyword": "lipo", "device": "mobile", "day": day(i), "position": 4.0 if i >= 4 else 9.0, "impressions": 10} for i in range(11)]
    p, _ = plan(base(rank_daily=rk))
    assert any("fell about 3+" in t["task"] and "lipo" in t["task"] for t in p["priority_tasks"])


def test_paused_site_gets_no_tasks():
    p, _ = plan(base(sites=[{"id": "k", "mode": "paused"}], proposals=[{"id": 1, "page": "a", "kind": "x", "status": "pending", "created_at": "2026-11-01T00:00:00Z"}]))
    assert p["priority_tasks"] == [] and any("paused" in r for r in p["risks"])


def test_cannibalization_and_duplicate_pending_are_conflicts():
    props = [{"id": i, "page": "same", "kind": "title_meta", "status": "pending", "created_at": "2026-11-01T00:00:00Z"} for i in (1, 2)]
    p, _ = plan(base(proposals=props, keyword_opps=[{"source": "cannibalization"}]))
    assert sum(1 for r in p["risks"] if r.startswith("Conflict")) == 2


def test_llm_summary_accepted_only_if_no_new_numbers():
    audits = [{"h1": 0, "images_no_alt": 0, "risky_claims": [], "schema_issues": [], "audited_at": "2026-11-01T00:00:00Z"}]
    good = lambda system, facts: "Namaste.\nIs hafte ek kaam hai: H1 theek karna.\nApproval chahiye.\nRisk kam hai.\nShukriya."
    p, s = plan(base(page_audits_latest=audits), ask=good)
    assert s.saved[0][1][0]["llm_used"] is True and p["summary_for_owner"].startswith("Namaste")
    bad = lambda system, facts: "Is hafte 9999 clicks aayenge.\nok\nok\nok\nok"
    p, s = plan(base(page_audits_latest=audits), ask=bad)
    assert s.saved[0][1][0]["llm_used"] is False and "9999" not in p["summary_for_owner"]
    boom = lambda system, facts: (_ for _ in ()).throw(RuntimeError("api down"))
    p, s = plan(base(page_audits_latest=audits), ask=boom)
    assert len(p["summary_for_owner"].splitlines()) == 5


def test_everything_missing_is_low_confidence_not_a_crash():
    p, _ = plan({"sites": [{"id": "k", "mode": "dry-run"}]})
    assert p["confidence"] == "Low" and p["priority_tasks"] == [] and p["evidence"]


def test_plan_is_json_serialisable_and_deterministic():
    d = base(proposals=[{"id": 1, "page": "a", "kind": "title_meta", "status": "pending", "created_at": "2026-11-01T00:00:00Z"}])
    a, _ = plan(d); b, _ = plan(d)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_tech_issues_closed_states_and_report_freshness():
    tech = [{"severity": "Critical", "status": "superseded"}, {"severity": "High", "status": "ignored"}, {"severity": "Low", "status": "open"}]
    p, _ = plan(base(tech_issues=tech, reports=[{"created_at": "2026-11-08T03:00:00Z"}]))
    assert not any("technical issue" in t["task"] for t in p["priority_tasks"])
    assert not any("report" in t["task"].lower() for t in p["priority_tasks"])          # fresh weekly report: no task
    p, _ = plan(base(tech_issues=[{"severity": "Critical", "status": "open"}], reports=[{"created_at": "2026-10-01T03:00:00Z"}]))
    assert any("Critical/High technical" in t["task"] and t["needs_approval"] for t in p["priority_tasks"])
    assert any("this week's report" in t["task"] for t in p["priority_tasks"])
