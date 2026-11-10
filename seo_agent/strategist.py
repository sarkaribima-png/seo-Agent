"""Strategist: site goals + closed loop on every applied change.
Run: python -m seo_agent.strategist <site_id|all> [--print]      (daily, .github/workflows/strategist.yml)

For each applied proposal: snapshot a GSC baseline (14 days before the change), then
  day 3   -> indexed / crawled check (URL Inspection API if the service account can, else "impressions seen" from gsc_daily)
  day 14  -> impressions-per-day delta
  day 28..45 -> verdict improved / flat / worse / too_early / DATA NOT AVAILABLE
Minimum-sample rule: fewer than MIN_IMPR impressions after the change -> 'too_early'. We never claim the change CAUSED anything ("ho sakta hai").
worse -> a pending follow-up proposal (rollback suggestion or re-optimize) that a human must approve. flat -> next lever. improved -> winner pattern.
Claude (llm.ask_json) only phrases next_action; any reply with a new number, a guarantee or a causal claim is dropped for the template.
Also: goals-aware prioritizer for the orchestrator (apply_goals / goal_boosts). Agents never touch a live site from here.
"""
import sys, re, json, os, datetime as dt
from collections import defaultdict
from . import sb

LAG = 2                                                   # Search Console data is ~2 days old
BASE_DAYS = 14                                            # baseline window before the change
POST_MAX = 28
MIN_IMPR = int(os.environ.get("STRAT_MIN_IMPR", "100"))   # impressions after the change needed before any verdict
MIN_POS_IMPR = 30                                         # impressions needed on each side to compare position
POS_GAIN, POS_LOSS = 1.0, 1.5                             # positions
IMPR_UP, IMPR_DOWN = 25.0, -30.0                          # % change of impressions per day
CLOSE_DAY = 45
LOOKBACK = 50
NA = "DATA NOT AVAILABLE"
HEDGE = "ho sakta hai"
LEVERS = {"title_meta": ["internal_link", "content_add", "schema"], "internal_link": ["content_add", "title_meta", "schema"],
          "content_add": ["title_meta", "internal_link", "schema"], "schema": ["title_meta", "internal_link", "content_add"],
          "alt_text": ["title_meta", "internal_link", "content_add"]}
LEVER_TEXT = {"title_meta": "title aur meta description ko search query ke hisaab se dobara likhna", "internal_link": "related pages se is page par internal link jodna",
              "content_add": "page par ek useful section (FAQ / detail) jodna", "schema": "sahi structured data (schema) jodna", "alt_text": "images ka alt text theek karna"}
BAD = re.compile(r"(?i)guarant|100\s*%|#\s*1\b|number\s*(one|1)\b|rank(ing)?s?\s+(pakka|guarantee)|because of (this|our|the) change|is change (ki wajah|se hi)|isi (wajah|change) se|caused by|due to (our|this) change|proves?\b|pakka")
POST_TASK = re.compile(r"(?i)\b(new|naya|nayi|write|draft|publish)\b.*\b(post|article|blog)s?\b|\b(post|article|blog)s?\b.*\b(write|draft|publish|likhna)\b|content calendar")


# ---------------------------------------------------------------- small helpers
def D(s): return dt.date.fromisoformat(str(s)[:10])
def now_iso(): return dt.datetime.now(dt.timezone.utc).isoformat()
def norm(u):
    u = re.sub(r"^https?://(www\.)?", "", str(u or "").strip().lower()); u = re.split(r"[?#]", u)[0]
    return u.rstrip("/")
def sdiv(a, b):
    """a / b, or None when b is missing or zero (never a ZeroDivisionError)."""
    try: return None if a is None or not b else a / b
    except (TypeError, ZeroDivisionError): return None
def pct_change(now, base): r = sdiv(now, base); return None if r is None else round((r - 1) * 100, 1)
def num(x):
    try: return None if x is None else float(x)
    except (TypeError, ValueError): return None


