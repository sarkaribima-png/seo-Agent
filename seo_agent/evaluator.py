"""Performance Evaluator: weekly scorecards for (A) SEO outcomes per site and (B) the agents themselves. Stored in table `agent_scorecards`.
Run: python -m seo_agent.evaluator <site_id|all> [--print] [--no-store] [--no-llm]

Everything is computed from tables the repo already has (proposals, gsc_daily/gsc_by_day, content_posts + post_perf, tech_issues, keyword_opps, reports,
llm_usage) plus agent_runs (schema_v26, written by runlog.log_run). Missing table or no data -> DATA NOT AVAILABLE, never a guess.
Rules: every metric carries its sample size; small samples are marked Low confidence; lifts are correlation, not causation (other changes, seasonality and
Google updates move numbers too). Grade thresholds are in CFG below: change them there, nowhere else.
Role and rules: agents/evaluator.md. Nothing here touches a live site. Claude only phrases the 5-line owner summary (template fallback)."""
import re, sys, json, datetime as dt
from . import sb

NA = "DATA NOT AVAILABLE"

# ----------------------------------------------------------------------------- CONFIG (all tunables live here)
CFG = {
    "window_days": 28,            # proposals / runs / cost rates are measured over the last N days
    "lift_lookback_days": 90,     # applied changes older than this are not measured
    "max_lift_changes": 80,       # most recent N applied changes per site get a GSC lookup (keeps the run fast)
    "before_days": 14, "after_days": 14,
    "min_after_days": 7,          # fewer finished days after the change -> "too early", not measured
    "gsc_lag": 2,                 # Search Console data is ~2 days old
    "min_coverage": 0.8,          # share of days in a window where the site has any GSC data; else the window is not trusted
    "win_pct": 0.10, "min_abs_delta": 0.2,   # a change "wins" if clicks/day rise by max(10% of before, 0.2/day); "loses" if they fall by as much
    "lift_conf": {"high_clicks": 100, "high_after_days": 14, "med_clicks": 30},   # confidence of one measured change
    "min_sample": 10,             # below this many items a card/metric is marked Low confidence (>= 2x -> High)
    "min_graded_n": 3,            # a metric with fewer items than this is shown but not graded
    "min_site_clicks": 30,        # site click change is graded only if the earlier 28 days had at least this many clicks
    # metric -> (direction, A, B, C). "hi": value >= A is A, >= B is B, >= C is C, else D. "lo": value <= A is A ... else D.
    "grade_rules": {
        "run_success_rate": ("hi", 0.95, 0.85, 0.70),
        "failed_rate": ("lo", 0.05, 0.15, 0.30),
        "rollback_rate": ("lo", 0.03, 0.10, 0.20),
        "approval_rate": ("hi", 0.60, 0.40, 0.20),
        "win_rate": ("hi", 0.60, 0.45, 0.30),
        "tech_open_change": ("lo", -0.20, 0.00, 0.20),      # open tech issues now vs ~4 weeks ago (relative change)
        "clicks_change_28d": ("hi", 0.10, 0.00, -0.10),     # site clicks last 28 days vs the 28 before
    },
    "grade_points": {"A": 4, "B": 3, "C": 2, "D": 1},
    "overall_cuts": (3.5, 2.5, 1.5),                        # mean points >= 3.5 -> A, >= 2.5 -> B, >= 1.5 -> C, else D
    "agent_metrics": ("run_success_rate", "failed_rate", "rollback_rate", "approval_rate", "win_rate"),
    "site_metrics": ("win_rate", "tech_open_change", "clicks_change_28d"),
    "known_agents": ("orchestrator", "technical_auditor", "keyword_researcher", "content_optimizer", "rank_reporter", "evaluator"),
    # proposals have no agent column: attribute by `agent` if present, else source prefix, else kind. Edit when new agents write proposals.
    "source_agent": {"onpage": "content_optimizer", "tech": "technical_auditor", "keyword": "keyword_researcher", "orchestrator": "orchestrator"},
    "kind_agent": {"title_meta": "daily_optimizer", "schema": "content_optimizer", "internal_link": "content_optimizer", "content_add": "content_optimizer", "alt_text": "content_optimizer"},
    # module/command names used in agent_runs.agent (run.each patch logs the module name) -> agent name
    "run_alias": {"tech_audit": "technical_auditor", "keywords": "keyword_researcher", "onpage": "content_optimizer", "briefs": "content_optimizer",
                  "report": "rank_reporter", "run": "daily_optimizer", "orchestrator": "orchestrator", "evaluator": "evaluator"},
    "verify_mark": "did not show on the live page",     # run.apply_mod writes this in proposals.reason when its live check fails
    "top_n": 5,
}


# ----------------------------------------------------------------------------- small pure helpers
def safe_div(a, b): return None if not b else a / b
def median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs: return None
    n = len(xs); return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
