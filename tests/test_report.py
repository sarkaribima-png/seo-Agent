import sys, types, os, datetime as dt, copy
sys.path.insert(0, os.path.dirname(__file__))
import pytest
from fixtures_report import *
from seo_agent import report as R
SITE = {"id": "demo", "domain": "demo.example"}


def rep(data=None, period="week"): return R.build_report(SITE, period, data or synthetic(), TODAY)
def titles(r): return [a["title"] for a in r["alerts"]]


def test_windows_week_and_month():
    w = R.windows("week", TODAY); assert w["cur"] == (dt.date(2026, 3, 8), dt.date(2026, 3, 14)) and w["prev"] == (dt.date(2026, 3, 1), dt.date(2026, 3, 7))
    m = R.windows("month", TODAY); assert (m["cur"][1] - m["cur"][0]).days == 27 and m["prev"][1] == m["cur"][0] - dt.timedelta(days=1)


def test_key_numbers_change_pct():
    t = {x["metric"]: x for x in rep()["table"]}
    assert t["Clicks"]["last"] == (10 + 8 + 6 + 4) * 7 and t["Clicks"]["now"] == (5 + 8 + 6 + 9) * 7
    assert t["Clicks"]["change_pct"] == pytest.approx(0.0)
    t2 = {x["metric"]: x for x in rep(synthetic(0.0))["table"]}; assert t2["Clicks"]["change_pct"] == pytest.approx((10 + 8 + 6 + 9 - 28) / 28 * 100)
    assert t["Impressions"]["last"] == 2800


def test_ga4_not_available_when_unset_and_real_when_present():
    t = {x["metric"]: x for x in rep()["table"]}; assert t["GA4 sessions"]["now"] == R.NA and t["GA4 sessions"]["change_pct"] is None
    d = synthetic(); d["ga4"] = [{"date": "2026-03-02", "sessions": 100, "conversions": 2}, {"date": "2026-03-09", "sessions": 150, "conversions": 3}]
    r = rep(d); t = {x["metric"]: x for x in r["table"]}; assert t["GA4 sessions"]["change_pct"] == 50 and r["ga4_connected"]


@pytest.mark.parametrize("drop,expect", [(0.19, False), (0.21, True), (0.5, True)])
def test_click_drop_threshold_page(drop, expect):
    r = rep(synthetic(drop)); got = any(a["level"] == "page" and a["page"] == "https://x.in/a" and "clicks" in a["title"] for a in r["alerts"]); assert got == expect


@pytest.mark.parametrize("fall,expect", [(2.9, False), (3.0, True), (4.0, True)])
def test_position_fall_threshold_page(fall, expect):
    d = synthetic(); d["gsc_daily"] = [r for r in d["gsc_daily"] if r["page"] != "https://x.in/c"] + gsc_rows("https://x.in/c", 6, 6, 4.0, 4.0 + fall)
    r = rep(d); assert any(a["level"] == "page" and a["page"] == "https://x.in/c" for a in r["alerts"]) == expect


def test_keyword_alert_and_low_impressions_ignored():
    r = rep(); k = [a for a in r["alerts"] if a["level"] == "keyword"]
    assert any("tummy tuck cost" in a["title"] for a in k)           # 5.0 -> 9.5 = 4.5 worse
    assert not any("rare kw" in a["title"] for a in k)                # 0 impressions: too little data
    assert not any("rhinoplasty" in a["title"] for a in k)            # improved


def test_keyword_leaves_page_one_is_high():
    d = synthetic(); d["rank_daily"] = rank_rows("kw", 8.0, 11.5)
    a = [x for x in rep(d)["alerts"] if x["level"] == "keyword"][0]; assert "page 1 se bahar" in a["title"] and a["severity"] == "high"


def test_site_alert_needs_enough_base():
    d = {"gsc_daily": gsc_rows("https://x.in/p", 1, 0, 5, 5), "rank_daily": [], "tracked_keywords": [], "proposals": [], "page_changes": []}   # 7 -> 0 clicks, base < 30
    r = rep(d); assert not any(a["level"] == "site" for a in r["alerts"]) and any(a["level"] == "note" for a in r["alerts"]) and r["confidence"] == "Low"
    d = {"gsc_daily": gsc_rows("https://x.in/p", 10, 7, 5, 5), "rank_daily": [], "tracked_keywords": [], "proposals": [], "page_changes": []}   # -30%
    assert any(a["level"] == "site" for a in rep(d)["alerts"])
    d = {"gsc_daily": gsc_rows("https://x.in/p", 10, 8.5, 5, 5), "rank_daily": [], "tracked_keywords": [], "proposals": [], "page_changes": []}  # -15%
    assert not any(a["level"] in ("site", "page") for a in rep(d)["alerts"])


def test_causes_use_ho_sakta_hai_and_name_logged_changes():
    a = [x for x in rep()["alerts"] if x["level"] == "page" and x["page"] == "https://x.in/a"][0]
    txt = " ".join(a["causes"]); assert "ho sakta hai" in txt and "2026-03-12" in txt and "Algorithm" in txt and "CTR gira ho sakta hai" in txt and "Competitor" in txt and "Technical" in txt
    assert "DATA NOT AVAILABLE" in txt


def test_winners_losers_top5_sorted_and_signed():
    wl = rep()["winners_losers"]
    assert wl["pages_win"][0]["page"] == "https://x.in/d" and all(x["delta"] > 0 for x in wl["pages_win"])
    assert wl["pages_lose"][0]["page"] == "https://x.in/a" and all(x["delta"] < 0 for x in wl["pages_lose"])
    assert wl["kw_win"][0]["keyword"] == "rhinoplasty" and wl["kw_lose"][0]["keyword"] == "tummy tuck cost" and wl["kw_skipped_low_data"] == 1


