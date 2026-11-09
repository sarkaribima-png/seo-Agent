"""SEO Orchestrator: builds the Weekly Action Plan for each site (max 5 tasks) and stores it in Supabase table `weekly_plans`.
Run: python -m seo_agent.orchestrator <site_id|all> [--print]

Role and rules: agents/orchestrator.md (owner's prompt, verbatim). This file does the boring, checkable part in plain code:
  1. read the site's status from Supabase (audits, proposals, tracking, and the tables of the sibling agents if they exist)
  2. turn that into candidate tasks, each with owner agent, impact, effort, needs_approval
  3. rank by impact x effort (high impact + low effort first), keep at most 5
  4. phrase summary_for_owner in simple Hinglish (a template; Claude may re-phrase it, but only if it adds no number that is not in the plan)
Nothing here touches a live site. It only reads tables and writes one row per site per week.
Missing data is written as DATA NOT AVAILABLE. Missing tables (schema_v17..v20 not run yet) are skipped, never guessed."""
import os, re, sys, json, datetime as dt
from . import sb

TA, KR, CO, RR = "Technical Auditor", "Keyword & SERP Researcher", "Content & On-Page Optimizer", "Rank Tracker & Reporter"
AGENT_ORDER = {TA: 0, KR: 1, CO: 2, RR: 3}
IMPACT = {"high": 3, "med": 2, "low": 1}
EASE = {"low": 3, "med": 2, "high": 1}          # low effort = easy = scores higher
DEADLINE_DAYS = {"high": 3, "med": 5, "low": 7}
MAX_TASKS = 5
NA = "DATA NOT AVAILABLE"
MIN_CLICKS = int(os.environ.get("ORCH_MIN_CLICKS", "30"))       # below this a click drop is too small a sample to alert on
CLICK_DROP = 0.20                                                  # role rule: more than 20% clicks drop
POS_DROP = 3.0                                                     # role rule: position worse by about 3+
RISKY = re.compile(r"(?i)redirect|noindex|url change|slug|permalink|delete|deletion|remove page|canonical|robots\.txt|content rewrite")
CLOSED = {"fixed", "ignored", "superseded", "resolved", "closed", "dismissed", "rejected", "done", "applied"}   # tech_issues.status is open|fixed|ignored|superseded
LLM_MODEL = os.environ.get("ORCH_MODEL") or os.environ.get("CONTENT_MODEL", "claude-sonnet-5-5")


# ---------------------------------------------------------------- reading Supabase
def q(table, **params):
    """Rows of a table, or None if the table does not exist / cannot be read (sibling agent not installed yet)."""
    try: return sb.select(table, **params)
    except Exception as e:
        q.errors[table] = str(e)[:120]; return None
q.errors = {}


def week_of(today): y, w, _ = today.isocalendar(); return f"{y}-W{w:02d}"
def d10(d): return d.isoformat()
def parse_ts(s):
    try: return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception: return None


def gather(site_id, today):
    """Everything the plan needs, as plain data. None means 'table missing or unreadable' (reported as DATA NOT AVAILABLE)."""
    st = {"site_id": site_id}; end = today - dt.timedelta(days=2)       # Search Console data is about 2 days old
    q.errors = {}
    srow = q("sites", id=f"eq.{site_id}", select="*"); st["site"] = (srow or [{}])[0] if srow is not None else None
    st["audits"] = q("page_audits_latest", site_id=f"eq.{site_id}", select="h1,images_no_alt,risky_claims,schema_issues,audited_at", limit="3000")
    st["proposals"] = q("proposals", site_id=f"eq.{site_id}", select="id,page,kind,status,reason,created_at,applied_at", order="id.desc", limit="1000")
    st["gsc"] = q("gsc_by_day", site_id=f"eq.{site_id}", date=f"gte.{d10(end - dt.timedelta(days=14))}", select="date,clicks,impressions", limit="2000")
    st["tracked"] = q("tracked_keywords", site_id=f"eq.{site_id}", active="eq.true", select="keyword", limit="1000")
    st["rank"] = q("rank_daily", site_id=f"eq.{site_id}", day=f"gte.{d10(end - dt.timedelta(days=12))}", select="keyword,device,day,position,impressions", limit="5000")
    st["posts"] = q("content_posts", site_id=f"eq.{site_id}", status="in.(draft,approved)", select="id,status,issues", limit="200")
    # sibling agents (may not exist yet)
    st["tech"] = q("tech_issues", site_id=f"eq.{site_id}", select="*", limit="500")
    st["kw"] = q("keyword_opps", site_id=f"eq.{site_id}", status="eq.new", select="source,priority,keyword,page_exists", limit="500")
    st["report"] = q("reports", site_id=f"eq.{site_id}", period="eq.week", select="created_at,period_end", order="created_at.desc", limit="1")
    st["missing"] = sorted(t for t in ("page_audits_latest", "proposals", "gsc_by_day", "tracked_keywords", "rank_daily", "content_posts", "tech_issues", "keyword_opps", "reports")
                           if (st.get({"page_audits_latest": "audits", "gsc_by_day": "gsc", "tracked_keywords": "tracked", "rank_daily": "rank", "content_posts": "posts",
                                       "tech_issues": "tech", "keyword_opps": "kw", "reports": "report"}.get(t, t)) is None))
    return st