def r2(x, k=2): return None if x is None else round(x, k)
def pct_change(now, prev): return None if now is None or not prev else (now - prev) / prev
def parse_ts(s):
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except Exception: return None
def day(s):
    try: return dt.date.fromisoformat(str(s)[:10])
    except Exception: return None
def week_of(today): y, w, _ = today.isocalendar(); return f"{y}-W{w:02d}"
def confidence(n, cfg=CFG):
    """High / Medium / Low from a sample size. n=0 or None -> Low."""
    n = n or 0
    return "High" if n >= 2 * cfg["min_sample"] else "Medium" if n >= cfg["min_sample"] else "Low"

def grade_value(value, rule):
    """Letter for one metric value, or None when there is no value."""
    if value is None: return None
    d, a, b, c = rule
    ok = (lambda t: value >= t) if d == "hi" else (lambda t: value <= t)
    return "A" if ok(a) else "B" if ok(b) else "C" if ok(c) else "D"

def grade_card(parts, names, cfg=CFG):
    """parts: {metric: (value, n)}. Only metrics in `names` with value not None and n >= min_graded_n count.
    Returns (grade, {metric: letter}, confidence). No gradable metric -> 'DATA NOT AVAILABLE'. Confidence follows the smallest graded sample."""
    letters = {}; ns = []
    for m in names:
        v, n = parts.get(m, (None, 0))
        if v is None or (n or 0) < cfg["min_graded_n"]: continue
        letters[m] = grade_value(v, cfg["grade_rules"][m]); ns.append(n)
    if not letters: return NA, {}, "Low"
    avg = sum(cfg["grade_points"][l] for l in letters.values()) / len(letters); a, b, c = cfg["overall_cuts"]
    return ("A" if avg >= a else "B" if avg >= b else "C" if avg >= c else "D"), letters, confidence(min(ns), cfg)


# ----------------------------------------------------------------------------- proposals
def agent_of(p, cfg=CFG):
    if p.get("agent"): return str(p["agent"])
    src = str(p.get("source") or "")
    for pre, a in cfg["source_agent"].items():
        if src.startswith(pre): return a
    return cfg["kind_agent"].get(p.get("kind"), "unknown")

def proposal_stats(props, cfg=CFG):
    """Counts and rates for a list of proposals (one agent / one site). Ratios are None when the denominator is 0."""
    c = dict(created=len(props), pending=0, approved=0, rejected=0, auto_applied=0, applied=0, rolled_back=0, failed=0, verify_failed=0, rollback_failed=0)
    tta = []
    for p in props:
        st = p.get("status"); human = bool(p.get("needs_approval")) or bool(p.get("decided_at")); reason = str(p.get("reason") or "")
        if st == "pending": c["pending"] += 1; continue
        if st == "rejected": c["rejected"] += 1
        elif st == "failed" and reason.startswith("rollback:"): c["rollback_failed"] += 1; c["approved"] += 1; continue
        elif human: c["approved"] += 1
        elif st in ("applied", "rolled_back", "rollback_requested"): c["auto_applied"] += 1
        if st == "applied": c["applied"] += 1
        elif st in ("rolled_back", "rollback_requested"): c["rolled_back"] += 1
        elif st == "failed":
            c["failed"] += 1
            if cfg["verify_mark"] in reason.lower(): c["verify_failed"] += 1
        # decided_at is overwritten when a rollback is requested, so those rows would give a wrong approval time
        if human and st not in ("rolled_back", "rollback_requested") and p.get("decided_at") and p.get("created_at"):
            a, b = parse_ts(p["created_at"]), parse_ts(p["decided_at"])
            if a and b and b >= a: tta.append((b - a).total_seconds() / 3600)
    ever = c["applied"] + c["rolled_back"]; decided = c["approved"] + c["rejected"]; tried = ever + c["failed"]
    c.update(applied_ever=ever, decided=decided, tried=tried,
             approval_rate=r2(safe_div(c["approved"], decided), 3), rollback_rate=r2(safe_div(c["rolled_back"], ever), 3),
             failed_rate=r2(safe_div(c["failed"], tried), 3), verify_failed_rate=r2(safe_div(c["verify_failed"], tried), 3),
             median_hours_to_approve=r2(median(tta), 1), tta_n=len(tta))
    return c


# ----------------------------------------------------------------------------- lift per applied change (GSC before/after)
def _window(rows, start, end):
    """Sum a page's GSC rows between two dates (inclusive). Calendar days, so a day with no impressions counts as a real zero."""
    n = (end - start).days + 1; clk = imp = pw = 0.0
    for r in rows:
        d = day(r.get("date"))
        if d is None or d < start or d > end: continue
        i = float(r.get("impressions") or 0); clk += float(r.get("clicks") or 0); imp += i
        if r.get("position") is not None: pw += float(r["position"]) * i
    return {"days": n, "clicks": clk, "impressions": imp, "clicks_pd": clk / n if n else None, "impr_pd": imp / n if n else None, "position": r2(pw / imp, 2) if imp else None}