def agg(rows, a, b):
    """Totals for rows with a <= date <= b: clicks, impressions, impression-weighted position (None if no impressions)."""
    c = i = pw = 0.0; days = set()
    for r in rows:
        if not (a <= D(r["date"]) <= b): continue
        ci, ii, pi = num(r.get("clicks")) or 0, num(r.get("impressions")) or 0, num(r.get("position"))
        c += ci; i += ii; days.add(str(r["date"])[:10])
        if pi is not None: pw += pi * ii
    return {"clicks": round(c, 2), "impressions": round(i, 2), "position": round(pw / i, 2) if i > 0 else None, "days_with_data": len(days)}


def baseline_for(rows, applied, today):
    """Baseline from gsc_daily: the BASE_DAYS before the change day. `complete` is False while Search Console has not caught up yet."""
    a, b = applied - dt.timedelta(days=BASE_DAYS), applied - dt.timedelta(days=1)
    if not rows: return {"clicks": None, "impressions": None, "position": None, "days": BASE_DAYS, "window": [str(a), str(b)], "complete": False, "note": NA, "captured_at": now_iso()}
    g = agg(rows, a, b)
    return {**g, "days": BASE_DAYS, "window": [str(a), str(b)], "complete": b <= today - dt.timedelta(days=LAG), "captured_at": now_iso(), "source": "gsc_daily"}


def post_window(applied, today):
    a = applied + dt.timedelta(days=1); b = min(today - dt.timedelta(days=LAG), applied + dt.timedelta(days=POST_MAX))
    return a, b, max((b - a).days + 1, 0)


# ---------------------------------------------------------------- checks
def check_day3(fu, rows, today, inspect=None):
    """Indexed / crawled. URL Inspection if available, else 'impressions seen' from our own gsc_daily (an inference, labelled)."""
    applied = D(fu["applied_at"]); res = None
    if inspect:
        try: res = inspect(fu["url"], fu)
        except Exception as e: res = {"error": str(e)[:100]}
    if res and not res.get("error") and res.get("indexed") is not None:
        crawl = res.get("last_crawl"); after = None
        if crawl: after = D(crawl) >= applied
        return {"status": "indexed" if res["indexed"] else "not_indexed", "crawled_after_change": after, "last_crawl": crawl, "source": "url_inspection", "checked_at": str(today)}
    a, b, n = post_window(applied, today); seen = agg(rows, a, b)["impressions"] if n else 0
    if seen and seen > 0: return {"status": "indexed", "crawled_after_change": None, "source": "gsc_impressions", "note": "impressions mile hain, isliye page Google me dikh raha hai (URL Inspection data nahi)", "checked_at": str(today)}
    return {"status": "unknown", "source": "none", "note": NA + ": URL Inspection ya impressions nahi mile. Search Console me URL Inspection se manually dekho.", "checked_at": str(today)}


def check_day14(fu, rows, today):
    applied = D(fu["applied_at"]); a, b, n = post_window(applied, today); p = agg(rows, a, b); base = fu.get("baseline") or {}
    br = sdiv(num(base.get("impressions")), num(base.get("days"))); pr = sdiv(p["impressions"], n)
    return {"days_after": n, "impressions": p["impressions"], "clicks": p["clicks"], "impr_per_day_before": None if br is None else round(br, 2),
            "impr_per_day_after": None if pr is None else round(pr, 2), "impr_delta_pct": pct_change(pr, br) if br else None,
            "note": None if br else "baseline impressions 0 ya missing: % change " + NA, "checked_at": str(today)}


def site_trend(rows, applied, today):
    """Whole-site impressions/day change over the same two windows (context only: seasonality, algorithm updates)."""
    b0, b1 = applied - dt.timedelta(days=BASE_DAYS), applied - dt.timedelta(days=1); a, b, n = post_window(applied, today)
    if not n: return None
    base = agg(rows, b0, b1)["impressions"]
    if base < MIN_IMPR * 3: return None
    return pct_change(sdiv(agg(rows, a, b)["impressions"], n), sdiv(base, BASE_DAYS))