# ---------------------------------------------------------------- signals (pure functions on the gathered data)
def click_trend(gsc, today):
    """Clicks last 7 days vs the 7 before, from Search Console. Returns dict or None when the sample is too thin."""
    if not gsc: return None
    end = today - dt.timedelta(days=2); by = {}
    for r in gsc: by[r["date"]] = by.get(r["date"], 0) + float(r.get("clicks") or 0)
    rec = [by[d10(end - dt.timedelta(days=i))] for i in range(7) if d10(end - dt.timedelta(days=i)) in by]
    prv = [by[d10(end - dt.timedelta(days=i))] for i in range(7, 14) if d10(end - dt.timedelta(days=i)) in by]
    if len(rec) < 5 or len(prv) < 5: return None
    r, p = sum(rec), sum(prv)
    if p < MIN_CLICKS: return {"recent": r, "prev": p, "change": None, "thin": True}
    return {"recent": r, "prev": p, "change": (r - p) / p, "thin": False}


def rank_drops(rank, today):
    """Tracked keywords whose position got about 3+ worse (last 4 days vs the 7 days before), per device."""
    end = today - dt.timedelta(days=2); cut = d10(end - dt.timedelta(days=3)); lo = d10(end - dt.timedelta(days=10)); g = {}
    for r in rank or []:
        if r.get("position") is None: continue
        g.setdefault((r["keyword"], r["device"]), {"rec": [], "prv": [], "impr": 0})
        if r["day"] >= cut: g[(r["keyword"], r["device"])]["rec"].append(float(r["position"])); g[(r["keyword"], r["device"])]["impr"] += int(r.get("impressions") or 0)
        elif r["day"] >= lo: g[(r["keyword"], r["device"])]["prv"].append(float(r["position"]))
    out = []
    for (kw, dev), v in g.items():
        if len(v["rec"]) < 2 or len(v["prv"]) < 3 or v["impr"] < 10: continue     # tiny samples are not alerts
        d = sum(v["rec"]) / len(v["rec"]) - sum(v["prv"]) / len(v["prv"])
        if d >= POS_DROP: out.append({"keyword": kw, "device": dev, "worse_by": round(d, 1)})
    return sorted(out, key=lambda x: -x["worse_by"])


def audit_issue_counts(audits):
    a = audits or []
    return {"pages": len(a), "h1": sum(1 for r in a if r.get("h1") != 1), "alt": sum(1 for r in a if (r.get("images_no_alt") or 0) > 0),
            "risky": sum(1 for r in a if r.get("risky_claims")), "schema": sum(1 for r in a if any(not str(s).startswith("h1_") for s in (r.get("schema_issues") or [])))}


def oldest_audit_days(audits, today):
    ts = [parse_ts(r.get("audited_at")) for r in audits or []]; ts = [t for t in ts if t]
    return (today - min(ts).date()).days if ts else None


# ---------------------------------------------------------------- tasks
def T(task, agent, impact, effort, approval, n=0, evidence=""):
    return {"task": task, "agent": agent, "impact": impact, "effort": effort, "needs_approval": bool(approval or RISKY.search(task)), "_n": n, "_ev": evidence}