def _coverage(site_dates, start, end):
    n = (end - start).days + 1
    return safe_div(sum(1 for i in range(n) if (start + dt.timedelta(days=i)).isoformat() in site_dates), n) or 0.0

def measure_change(p, page_rows, site_dates, today, cfg=CFG):
    """Before/after Search Console numbers for one applied change. Returns a dict with status measured | too_early | no_gsc_coverage.
    This is a correlation: nothing here proves the change caused the movement."""
    out = {"id": p.get("id"), "page": p.get("page"), "kind": p.get("kind"), "agent": agent_of(p, cfg), "applied_at": p.get("applied_at"), "status": "measured"}
    t = parse_ts(p.get("applied_at"))
    if not t: out["status"] = "no_gsc_coverage"; out["why"] = "applied_at missing"; return out
    a = t.date(); end_data = today - dt.timedelta(days=cfg["gsc_lag"])
    b0, b1 = a - dt.timedelta(days=cfg["before_days"]), a - dt.timedelta(days=1)
    a0, a1 = a + dt.timedelta(days=1), min(a + dt.timedelta(days=cfg["after_days"]), end_data)
    if (a1 - a0).days + 1 < cfg["min_after_days"]: out["status"] = "too_early"; return out
    if _coverage(site_dates, b0, b1) < cfg["min_coverage"] or _coverage(site_dates, a0, a1) < cfg["min_coverage"]:
        out["status"] = "no_gsc_coverage"; out["why"] = "site-level Search Console data missing for part of the window"; return out
    B, A = _window(page_rows, b0, b1), _window(page_rows, a0, a1)
    d = A["clicks_pd"] - B["clicks_pd"]; thr = max(cfg["win_pct"] * B["clicks_pd"], cfg["min_abs_delta"])
    tot = B["clicks"] + A["clicks"]; lc = cfg["lift_conf"]
    out.update(before=B, after=A, delta_clicks_pd=r2(d, 3), pct_clicks=r2(pct_change(A["clicks_pd"], B["clicks_pd"]), 3), delta_impr_pd=r2(A["impr_pd"] - B["impr_pd"], 3),
               position_gain=r2(B["position"] - A["position"], 2) if B["position"] is not None and A["position"] is not None else None,
               verdict="win" if d >= thr else "loss" if d <= -thr else "flat",
               confidence="High" if tot >= lc["high_clicks"] and A["days"] >= lc["high_after_days"] else "Medium" if tot >= lc["med_clicks"] else "Low")
    return out

def mark_confounded(changes, applied_all, cfg=CFG):
    """Another change on the same page close in time makes the lift unattributable: flag it (such changes are not ranked or counted in win rate)."""
    span = dt.timedelta(days=cfg["before_days"] + cfg["after_days"])
    for c in changes:
        t = parse_ts(c.get("applied_at"))
        c["confounded"] = bool(t) and any(o.get("page") == c["page"] and o.get("id") != c["id"] and (ot := parse_ts(o.get("applied_at"))) and abs(ot - t) <= span for o in applied_all)
    return changes

def lift_stats(changes):
    """Summary of measured changes. Win rate uses unconfounded, measured changes only."""
    ms = [c for c in changes if c["status"] == "measured"]; ok = [c for c in ms if not c.get("confounded")]
    w = sum(c["verdict"] == "win" for c in ok); l = sum(c["verdict"] == "loss" for c in ok); f = sum(c["verdict"] == "flat" for c in ok)
    return {"measured": len(ok), "wins": w, "losses": l, "flat": f, "win_rate": r2(safe_div(w, len(ok)), 3), "confounded": len(ms) - len(ok),
            "too_early": sum(c["status"] == "too_early" for c in changes), "no_coverage": sum(c["status"] == "no_gsc_coverage" for c in changes),
            "median_delta_clicks_pd": r2(median([c["delta_clicks_pd"] for c in ok]), 3), "median_position_gain": r2(median([c.get("position_gain") for c in ok]), 2),
            "low_confidence": sum(c.get("confidence") == "Low" for c in ok)}

def brief(c):
    return {k: c.get(k) for k in ("id", "page", "kind", "agent", "applied_at", "delta_clicks_pd", "pct_clicks", "delta_impr_pd", "position_gain", "confidence")} | \
           {"clicks_pd_before": r2(c["before"]["clicks_pd"], 2), "clicks_pd_after": r2(c["after"]["clicks_pd"], 2)}

def top_changes(changes, n=CFG["top_n"]):
    ok = [c for c in changes if c["status"] == "measured" and not c.get("confounded")]
    wins = sorted([c for c in ok if c["verdict"] == "win"], key=lambda c: -c["delta_clicks_pd"])[:n]
    worst = sorted([c for c in ok if c["verdict"] == "loss"], key=lambda c: c["delta_clicks_pd"])[:n]
    return [brief(c) for c in wins], [brief(c) for c in worst]