def verdict_for(fu, page_rows, site_rows, today):
    """Returns dict(verdict, confidence, pos_delta, impr_delta_pct, ...). Pure function of numbers, no LLM."""
    applied = D(fu["applied_at"]); a, b, n = post_window(applied, today); base = fu.get("baseline") or {}
    p = agg(page_rows, a, b); bi, bp = num(base.get("impressions")), num(base.get("position"))
    out = {"post_days": n, "post_impressions": p["impressions"], "post_clicks": p["clicks"], "post_position": p["position"], "base_impressions": bi, "base_position": bp,
           "min_impressions": MIN_IMPR, "checked_at": str(today)}
    if bi is None and not p["days_with_data"]:
        return {**out, "verdict": NA, "confidence": "Low", "reason": "baseline aur change ke baad ka GSC data dono nahi mile"}
    br = sdiv(bi, base.get("days")); pr = sdiv(p["impressions"], n); expected = (br or 0) * n
    st = site_trend(site_rows, applied, today); out["site_impr_trend_pct"] = st
    delta = pct_change(pr, br) if br else None; out["impr_delta_pct"] = delta
    adj = delta if (delta is None or st is None) else round(delta - st, 1); out["impr_delta_vs_site_pct"] = adj
    pos_delta = None
    if bp is not None and p["position"] is not None and (bi or 0) >= MIN_POS_IMPR and p["impressions"] >= MIN_POS_IMPR: pos_delta = round(bp - p["position"], 2)   # + = better
    out["pos_delta"] = pos_delta
    if p["impressions"] < MIN_IMPR:                           # minimum-sample rule
        if expected >= MIN_IMPR * 2 and p["impressions"] <= 0.3 * expected and n >= 14 and bi is not None:
            return {**out, "verdict": "worse", "confidence": "Medium", "reason": "page ke impressions pehle ke mukable bahut kam dikh rahe hain (baseline bada tha)"}
        return {**out, "verdict": "too_early", "confidence": "Low", "reason": f"change ke baad sirf {int(p['impressions'])} impressions (kam se kam {MIN_IMPR} chahiye)"}
    up = (pos_delta is not None and pos_delta >= POS_GAIN) or (adj is not None and adj >= IMPR_UP) or (br in (None, 0) and p["impressions"] >= MIN_IMPR)
    down = (pos_delta is not None and pos_delta <= -POS_LOSS) or (adj is not None and adj <= IMPR_DOWN)
    v = "flat" if up == down else ("improved" if up else "worse")
    conf = "High" if (p["impressions"] >= 5 * MIN_IMPR and n >= 21 and (bi or 0) >= MIN_IMPR and st is None) else ("Medium" if (bi or 0) >= MIN_POS_IMPR else "Low")
    if st is not None and abs(st) >= IMPR_UP: conf = "Low"; out["note"] = "poori site ka trend bhi badla tha (season ya Google update ho sakta hai), isliye asar alag nahi kiya ja sakta"
    if br in (None, 0): out["note"] = "baseline impressions 0 ya missing: sirf change ke baad ki visibility dekhi gayi, % change " + NA
    return {**out, "verdict": v, "confidence": conf, "reason": "position / impressions ka comparison, minimum sample poora"}


# ---------------------------------------------------------------- next action (template + Claude)
def pick_lever(kind, tried):
    for k in LEVERS.get(kind or "", ["title_meta", "internal_link", "content_add"]):
        if k not in tried: return k
    return None


def template_action(verdict, fu, res, lever=None, action=None):
    kind = fu.get("kind") or "change"
    if verdict == "improved": return f"Is {kind} ke baad page ka performance behtar dikha, ye change kaam kar gaya ho sakta hai. Isi pattern ko dusre pages par try karo (proposal banakar)."
    if verdict == "worse":
        if action == "rollback_suggestion": return f"Is {kind} ke baad page ka performance kam dikha; ye change wajah ho sakta hai. Rollback ka suggestion approval ke liye bana hai, pehle check karke decide karo."
        return f"Is {kind} ke baad page ka performance kam dikha, ye change wajah ho sakta hai. Page ko dobara optimize karne ka proposal approval ke liye bana hai."
    if verdict == "flat": return "Abhi koi saaf farak nahi dikha, ho sakta hai ye change akela kaafi nahi tha. Agla step: " + LEVER_TEXT.get(lever, "koi doosra lever") + " (proposal banakar, approval ke saath)."
    if verdict == NA: return "GSC data nahi mila, isliye " + NA + ". Search Console connection check karo."
    return "Abhi data kam hai (too_early): " + str(res.get("reason") or "") + ". Kuch din baad dobara check hoga."


def nums_in(s): return set(re.findall(r"\d+(?:\.\d+)?", str(s)))