def candidates(st, today):
    """All candidate tasks + risks + evidence for one site. Deterministic. Risky work is always needs_approval."""
    site = st["site"] or {}; tasks, risks, ev = [], [], []
    medical = site.get("profile") == "medical" or bool(site.get("reviewer_required"))
    if st["site"] is None: risks.append("Site row could not be read from Supabase: " + NA)
    if site.get("mode") == "paused": risks.append("Site is paused in the dashboard: no agent work will run until it is switched on.")
    if site.get("mode") == "live": risks.append("Mode is live: title/meta proposals are applied automatically (1 page/day). Check Proposals regularly.")
    if medical: risks.append("Medical (YMYL) site: every content change needs the named medical reviewer before it goes live.")

    # --- search data health and alerts (Rank Tracker & Reporter / Technical Auditor)
    if st["gsc"] is not None:
        last = max((r["date"] for r in st["gsc"]), default=None)
        if last is None or (today - dt.date.fromisoformat(last)).days > 5:
            tasks.append(T(f"Find out why Search Console data is stale (last day with data: {last or NA})", TA, "high", "low", False, 1, "gsc_by_day"))
        tr = click_trend(st["gsc"], today)
        if tr is None: risks.append("Click trend: " + NA + " (fewer than 5 days of data in a 7-day window).")
        elif tr["thin"]: risks.append(f"Click trend not judged: only {int(tr['prev'])} clicks in the previous week (sample too small).")
        else:
            ev.append(f"gsc_by_day: clicks {int(tr['recent'])} vs {int(tr['prev'])} (last 7 days vs previous 7)")
            if tr["change"] <= -CLICK_DROP:
                tasks.append(T(f"Investigate click drop of {abs(tr['change']) * 100:.0f}% (last 7 days vs previous 7). Check: algorithm update, technical issue, competitor, seasonality. Correlation is not proof of cause.", RR, "high", "low", False, int(tr["prev"] - tr["recent"]), "gsc_by_day"))
    else: risks.append("Search Console data (gsc_by_day): " + NA)
    if st["rank"] is not None:
        drops = rank_drops(st["rank"], today)
        if drops:
            tasks.append(T(f"Investigate {len(drops)} tracked keyword(s) that fell about {POS_DROP:.0f}+ positions (worst: '{drops[0]['keyword']}' on {drops[0]['device']}, {drops[0]['worse_by']} worse)", RR, "high", "low", False, len(drops), "rank_daily"))
            ev.append(f"rank_daily: {len(drops)} keyword/device pairs worse by 3+")
    if st["tracked"] is not None and len(st["tracked"]) == 0:
        tasks.append(T("Add tracked keywords for the main pages so rank alerts and experiments have data", KR, "med", "low", False, 1, "tracked_keywords"))

    # --- the agents' own queue (Content & On-Page Optimizer)
    props = st["proposals"]
    if props is not None:
        by = {}
        for p in props: by.setdefault(p["status"], []).append(p)
        pend = by.get("pending", []); approved = by.get("approved", [])
        ev.append(f"proposals: {len(pend)} pending, {len(approved)} approved, {len(by.get('failed', []))} failed, {len(by.get('applied', []))} applied")
        if pend:
            kinds = sorted({p["kind"] for p in pend})
            tasks.append(T(f"Review and approve or reject {len(pend)} pending proposal(s) ({', '.join(kinds)})", CO, "high" if len(pend) >= 5 else "med", "low", True, len(pend), "proposals"))
            seen = {}
            for p in pend: seen.setdefault((p["page"], p["kind"]), 0); seen[(p["page"], p["kind"])] += 1
            dup = [k for k, v in seen.items() if v > 1]
            if dup: risks.append(f"Conflict: {len(dup)} page(s) have two pending proposals of the same kind (e.g. {dup[0][0]}). Approve only one.")
        cut = today - dt.timedelta(days=14)
        fail = [p for p in by.get("failed", []) if (parse_ts(p.get("created_at")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)).date() >= cut]
        if fail: tasks.append(T(f"Re-check {len(fail)} failed proposal(s) from the last 14 days (reason on the Proposals page), then re-run or reject them", CO, "med", "low", False, len(fail), "proposals"))
        if len(approved) >= 3: risks.append(f"{len(approved)} approved proposals are waiting to be applied (next daily run applies them).")
    else: risks.append("Proposals: " + NA)

    # --- on-page issues from the built-in audit (Technical Auditor / Content & On-Page Optimizer)
    if st["audits"] is not None:
        c = audit_issue_counts(st["audits"]); age = oldest_audit_days(st["audits"], today)
        if c["pages"] == 0: tasks.append(T("Run the full page audit (audit-all) so issues can be found: no audit data yet", TA, "med", "low", False, 1, "page_audits_latest"))
        else:
            ev.append(f"page_audits_latest: {c['pages']} pages, issues h1={c['h1']}, no-alt={c['alt']}, risky-claims={c['risky']}, schema={c['schema']}")
            if c["risky"]: tasks.append(T(f"Propose safe rewording for {c['risky']} page(s) with risky claims (guarantee / 100% / cure type words)", CO, "high", "low", True, c["risky"], "page_audits_latest"))
            if c["schema"]: tasks.append(T(f"Fix invalid or risky schema on {c['schema']} page(s) (as proposals)", TA, "med", "low", True, c["schema"], "page_audits_latest"))
            if c["h1"]: tasks.append(T(f"Fix H1 (none or more than one) on {c['h1']} page(s)", TA, "med", "med", True, c["h1"], "page_audits_latest"))
            if c["alt"]: tasks.append(T(f"Add alt text to images on {c['alt']} page(s) (bulk, as proposals)", CO, "med", "med", True, c["alt"], "page_audits_latest"))
            if age is not None and age > 30: tasks.append(T(f"Re-run the page audit: oldest audit is {age} days old", TA, "low", "low", False, age, "page_audits_latest"))
    else: risks.append("Page audits: " + NA)

    # --- Technical Auditor output (schema_v17, optional)
    if st["tech"] is None: risks.append("Technical Auditor results (tech_issues): " + NA + " (schema_v17 / tech_audit not run yet).")
    else:
        op = [r for r in st["tech"] if str(r.get("status") or "open").lower() not in CLOSED]
        crit = [r for r in op if str(r.get("severity") or "").lower() in ("critical", "high")]
        if crit:
            tasks.append(T(f"Fix {len(crit)} Critical/High technical issue(s) from the latest technical audit (see Tech page; fixes go through approval)", TA, "high", "med", True, len(crit), "tech_issues"))
            ev.append(f"tech_issues: {len(crit)} critical/high open of {len(op)} open")

    # --- Keyword & SERP Researcher output (schema_v18, optional)
    if st["kw"] is None: risks.append("Keyword research results (keyword_opps): " + NA + " (schema_v18 / keywords not run yet).")
    else:
        sd = [r for r in st["kw"] if r.get("source") == "striking_distance"]; cn = [r for r in st["kw"] if r.get("source") == "cannibalization"]
        if sd:
            tasks.append(T(f"Review {len(sd)} striking-distance keyword(s) (position 8-20) and approve the ones to push", KR, "high", "low", True, len(sd), "keyword_opps"))
            ev.append(f"keyword_opps: {len(sd)} striking distance, {len(cn)} cannibalization (status new)")
        if cn:
            tasks.append(T(f"Decide one page per keyword for {len(cn)} cannibalization case(s) (merge or redirect needs your approval)", KR, "high", "med", True, len(cn), "keyword_opps"))
            risks.append(f"Conflict: {len(cn)} keyword(s) are targeted by more than one page. One keyword = one page; do not let two agents optimise the same keyword on different URLs.")

    # --- Reporter output (schema_v20, optional)
    if st["report"] is None: risks.append("Weekly report (reports): " + NA + " (schema_v20 / report not run yet).")
    else:
        ts = parse_ts((st["report"][0] if st["report"] else {}).get("created_at"))
        if ts is None or (today - ts.date()).days > 7:
            tasks.append(T("Generate this week's report (Hinglish summary, winners/losers, alerts)", RR, "med", "low", False, 1, "reports"))

    # --- content pipeline
    if st["posts"] is not None:
        dr = [p for p in st["posts"] if p["status"] == "draft"]; clean = [p for p in dr if not p.get("issues")]
        if dr:
            who = "medical reviewer sign-off" if medical else "your check"
            tasks.append(T(f"Review {len(dr)} draft post(s) ({len(clean)} without issues) before publishing; needs {who}", CO, "med", "low", True, len(dr), "content_posts"))
            ev.append(f"content_posts: {len(dr)} draft")
    return tasks, risks, ev