# ----------------------------------------------------------------------------- other measurements (pure)
def cost_stats(usage, month_start, win_start, agent, applied_ever):
    """Claude cost for one agent: last N days, this month, and per applied change (None when nothing was applied)."""
    mine = [u for u in usage or [] if u.get("agent") == agent]
    def tot(since):
        return round(sum(float(u.get("cost_usd") or 0) for u in mine if (parse_ts(u.get("created_at")) or since) >= since), 4)
    w, m = tot(win_start), tot(month_start)
    return {"cost_usd_window": w, "cost_usd_month": m, "calls_window": sum(1 for u in mine if (parse_ts(u.get("created_at")) or win_start) >= win_start),
            "cost_per_applied_change": r2(safe_div(w, applied_ever), 4)}

def run_stats(runs, agent, win_start, cfg=CFG):
    mine = [r for r in runs or [] if cfg["run_alias"].get(r.get("agent"), r.get("agent")) == agent and (parse_ts(r.get("created_at")) or win_start) >= win_start]
    bad = [r for r in mine if not r.get("ok")]
    return {"runs": len(mine), "ok_runs": len(mine) - len(bad), "run_success_rate": r2(safe_div(len(mine) - len(bad), len(mine)), 3),
            "median_seconds": r2(median([r.get("seconds") for r in mine]), 1), "last_failure": (bad[-1].get("detail") or "")[:160] if bad else None}

def tech_stats(rows, today):
    if rows is None: return None
    by = lambda s: sum(1 for r in rows if r.get("status") == s); op = [r for r in rows if r.get("status") == "open"]
    cut = today - dt.timedelta(days=7)
    return {"open": len(op), "fixed": by("fixed"), "ignored": by("ignored"), "superseded": by("superseded"),
            "open_critical_high": sum(1 for r in op if r.get("severity") in ("Critical", "High")),
            "found_last_7d": sum(1 for r in rows if (d := day(r.get("found_at"))) and d >= cut)}

def kw_stats(rows):
    if rows is None: return None
    n = lambda s: sum(1 for r in rows if r.get("status") == s); conv = n("approved") + n("fed"); dec = conv + n("rejected")
    return {"total": len(rows), "new": n("new"), "approved": n("approved"), "fed": n("fed"), "rejected": n("rejected"), "converted": conv, "conversion_rate": r2(safe_div(conv, dec), 3), "decided": dec}

def index_stats(posts, perf):
    """Days from publish to the first check that saw the post indexed. The check runs on a schedule, so this is an upper bound (resolution = check interval)."""
    if posts is None or perf is None: return None
    first = {}
    for r in perf:
        if r.get("indexed") is True and (d := day(r.get("day"))) and (r["post_id"] not in first or d < first[r["post_id"]]): first[r["post_id"]] = d
    checked = {r["post_id"] for r in perf}; days = []; waiting = 0; unchecked = 0
    for p in posts:
        pub = day(p.get("published_at"))
        if not pub: continue
        if p["id"] in first: days.append(max((first[p["id"]] - pub).days, 0))
        elif p["id"] in checked: waiting += 1
        else: unchecked += 1
    return {"posts": len(posts), "indexed": len(days), "median_days_to_index": r2(median(days), 1), "max_days_to_index": max(days) if days else None,
            "published_not_indexed_yet": waiting, "never_checked": unchecked, "note": "upper bound: first check that saw it indexed"}

def site_click_stats(gsc, today, cfg=CFG):
    """Clicks / impressions / impression-weighted position, last 28 days vs the 28 before. None when there are not enough days with data."""
    if not gsc: return None
    end = today - dt.timedelta(days=cfg["gsc_lag"]); by = {}
    for r in gsc:
        d = day(r.get("date"))
        if d is None: continue
        x = by.setdefault(d, [0.0, 0.0, 0.0]); i = float(r.get("impressions") or 0)
        x[0] += float(r.get("clicks") or 0); x[1] += i; x[2] += float(r.get("position") or 0) * i
    def agg(s, e):
        ds = [d for d in by if s <= d <= e]
        if not ds: return None
        cl = sum(by[d][0] for d in ds); im = sum(by[d][1] for d in ds)
        return {"days_with_data": len(ds), "clicks": cl, "impressions": im, "position": r2(sum(by[d][2] for d in ds) / im, 2) if im else None}
    cur = agg(end - dt.timedelta(days=27), end); prev = agg(end - dt.timedelta(days=55), end - dt.timedelta(days=28))
    if not cur or not prev or cur["days_with_data"] < 20 or prev["days_with_data"] < 20: return {"clicks_change_28d": None, "now": cur, "before": prev, "note": "less than 20 days of data in a period"}
    ch = pct_change(cur["clicks"], prev["clicks"]) if prev["clicks"] >= cfg["min_site_clicks"] else None
    return {"clicks_change_28d": r2(ch, 3), "impressions_change_28d": r2(pct_change(cur["impressions"], prev["impressions"]), 3),
            "position_change_28d": r2(cur["position"] - prev["position"], 2) if cur["position"] is not None and prev["position"] is not None else None,
            "now": cur, "before": prev, "n_prev_clicks": prev["clicks"], "note": None if ch is not None else f"earlier 28 days had under {cfg['min_site_clicks']} clicks: change not graded"}