def llm_action(fu, res, lever, action, ask_json=None):
    """Claude phrases the rationale. Reject on: new numbers, guarantees, causal wording, missing hedge, bad shape. Returns text or None."""
    try:
        if ask_json is None: from .llm import ask_json
        facts = {"kind": fu.get("kind"), "url": fu.get("url"), "verdict": res["verdict"], "confidence": res["confidence"], "numbers": {k: res.get(k) for k in ("post_impressions", "base_impressions", "pos_delta", "impr_delta_pct", "post_days")}, "next_lever": LEVER_TEXT.get(lever), "action": action, "note": res.get("note")}
        system = ("Tum SEO Strategist ho. Ek applied change ka result simple Hinglish me 2 sentence me samjhao aur agla step batao. Rules: sirf diye gaye numbers use karo, naya number kabhi nahi; "
                  "kabhi mat kaho ki change ki wajah se hua (sirf 'ho sakta hai'); koi ranking/guarantee claim nahi; competitor ka naam nahi; live change sirf proposal + approval se. "
                  'Reply sirf JSON: {"next_action": "..."}')
        r = ask_json(system, facts, agent="strategist", site_id=fu.get("site_id"), max_tokens=400)
        t = str((r or {}).get("next_action") or "").strip() if isinstance(r, dict) else ""
    except Exception as e:
        print("strategist: LLM not used:", str(e)[:80]); return None
    allowed = nums_in(json.dumps(facts, default=str)) | nums_in(fu.get("url"))
    if not t or len(t) > 500 or BAD.search(t) or (nums_in(t) - allowed): return None
    if res["verdict"] in ("improved", "flat", "worse") and HEDGE not in t.lower(): return None
    return t


# ---------------------------------------------------------------- data access
def q(table, **p):
    try: return sb.select(table, **p)
    except Exception as e: print(f"strategist: {table} not readable: {str(e)[:80]}"); return None


def fetch_gsc(site_id, since, page=1000, cap=100):
    out = []
    for i in range(cap):
        rows = sb.select("gsc_daily", site_id=f"eq.{site_id}", select="date,page,clicks,impressions,position", order="date", date=f"gte.{since}", limit=str(page), offset=str(i * page))
        out += rows
        if len(rows) < page: break
    return out


def load_goals(site_id):
    r = q("site_goals", site_id=f"eq.{site_id}")
    g = (r or [None])[0]
    if g and g.get("active") is False: g = None
    return g or {"site_id": site_id, "goal_text": "", "target_keywords": [], "money_pages": [], "monthly_post_cap": 8, "autonomy_level": 2, "active": True, "_default": True}


def make_inspector(site_id):
    """URL Inspection through the same service account as gsc.py. Returns None if it is not configured (the day-3 check then falls back)."""
    if not os.environ.get("GSC_SERVICE_ACCOUNT_JSON"): return None
    try:
        from . import gsc, run as runmod
        prop = runmod.sites()[site_id]["gsc_property"]; svc = gsc.service()
    except Exception as e: print("strategist: URL Inspection off:", str(e)[:80]); return None
    def inspect(url, fu):
        r = svc.urlInspection().index().inspect(body={"inspectionUrl": url, "siteUrl": prop}).execute().get("inspectionResult", {}).get("indexStatusResult", {})
        return {"indexed": None if not r else r.get("verdict") == "PASS", "last_crawl": r.get("lastCrawlTime")}
    return inspect


# ---------------------------------------------------------------- loop
def sync_new(site_id, today, site_rows):
    """Create a followup row (with baseline) for every applied proposal that has none yet."""
    have = {r["proposal_id"] for r in (q("change_log_followups", site_id=f"eq.{site_id}", select="proposal_id") or [])}
    since = (today - dt.timedelta(days=LOOKBACK)).isoformat(); new = []
    for p in sb.select("proposals", site_id=f"eq.{site_id}", status="eq.applied", applied_at=f"gte.{since}"):
        if p["id"] in have or not p.get("applied_at") or p.get("kind") == "strategy_followup" or p.get("source") == "strategist": continue
        rows = [r for r in site_rows if norm(r["page"]) == norm(p["page"])]
        new.append({"proposal_id": p["id"], "site_id": site_id, "url": p["page"], "kind": p.get("kind"), "applied_at": p["applied_at"], "baseline": baseline_for(rows, D(p["applied_at"]), today)})
    if new: sb.insert("change_log_followups", new)
    return new