def rank_tasks(tasks, today):
    """High impact x low effort first. Stable and explainable: score, then number of items affected, then agent order, then text."""
    for t in tasks: t["_score"] = IMPACT[t["impact"]] * EASE[t["effort"]]
    tasks = sorted(tasks, key=lambda t: (-t["_score"], -t["_n"], AGENT_ORDER[t["agent"]], t["task"]))[:MAX_TASKS]
    out = []
    for t in tasks:
        o = {k: t[k] for k in ("task", "agent", "impact", "effort", "needs_approval")}
        o["deadline"] = d10(today + dt.timedelta(days=DEADLINE_DAYS[t["impact"]])); out.append(o)
    return out


# ---------------------------------------------------------------- summary in Hinglish
def template_summary(site_id, week, tasks, risks, st):
    n = len(tasks); a = sum(1 for t in tasks if t["needs_approval"])
    if not n: l1 = f"{site_id} ({week}): is hafte koi naya kaam nahi nikla."
    else: l1 = f"{site_id} ({week}): is hafte {n} kaam hain, inme se {a} me aapki approval chahiye."
    l2 = f"Sabse pehle: {tasks[0]['task'][:150]}" if n else "Agents ke paas abhi pending kaam nahi hai."
    tr = click_trend(st["gsc"], st["today"]) if st.get("gsc") is not None else None
    if tr and not tr["thin"]: l3 = f"Clicks pichhle 7 din me {int(tr['recent'])}, usse pehle ke 7 din me {int(tr['prev'])} the (Search Console data)."
    else: l3 = "Clicks ka comparison abhi available nahi hai (DATA NOT AVAILABLE ya sample bahut chhota)."
    l4 = ("Dhyan rakhna: " + risks[0][:150]) if risks else "Koi bada risk nahi dikha."
    l5 = "Koi bhi change live site par tabhi jayega jab aap Proposals page me approve karoge."
    return "\n".join([l1, l2, l3, l4, l5])