def tech_trend(prior_cards, now_open, cfg=CFG):
    """Open tech issues now vs ~4 weeks ago, from this evaluator's own earlier weekly rows (the audit table keeps no history)."""
    series = [(r.get("week"), ((r.get("metrics") or {}).get("tech") or {}).get("open")) for r in prior_cards or []]
    series = sorted([(w, o) for w, o in series if o is not None])[-4:]
    if now_open is None or not series: return {"series": series, "open_change": None, "n": 0}
    prev = series[0][1]
    return {"series": series, "open_change": r2(pct_change(now_open, prev), 3), "n": prev, "since_week": series[0][0]}


# ----------------------------------------------------------------------------- reading Supabase
def q(table, **params):
    """All rows of a table (paged), or None if the table does not exist / cannot be read."""
    out = []
    try:
        if "limit" in params: return sb.select(table, **params)          # caller asked for a fixed number of rows
        for i in range(200):
            rows = sb.select(table, **params, limit="1000", offset=str(i * 1000)); out += rows
            if len(rows) < 1000: break
        return out
    except Exception as e:
        q.errors[table] = str(e)[:100]; return None
q.errors = {}

def gather(site_id, today, cfg=CFG):
    """Everything the cards need. site_id=None -> all sites. None for a table means missing/unreadable."""
    q.errors = {}; S = {"site_id": f"eq.{site_id}"} if site_id else {}
    win = (today - dt.timedelta(days=cfg["window_days"])).isoformat(); month = today.replace(day=1).isoformat(); since = min(win, month)
    st = {"win_start": parse_ts(win), "month_start": parse_ts(month)}
    st["proposals"] = q("proposals", **S, select="id,page,kind,status,reason,source,agent,needs_approval,created_at,decided_at,applied_at", created_at=f"gte.{win}")
    st["usage"] = q("llm_usage", **S, select="agent,cost_usd,created_at", created_at=f"gte.{since}")
    st["runs"] = q("agent_runs", **S, select="agent,ok,seconds,detail,created_at", created_at=f"gte.{win}")
    st["tech"] = q("tech_issues", **S, select="status,severity,found_at")
    st["kw"] = q("keyword_opps", **S, select="status")
    st["reports"] = q("reports", **S, select="created_at,period", order="created_at.desc", limit="1")
    return st

def gather_site_extra(site_id, today, cfg=CFG):
    S = {"site_id": f"eq.{site_id}"}; back = (today - dt.timedelta(days=cfg["lift_lookback_days"] + cfg["before_days"])).isoformat()
    ex = {}
    ex["applied_all"] = q("proposals", **S, status="in.(applied,rolled_back,rollback_requested)", select="id,page,kind,status,source,agent,applied_at", applied_at=f"gte.{back}")
    ex["gsc_site"] = q("gsc_by_day", **S, select="date,clicks,impressions,position", date=f"gte.{(today - dt.timedelta(days=cfg['lift_lookback_days'] + 70)).isoformat()}")
    ex["posts"] = q("content_posts", **S, status="eq.published", select="id,published_at", published_at=f"gte.{(today - dt.timedelta(days=365)).isoformat()}")
    ex["perf"] = q("post_perf", **S, select="post_id,day,indexed")
    ex["prior"] = q("agent_scorecards", **S, agent="eq.site_outcomes", select="week,metrics", order="week.desc", limit="6")
    return ex

def measure_site_changes(site_id, ex, today, cfg=CFG):
    """Look up GSC per applied change. Returns (changes, reason_if_not_available)."""
    if ex["applied_all"] is None or ex["gsc_site"] is None: return None, "table proposals or gsc_by_day not readable"
    cut = today - dt.timedelta(days=cfg["lift_lookback_days"])
    cand = sorted([p for p in ex["applied_all"] if p.get("status") == "applied" and p.get("page") and (t := parse_ts(p.get("applied_at"))) and t.date() >= cut],
                  key=lambda p: p["applied_at"], reverse=True)[:cfg["max_lift_changes"]]
    site_dates = {d.isoformat() for r in ex["gsc_site"] if float(r.get("impressions") or 0) > 0 and (d := day(r.get("date")))}
    out = []
    for p in cand:
        a = parse_ts(p["applied_at"]).date()
        lo, hi = (a - dt.timedelta(days=cfg["before_days"])).isoformat(), (a + dt.timedelta(days=cfg["after_days"])).isoformat()
        rows = q("gsc_daily", site_id=f"eq.{site_id}", page=f"eq.{p['page']}", select="date,clicks,impressions,position", **{"and": f"(date.gte.{lo},date.lte.{hi})"})
        if rows is None: return None, "gsc_daily not readable"
        out.append(measure_change(p, rows, site_dates, today, cfg))
    return mark_confounded(out, ex["applied_all"], cfg), None