def spawn_followup(fu, res, action, goals, prop):
    """worse -> pending proposal (needs approval). Level 1 = observe only."""
    if (goals.get("autonomy_level") or 2) < 2 or fu.get("followup_proposal_id") or res["verdict"] != "worse": return None
    row = {"site_id": fu["site_id"], "page": fu["url"], "post_id": (prop or {}).get("post_id"), "kind": "strategy_followup", "needs_approval": True, "status": "pending", "source": "strategist",
           "old": {"proposal_id": fu["proposal_id"], "kind": fu.get("kind")}, "new": {"action": action, "of_proposal": fu["proposal_id"], "verdict": "worse", "confidence": res["confidence"]},
           "reason": (fu.get("next_action") or "")[:300]}
    return sb.insert("proposals", [row])[0].get("id")


def process(fu, site_rows, today, goals, props, inspect=None, ask_json=None):
    """Advance one followup. Returns the update dict (also what is saved)."""
    applied = D(fu["applied_at"]); age = (today - applied).days; page_rows = [r for r in site_rows if norm(r["page"]) == norm(fu["url"])]; up = {}
    base = fu.get("baseline") or {}
    if base.get("complete") is False and age > LAG:
        nb = baseline_for(page_rows, applied, today)
        if nb.get("complete") or nb.get("impressions") is not None: up["baseline"] = nb; fu = {**fu, "baseline": nb}
    if age >= 3 and age < 14 and (fu.get("check_day3") or {}).get("status") != "indexed": up["check_day3"] = check_day3(fu, page_rows, today, inspect)
    if age >= 14 and not fu.get("check_day14"): up["check_day14"] = check_day14(fu, page_rows, today)
    if age >= 28 and (fu.get("status") != "closed"):
        res = verdict_for(fu, page_rows, site_rows, today); up["check_day28"] = res; up["verdict"] = res["verdict"]; up["confidence"] = res["confidence"]
        tried = {p["kind"] for p in props if norm(p.get("page")) == norm(fu["url"]) and p.get("status") == "applied"}
        lever = pick_lever(fu.get("kind"), tried) if res["verdict"] == "flat" else None
        prop = next((p for p in props if p["id"] == fu["proposal_id"]), None)
        action = ("rollback_suggestion" if prop and prop.get("backup_id") else "reoptimize") if res["verdict"] == "worse" else None
        text = llm_action(fu, res, lever, action, ask_json) if res["verdict"] in ("improved", "flat", "worse") else None; src = "llm" if text else "template"
        up["next_action"] = text or template_action(res["verdict"], fu, res, lever, action)
        up["meta"] = {**(fu.get("meta") or {}), "lever": lever, "action": action, "text_source": src, "evidence": f"gsc_daily {base.get('window')} vs {str(applied + dt.timedelta(days=1))}..+{res['post_days']}d"}
        if res["verdict"] == "improved":
            up["winner_pattern"] = {"kind": fu.get("kind"), "url": fu["url"], "pos_delta": res.get("pos_delta"), "impr_delta_pct": res.get("impr_delta_pct"), "confidence": res["confidence"], "note": "ho sakta hai ye pattern kaam karta hai; causation proven nahi"}
        if res["verdict"] == "worse":
            fid = spawn_followup({**fu, "next_action": up["next_action"]}, res, action, goals, prop)
            if fid: up["followup_proposal_id"] = fid
        if res["verdict"] not in ("too_early", NA): up["status"] = "closed"          # final verdict: frozen, no repeat LLM calls or follow-ups
    elif age >= 14 and fu.get("verdict") == "too_early" and not fu.get("next_action"): up["next_action"] = "Abhi data kam hai (too_early). Day 28 par verdict aayega."
    if age > CLOSE_DAY and fu.get("status") != "closed": up["status"] = "closed"
    if up:
        up["updated_at"] = now_iso(); sb.update("change_log_followups", {"id": fu["id"]}, up)
    return up