def llm_summary(plan, base, ask=None):
    """Optional: let Claude re-phrase the template in warmer Hinglish. Used only if the answer is 5 lines max and adds no new number."""
    if not ask and (os.environ.get("ORCH_LLM") == "0" or not os.environ.get("ANTHROPIC_API_KEY")): return None
    facts = json.dumps({k: plan[k] for k in ("site", "week", "priority_tasks", "risks")}, ensure_ascii=False)
    system = ("Tum SEO Orchestrator ho. Neeche JSON me ek site ka weekly plan hai. Owner ke liye simple Hinglish me exactly 5 chhoti lines likho. "
              "Sirf JSON me diye gaye facts aur numbers use karo, koi naya number ya claim mat jodo. Ranking ya result ka vaada mat karo. Sirf 5 lines, bina bullets.")
    try:
        if ask: out = ask(system, facts)
        else:
            import anthropic
            r = anthropic.Anthropic().messages.create(model=LLM_MODEL, max_tokens=500, system=system, messages=[{"role": "user", "content": facts}])
            out = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
    except Exception as e:
        print("note: Claude not used for the summary:", str(e)[:100]); return None
    lines = [l.strip(" -*\t") for l in out.strip().splitlines() if l.strip()]
    ok_nums = set(re.findall(r"\d+", facts))
    if not (1 <= len(lines) <= 5) or not set(re.findall(r"\d+", out)) <= ok_nums or re.search(r"(?i)guarantee|100\s?%|#1|number one", out): return None
    return "\n".join(lines)


# ---------------------------------------------------------------- plan
def build_plan(site_id, st, today, ask=None):
    st = dict(st, today=today); week = week_of(today)
    tasks, risks, ev = candidates(st, today)
    paused = (st["site"] or {}).get("mode") == "paused"
    plan_tasks = [] if paused else rank_tasks(tasks, today)
    if len(tasks) > MAX_TASKS and not paused: risks.append(f"{len(tasks) - MAX_TASKS} more task(s) were left for later weeks (limit: {MAX_TASKS} per site per week).")
    core_missing = [m for m in st["missing"] if m in ("gsc_by_day", "page_audits_latest", "proposals")]
    miss = st["missing"]
    conf = "Low" if len(core_missing) >= 2 else ("Medium" if miss else "High")
    if st["site"] is None: conf = "Low"
    if miss: ev.append("tables not available: " + ", ".join(miss))
    plan = {"site": site_id, "week": week, "priority_tasks": plan_tasks, "risks": risks, "summary_for_owner": "", "confidence": conf, "evidence": ev or [NA]}
    base = template_summary(site_id, week, plan_tasks, risks, st)
    llm = llm_summary(plan, base, ask)
    plan["summary_for_owner"] = llm or base
    return plan, bool(llm)


def run(site_id, today=None, write=True, ask=None):
    today = today or dt.date.today(); st = gather(site_id, today)
    plan, llm = build_plan(site_id, st, today, ask)
    print(json.dumps(plan, ensure_ascii=False, indent=1))
    if write:
        sb.upsert("weekly_plans", [{"site_id": site_id, "week": plan["week"], "plan": plan, "summary_for_owner": plan["summary_for_owner"],
                                    "needs_approval_count": sum(1 for t in plan["priority_tasks"] if t["needs_approval"]), "llm_used": llm}], "site_id,week")
        print(f"saved weekly plan {site_id} {plan['week']}")
    return plan


if __name__ == "__main__":
    from . import run as _r
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args: raise SystemExit("usage: python -m seo_agent.orchestrator <site_id|all> [--print]")
    _r.each(args[0], lambda sid: run(sid, write="--print" not in sys.argv))