# ----------------------------------------------------------------------------- scorecards
def agent_cards(st, changes, today, cfg=CFG):
    """One card per agent from gathered data (a site's, or all sites'). changes = measured changes for lift (may be None)."""
    props = st["proposals"]; names = set(cfg["known_agents"])
    if props: names |= {agent_of(p, cfg) for p in props}
    for u in st["usage"] or []: names.add(u.get("agent"))
    for r in st["runs"] or []: names.add(cfg["run_alias"].get(r.get("agent"), r.get("agent")))
    names.discard(None); cards = {}
    for a in sorted(names):
        notes = []; m = {"window_days": cfg["window_days"]}
        mine = [p for p in props if agent_of(p, cfg) == a] if props is not None else None
        if mine is None: m["proposals"] = NA; ps = {}; notes.append("proposals table not readable")
        else: ps = proposal_stats(mine, cfg); m["proposals"] = ps
        rs = run_stats(st["runs"], a, st["win_start"], cfg) if st["runs"] is not None else None
        m["runs"] = rs if rs and rs["runs"] else NA
        if st["runs"] is None: notes.append("agent_runs table missing: run schema_v26.sql and adopt log_run (PATCHES.md)")
        elif rs["runs"] == 0: notes.append("no runs logged for this agent yet")
        m["cost"] = cost_stats(st["usage"], st["month_start"], st["win_start"], a, ps.get("applied_ever") or 0) if st["usage"] is not None else NA
        if st["usage"] is None: notes.append("llm_usage not readable")
        mc = [c for c in changes if c["agent"] == a] if changes is not None else None
        ls = lift_stats(mc) if mc is not None else None
        m["lift"] = ls if ls and (ls["measured"] or ls["too_early"] or ls["no_coverage"] or ls["confounded"]) else NA
        if a == "technical_auditor": m["tech"] = tech_stats(st["tech"], today) or NA
        if a == "keyword_researcher": m["keyword_opps"] = kw_stats(st["kw"]) or NA
        if a == "rank_reporter":
            rp = (st["reports"] or [{}])[0] if st["reports"] is not None else None
            t = parse_ts(rp.get("created_at")) if rp else None
            m["last_report_age_days"] = (today - t.date()).days if t else NA
        parts = {"run_success_rate": (rs["run_success_rate"] if rs else None, rs["runs"] if rs else 0),
                 "failed_rate": (ps.get("failed_rate"), ps.get("tried", 0)), "rollback_rate": (ps.get("rollback_rate"), ps.get("applied_ever", 0)),
                 "approval_rate": (ps.get("approval_rate"), ps.get("decided", 0)), "win_rate": (ls["win_rate"] if ls else None, ls["measured"] if ls else 0)}
        g, letters, conf = grade_card(parts, cfg["agent_metrics"], cfg)
        n_basis = ps.get("created", 0) + (rs["runs"] if rs else 0)
        if g == NA: notes.append("not enough finished items to grade")
        elif conf == "Low": notes.append(f"small sample (smallest graded metric n<{cfg['min_sample']}): grade is provisional")
        if ps.get("created") and ps.get("pending"): notes.append(f"{ps['pending']} proposals still pending: rates will move")
        if ps.get("median_hours_to_approve") is not None and ps.get("tta_n", 0) < cfg["min_graded_n"]: notes.append("time-to-approve from fewer than 3 items")
        notes.append("lift is correlation, not causation")
        m.update(grade_letters=letters, confidence=conf, sample={"proposals": ps.get("created", 0), "runs": rs["runs"] if rs else 0, "basis_n": n_basis})
        active = n_basis or (m["cost"] != NA and m["cost"]["calls_window"]) or m["lift"] != NA
        cards[a] = {"metrics": m, "grade": g, "notes": "; ".join(notes), "active": bool(active)}
    return cards