def run(site_id, today=None, inspect="auto", ask_json=None):
    today = today or dt.date.today(); goals = load_goals(site_id)
    if (q("sites", id=f"eq.{site_id}") or [{}])[0].get("mode") == "paused": print("site paused: strategist skipped"); return {"site": site_id, "skipped": "paused"}
    open_f = q("change_log_followups", site_id=f"eq.{site_id}", status="eq.open")
    if open_f is None: return {"site": site_id, "error": "change_log_followups missing: run sql/schema_v24.sql"}
    props = q("proposals", site_id=f"eq.{site_id}", select="id,page,kind,status,backup_id,post_id") or []
    lo = today - dt.timedelta(days=LOOKBACK + BASE_DAYS + 2)
    try: rows = fetch_gsc(site_id, lo.isoformat())
    except Exception as e: rows = []; print("strategist: gsc_daily not readable:", str(e)[:80])
    new = sync_new(site_id, today, rows); open_f = q("change_log_followups", site_id=f"eq.{site_id}", status="eq.open") or []
    if inspect == "auto": inspect = make_inspector(site_id) if any(0 < (today - D(f["applied_at"])).days < 14 for f in open_f) else None
    out = {"site": site_id, "new_baselines": len(new), "updated": 0, "verdicts": defaultdict(int)}
    for fu in open_f:
        up = process(fu, rows, today, goals, props, inspect, ask_json)
        if up: out["updated"] += 1
        if "verdict" in up: out["verdicts"][up["verdict"]] += 1
    out["verdicts"] = dict(out["verdicts"]); out["autonomy_level"] = goals["autonomy_level"]; out["goals_set"] = not goals.get("_default")
    print(json.dumps(out, ensure_ascii=False)); return out


# ---------------------------------------------------------------- goals-aware prioritizer (orchestrator imports this)
def winning_kinds(site_id):
    r = q("change_log_followups", site_id=f"eq.{site_id}", verdict="eq.improved", select="kind") or []
    c = defaultdict(int)
    for x in r: c[x.get("kind") or ""] += 1
    return dict(c)


def goal_boosts(tasks, goals, wins=None, posts_this_month=0):
    """Pure. Adds `_boost` to each task dict (money page +4, target keyword +2, kind that already won +1) and marks tasks held back by the monthly post cap.
    Returns (tasks_kept, notes). Orchestrator's rank_tasks adds _boost to its impact x ease score. Never raises tasks to 'no approval'."""
    goals = goals or {}; kws = [str(k).lower() for k in (goals.get("target_keywords") or []) if k]; pages = [norm(p) for p in (goals.get("money_pages") or []) if p]
    cap = goals.get("monthly_post_cap"); capped = cap is not None and posts_this_month >= cap; kept, notes = [], []
    for t in tasks:
        txt = str(t.get("task", "")).lower(); b = 0
        if pages and any(p in norm(txt) or (p.split("/", 1)[-1] and p.split("/", 1)[-1] in txt and "/" in p) for p in pages): b += 4
        if kws and any(k in txt for k in kws): b += 2
        if wins and any(k and k.replace("_", " ") in txt for k in wins): b += 1
        if capped and POST_TASK.search(t.get("task", "")): notes.append(f"Held back (monthly post cap {cap} reached): {t['task']}"); continue
        t["_boost"] = b; kept.append(t)
    if kept and goals.get("goal_text") and not goals.get("_default"): notes.append("Goal: " + str(goals["goal_text"])[:120])
    return kept, notes


def apply_goals(tasks, site_id, posts_this_month=None):
    """One-call hook for the orchestrator: reads goals/wins from Supabase, boosts + caps. Fails open (returns tasks unchanged)."""
    try:
        goals = load_goals(site_id)
        if posts_this_month is None:
            d1 = dt.date.today().replace(day=1).isoformat(); r = q("content_posts", site_id=f"eq.{site_id}", created_at=f"gte.{d1}", select="id") or []; posts_this_month = len(r)
        kept, _ = goal_boosts(tasks, goals, winning_kinds(site_id), posts_this_month); return kept
    except Exception as e:
        print("strategist.apply_goals skipped:", str(e)[:80]); return tasks


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args: raise SystemExit("usage: python -m seo_agent.strategist <site_id|all> [--print]")
    from . import run as _r
    _r.each(args[0], lambda sid: run(sid))