def test_top5_cap():
    d = synthetic(); d["gsc_daily"] = sum((gsc_rows(f"https://x.in/p{i}", 5, 5 + i + 1, 5, 5) for i in range(8)), [])
    assert len(rep(d)["winners_losers"]["pages_win"]) == 5


def test_impact_sample_sizes_and_caveats():
    r = rep(); imp = {i["proposal_id"]: i for i in r["impact"]}
    assert set(imp) == {11, 12}
    assert imp[12]["verdict"] == "data kam" and imp[12]["after"]["calendar_days"] < 5          # applied 2026-03-12: too early
    assert "correlation" in imp[12]["caveat"] and "ho sakta hai" in imp[12]["caveat"]
    assert any("page edit" in m for m in imp[12]["mixed_with"])                                # manual edit on page /a nearby
    assert imp[12]["before"]["days_with_data"] > 0


def test_impact_verdict_when_enough_data():
    d = synthetic(); d["proposals"] = [{"id": 5, "page": "https://x.in/d", "kind": "title_meta", "applied_at": "2026-03-02T04:00:00+00:00", "status": "applied"}]
    d["gsc_daily"] += [{"date": str(dt.date(2026, 2, 23) + dt.timedelta(days=i)), "page": "https://x.in/d", "device": "MOBILE", "clicks": 4, "impressions": 100, "position": 12.0} for i in range(7)]
    i = rep(d)["impact"][0]; assert i["verdict"] == "behtar dikha" and i["before"]["days_with_data"] >= 5 and i["tracked_keywords"]


def test_non_applied_proposals_ignored():
    d = synthetic(); d["proposals"] = [{"id": 1, "page": "https://x.in/a", "kind": "title_meta", "applied_at": "2026-03-02T00:00:00+00:00", "status": "pending"}]
    assert rep(d)["impact"] == []


def test_markdown_has_five_sections_confidence_evidence():
    md = R.to_markdown(rep())
    for h in ("## 1. Ek line me haal", "## 2. Key numbers", "## 3. Winners / Losers", "## 4. Alerts", "## 5. Agle hafte kya karna hai", "**Confidence:**", "**Evidence:**", "**Sample size:**", "Pichhle changes aur kya hila"):
        assert h in md
    assert "DATA NOT AVAILABLE" in md and "ho sakta hai" in md


def test_month_period_heading():
    assert "## 5. Agle mahine kya karna hai" in R.to_markdown(rep(period="month"))


def test_confidence_levels():
    assert rep()["confidence"] == "Medium"
    big = synthetic(); big["gsc_daily"] = gsc_rows("https://x.in/a", 40, 40, 5, 5)
    assert rep(big)["confidence"] == "High"


def test_no_data_does_not_crash_or_invent():
    r = R.build_report(SITE, "week", {}, TODAY); md = R.to_markdown(r)
    assert "DATA NOT AVAILABLE" in r["summary"] and r["confidence"] == "Low" and r["alerts"] == []


def test_llm_summary_rejects_new_numbers(monkeypatch):
    r = rep(); fake = types.ModuleType("anthropic")
    def mk(text):
        class M:
            def __init__(s, **k): pass
            class messages:
                @staticmethod
                def create(**k): return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)])
        return M
    fake.Anthropic = mk("Clicks 999999 ho gaye"); monkeypatch.setitem(sys.modules, "anthropic", fake)
    assert R.llm_summary(r) == r["summary"]


def test_store_and_load_use_supabase_only(monkeypatch):
    from seo_agent import sb
    calls = []; d = synthetic()
    def sel(t, **p):
        calls.append(t); off = int(p.get("offset", 0))
        if t == "ga4_daily": raise RuntimeError("relation does not exist")
        return {"gsc_daily": d["gsc_daily"], "rank_daily": [{k: v for k, v in r.items() if k != "page_url"} for r in d["rank_daily"]], "tracked_keywords": d["tracked_keywords"],
                "proposals": d["proposals"], "page_changes": d["page_changes"]}[t][off:off + int(p["limit"])]
    saved = {}
    monkeypatch.setattr(sb, "select", sel); monkeypatch.setattr(sb, "upsert", lambda t, rows, on, chunk=1000: saved.update(t=t, rows=rows, on=on))
    data = R.load("demo", "week", TODAY); assert data["ga4"] is None and data["rank_daily"][0]["page_url"]
    r = R.build_report(SITE, "week", data, TODAY); R.store(r, R.to_markdown(r))
    assert saved["t"] == "reports" and saved["on"] == "site_id,period,period_start" and saved["rows"][0]["period_start"] == "2026-03-08" and saved["rows"][0]["markdown"].startswith("# SEO report")


def test_seasonality_cause_when_impressions_also_fall():
    d = synthetic(); d["gsc_daily"] = gsc_rows("https://x.in/a", 10, 5, 5.0, 5.0, prev_impr=100, cur_impr=50)
    a = [x for x in rep(d)["alerts"] if x["level"] == "page"][0]; assert any("Seasonality / demand kam ho sakta hai" in c for c in a["causes"])


def test_impact_needs_before_data_too():
    i = {i["proposal_id"]: i for i in rep()["impact"]}[11]; assert i["before"]["days_with_data"] == 0 and i["verdict"] == "data kam"


def test_keyword_alert_once_per_keyword_across_devices():
    ks = [a for a in rep()["alerts"] if a["level"] == "keyword"]; assert len(ks) == 1 and "desktop+mobile" in ks[0]["title"]