def site_card(site_id, st, ex, changes, why_no_lift, today, cfg=CFG):
    """Part A: SEO outcome card for one site."""
    notes = []; m = {"window_days": cfg["window_days"]}
    ls = lift_stats(changes) if changes is not None else None
    m["lift"] = ls if ls else NA
    if changes is None: notes.append(f"lift per change: {NA} ({why_no_lift})")
    wins, worst = top_changes(changes or []); m["top_wins"], m["worst_changes"] = wins, worst
    gs = site_click_stats(ex["gsc_site"], today, cfg) if ex["gsc_site"] is not None else None
    m["gsc"] = gs or NA
    if gs is None: notes.append("Search Console data " + NA)
    m["tech"] = tech_stats(st["tech"], today) or NA
    if st["tech"] is None: notes.append("tech_issues table missing (schema_v17)")
    tt = tech_trend(ex["prior"], (m["tech"] or {}).get("open") if m["tech"] != NA else None, cfg); m["tech_trend"] = tt
    if tt["open_change"] is None: notes.append("tech issue trend needs earlier weekly rows (the audit keeps no history); builds up week by week")
    m["keyword_opps"] = kw_stats(st["kw"]) or NA
    m["indexing"] = index_stats(ex["posts"], ex["perf"]) or NA
    ps = proposal_stats(st["proposals"] or [], cfg) if st["proposals"] is not None else {}
    m["proposals"] = ps or NA
    m["cost_month_usd"] = r2(sum(float(u.get("cost_usd") or 0) for u in st["usage"] or [] if (parse_ts(u.get("created_at")) or st["month_start"]) >= st["month_start"]), 4) if st["usage"] is not None else NA
    parts = {"win_rate": (ls["win_rate"] if ls else None, ls["measured"] if ls else 0),
             "tech_open_change": (tt["open_change"], tt["n"]), "clicks_change_28d": ((gs or {}).get("clicks_change_28d"), 99 if gs and gs.get("clicks_change_28d") is not None else 0)}
    g, letters, conf = grade_card(parts, cfg["site_metrics"], cfg)
    m.update(grade_letters=letters, confidence=conf)
    if ls and ls["measured"] < cfg["min_sample"]: notes.append(f"only {ls['measured']} measured changes: win rate is Low confidence")
    if ls and ls["confounded"]: notes.append(f"{ls['confounded']} changes skipped as confounded (another change on the same page within {cfg['before_days'] + cfg['after_days']} days)")
    if g == NA: notes.append("not enough data to grade this site yet")
    notes.append("lift is correlation, not causation (seasonality, Google updates and other edits also move clicks)")
    return {"metrics": m, "grade": g, "notes": "; ".join(notes)}


# ----------------------------------------------------------------------------- owner summary (Hinglish, 5 lines)
def pc(x): return "n/a" if x is None else f"{x * 100:+.0f}%"

def template_summary(site_id, week, sc, cards):
    """Five plain Hinglish lines. Only numbers from the computed metrics; nothing is estimated."""
    m = sc["metrics"]; ps = m["proposals"] if m["proposals"] != NA else None; ls = m["lift"] if m["lift"] != NA else None
    L = [f"{site_id} ({week}): overall grade {sc['grade']}." + (" Data kam hai, isliye grade abhi pakka nahi." if sc["grade"] != NA and m["confidence"] == "Low" else
                                                              " Grade dene ke liye abhi data kam hai (DATA NOT AVAILABLE)." if sc["grade"] == NA else "")]
    L.append(f"Pichhle {m['window_days']} din me {ps['created']} proposals bane, {ps['approved']} approve, {ps['rejected']} reject, {ps['applied_ever']} live hue; rollback {ps['rolled_back']}, fail {ps['failed']}."
             if ps and ps["created"] else f"Pichhle {m['window_days']} din me koi proposal nahi bana (ya data nahi mila).")
    L.append(f"{ls['measured']} live changes ka asar naapa: {ls['wins']} me clicks badhe, {ls['losses']} me ghate, {ls['flat']} barabar. Ye correlation hai, pakka karan nahi."
             if ls and ls["measured"] else "Live changes ka asar naapne ke liye abhi data kam hai (DATA NOT AVAILABLE).")
    t = m["tech"] if m["tech"] != NA else None; g = m["gsc"] if m["gsc"] != NA else None
    p4 = f"Technical issues open: {t['open']}" + (f" (4 hafte pehle se {pc(m['tech_trend']['open_change'])})" if m["tech_trend"]["open_change"] is not None else "") if t else "Technical issues: DATA NOT AVAILABLE"
    p4 += f". Clicks (28 din): {pc(g['clicks_change_28d'])}." if g and g.get("clicks_change_28d") is not None else ". Clicks ka 28-din comparison: DATA NOT AVAILABLE."
    L.append(p4)
    graded = sorted([(a, c["grade"]) for a, c in cards.items() if c["grade"] in "ABCD" and len(c["grade"]) == 1 and c["active"]], key=lambda x: (x[1], x[0]), reverse=True)
    cost = m["cost_month_usd"]; p5 = f"Is mahine Claude kharcha ${cost:.2f}." if cost != NA else "Claude kharcha: DATA NOT AVAILABLE."
    if graded and graded[0][1] in ("C", "D"): p5 += f" Sabse kamzor agent: {graded[0][0]} (grade {graded[0][1]})."
    L.append(p5)
    return L

SYS = ("Tum SEO agency ke owner ke liye 5-line ka simple Hinglish summary likhte ho. Input me 5 lines hain jo pehle se sahi hain. "
       "Unhe sirf aasan Hinglish me rephrase karo: exactly 5 lines, har line ek sentence ya do. Koi naya number, naya claim, guarantee, ranking promise ya competitor ka naam mat jodo. "
       "Jo 'DATA NOT AVAILABLE' hai use waise hi rehne do. 'correlation, pakka karan nahi' wali baat rakho. Sirf 5 lines do, koi heading ya bullets nahi.")

def nums(t): return set(re.findall(r"\d+(?:\.\d+)?", t))

def owner_summary(site_id, week, sc, cards, ask=None):
    """Returns (text, llm_used). Claude may only rephrase: reply must be 5 lines and add no number that is not in the template."""
    base = template_summary(site_id, week, sc, cards); tpl = "\n".join(base)
    if ask is None: return tpl, False
    try:
        r = ask(SYS, tpl).strip(); lines = [x.strip(" -*\t") for x in r.splitlines() if x.strip()]
        if len(lines) == 5 and nums(r) <= nums(tpl) and not re.search(r"(?i)guarantee|100%|rank(ing)? #?1\b", r): return "\n".join(lines), True
    except Exception as e: print("summary: using template:", str(e)[:80])
    return tpl, False

def default_ask(system, user):
    from .llm import ask
    return ask(system, user, agent="evaluator", max_tokens=500)


# ----------------------------------------------------------------------------- run
def rows_for(week, site_id, cards, site=None, summary=None):
    scope = site_id or "ALL"; out = []
    if site:
        out.append({"week": week, "site_id": site_id, "scope": scope, "agent": "site_outcomes", "metrics": {**site["metrics"], "owner_summary": summary[0], "summary_llm": summary[1]}, "grade": site["grade"], "notes": site["notes"]})
    for a, c in cards.items():
        if c["active"] or a in CFG["known_agents"]:
            out.append({"week": week, "site_id": site_id, "scope": scope, "agent": a, "metrics": c["metrics"], "grade": c["grade"], "notes": c["notes"]})
    return out

def evaluate_site(site_id, today, ask=None, cfg=CFG):
    st = gather(site_id, today, cfg); ex = gather_site_extra(site_id, today, cfg)
    changes, why = measure_site_changes(site_id, ex, today, cfg)
    if st["proposals"] is None: changes, why = None, "proposals table missing"
    cards = agent_cards(st, changes, today, cfg); sc = site_card(site_id, st, ex, changes, why, today, cfg); week = week_of(today)
    summ = owner_summary(site_id, week, sc, cards, ask)
    return rows_for(week, site_id, cards, sc, summ), changes or [], cards, sc

def run(site_ids, today=None, write=True, ask=None, global_rows=True, show=False):
    """Evaluate each site, then (optionally) one all-sites row per agent. One site failing does not stop the others (error is raised at the end)."""
    today = today or dt.datetime.now(dt.timezone.utc).date(); rows = []; allch = []; bad = []; week = week_of(today)
    for sid in site_ids:
        try:
            r, ch, _, _ = evaluate_site(sid, today, ask); rows += r; allch += ch
        except Exception as e: bad.append(sid); print(f"SITE {sid} FAILED: {e}")
    if global_rows and site_ids:
        st = gather(None, today); cards = agent_cards(st, allch if allch or not bad else None, today)
        rows += rows_for(week, None, cards)
    if show:
        for r in rows: print(f"[{r['scope']}] {r['agent']}: {r['grade']} | {r['notes'][:140]}")
        for r in rows:
            if r["agent"] == "site_outcomes": print(f"\n{r['scope']} summary:\n{r['metrics']['owner_summary']}")
    if write and rows: sb.upsert("agent_scorecards", rows, "week,scope,agent"); print("scorecards saved:", len(rows))
    if bad: raise RuntimeError("failed sites: " + ", ".join(bad))
    return rows


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0].startswith("--"): raise SystemExit("usage: python -m seo_agent.evaluator <site_id|all> [--print] [--no-store] [--no-llm]")
    import time
    from . import run as _r
    from .runlog import log_run
    t0 = time.time(); ok = False
    try:
        ids = _r.site_ids(a[0]); run(ids, write="--no-store" not in a, ask=None if "--no-llm" in a else default_ask, global_rows=a[0] == "all", show="--print" in a); ok = True
    except BaseException as e:
        log_run("evaluator", None if a[0] == "all" else a[0], False, time.time() - t0, str(e)[:200]); raise
    log_run("evaluator", None if a[0] == "all" else a[0], ok, time.time() - t0)
