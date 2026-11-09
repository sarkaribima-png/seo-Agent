"""Weekly / monthly client report built ONLY from Supabase data (Search Console tables + our own logs).
Run: python -m seo_agent.report <site_id|all> [--period week|month] [--no-store] [--print] [--llm]

Everything is computed deterministically. The one-line summary uses a fixed template; with --llm (or REPORT_LLM=1)
Claude may rephrase it, but only if it adds no number that is not already in the computed data (else template is used).
GA4 is optional: without a `ga4_daily` table the report says DATA NOT AVAILABLE. Nothing is ever invented.
"""
import sys, re, json, datetime as dt
from collections import defaultdict

LAG = 2                               # Search Console data is ~2 days old (same as track.py)
PERIOD_DAYS = {"week": 7, "month": 28}
CLICK_DROP = 0.20                     # >20% fewer clicks  -> alert
POS_FALL = 3.0                        # ~3+ positions worse -> alert
MIN_SITE_CLICKS = 30                  # below this the previous-period base is too small to call a drop
MIN_PAGE_CLICKS = 10
MIN_PAGE_IMPR = 30
MIN_KW_IMPR = 10
NA = "DATA NOT AVAILABLE"
KIND = {"title_meta": "Title + meta", "schema": "Schema", "internal_link": "Internal link", "content_add": "Naya section"}


# ---------- small helpers ----------
def D(s): return dt.date.fromisoformat(str(s)[:10])
def pct(now, prev):
    if prev in (None, 0) or now is None: return None
    return (now - prev) / prev * 100.0
def f0(x): return "-" if x is None else f"{x:,.0f}"
def f1(x): return "-" if x is None else f"{x:,.1f}"
def fp(x): return "n/a" if x is None else f"{x:+.0f}%"
def short(url): return re.sub(r"^https?://[^/]+", "", url or "") or "/"


def windows(period, today=None):
    n = PERIOD_DAYS[period]; today = today or dt.date.today()
    cur_end = today - dt.timedelta(days=LAG); cur_start = cur_end - dt.timedelta(days=n - 1)
    prev_end = cur_start - dt.timedelta(days=1); prev_start = prev_end - dt.timedelta(days=n - 1)
    return {"n": n, "cur": (cur_start, cur_end), "prev": (prev_start, prev_end)}


def agg(rows, a, b, dk="date", device=None):
    """Sum clicks/impressions and impression-weighted position over days a..b (inclusive)."""
    c = i = pw = 0.0; days = set(); n = 0
    for r in rows:
        d = D(r[dk])
        if d < a or d > b: continue
        if device and str(r.get("device", "")).lower() != device: continue
        imp = float(r.get("impressions") or 0); c += float(r.get("clicks") or 0); i += imp
        if r.get("position") is not None: pw += float(r["position"]) * imp
        days.add(d); n += 1
    return {"clicks": c, "impr": i, "pos": (pw / i if i else None), "ctr": (c / i * 100 if i else None), "days": len(days), "rows": n}


def group(rows, key): 
    g = defaultdict(list)
    for r in rows: g[key(r)].append(r)
    return g


# ---------- changes (our proposals + manual edits seen by track.py) ----------
def change_index(proposals, page_changes):
    idx = defaultdict(list)
    for p in proposals or []:
        if p.get("status") == "applied" and p.get("applied_at"):
            idx[p["page"]].append({"date": D(p["applied_at"]), "what": f"agent: {KIND.get(p.get('kind'), p.get('kind'))} (#{p.get('id')})", "agent": True})
    for c in page_changes or []:
        idx[c["page_url"]].append({"date": D(c["modified_at"]), "what": "page edit (WordPress modified time)", "agent": False})
    return idx


def near_changes(idx, page, a, b):
    return sorted([c for c in idx.get(page, []) if a <= c["date"] <= b], key=lambda c: c["date"])


# ---------- cause checklist ----------
def causes(kind, ctx):
    """Possible-cause checklist. Wording is 'ho sakta hai' on purpose: we only see patterns, not reasons."""
    out = []
    ch = ctx.get("changes") or []
    if ch:
        out.append("Hamara / manual change ho sakta hai: " + "; ".join(f"{c['date']} {c['what']}" for c in ch[:3]) + ". Date match ka matlab pakka karan nahi hai.")
    else:
        out.append("Koi logged change nahi mila (agent ka ya WordPress edit), isliye apna change karan hona kam sambhav lagta hai - par bina log wala manual edit ho sakta hai.")
    share = ctx.get("sitewide_share")
    if kind == "site" or (share is not None and share >= 0.5):
        extra = f" ({ctx['sitewide_n']} me se {round(share * 100)}% pages ke clicks gire)" if share is not None else ""
        out.append("Google algorithm update ho sakta hai, khaaskar jab kayi pages ek saath gire" + extra + ". Update ka calendar yahan DATA NOT AVAILABLE - Google Search Status Dashboard khud dekho.")
    else:
        out.append("Algorithm update ho sakta hai, par sirf ek-do pages girne par ye kam sambhav hai. Update ka calendar yahan DATA NOT AVAILABLE.")
    out.append("Technical issue ho sakta hai (page indexed nahi raha, noindex/robots, server error, redirect). Is report me crawl/index data nahi hai - Search Console URL Inspection se check karo.")
    out.append("Competitor ne behtar page ya snippet laga diya ho sakta hai. Competitor watch / Google top 10 page me dekho.")
    ic = ctx.get("impr_change")
    if ic is not None and ic <= -20:
        out.append(f"Seasonality / demand kam ho sakta hai: impressions bhi {ic:+.0f}% gaye. Pichhle saal ka data DATA NOT AVAILABLE, isliye pakka nahi.")
    elif ic is not None and ic > -10 and kind in ("site", "page"):
        out.append(f"Impressions lagbhag same ({ic:+.0f}%) par clicks gire: CTR gira ho sakta hai (title/snippet, ya SERP me ads/AI answers/featured snippet ka aana).")
    else:
        out.append("Seasonality ho sakta hai (festive/mausam/exam time). Pichhle saal ka data DATA NOT AVAILABLE.")
    return out


# ---------- alerts ----------
def build_alerts(w, gsc, rank, idx, cur_total, prev_total):
    (cs, ce), (ps, pe) = w["cur"], w["prev"]; alerts = []
    # 1. site level
    c, p = cur_total, prev_total
    if p["clicks"] >= MIN_SITE_CLICKS and c["clicks"] < p["clicks"] * (1 - CLICK_DROP):
        d = pct(c["clicks"], p["clicks"]); ic = pct(c["impr"], p["impr"])
        # share of pages (with enough base) that lost >20% clicks
        pages = group(gsc, lambda r: r["page"]); base = lost = 0
        for pg, rs in pages.items():
            a, b = agg(rs, ps, pe), agg(rs, cs, ce)
            if a["clicks"] >= MIN_PAGE_CLICKS: base += 1; lost += b["clicks"] < a["clicks"] * (1 - CLICK_DROP)
        share = lost / base if base else None
        alerts.append({"level": "site", "severity": "high" if d <= -40 else "medium", "title": f"Poori site ke clicks {d:.0f}% gire",
                       "numbers": {"clicks_prev": p["clicks"], "clicks_now": c["clicks"], "impr_change_pct": ic, "pages_checked": base},
                       "sample": f"pichhle period me {f0(p['clicks'])} clicks (base)", 
                       "causes": causes("site", {"changes": [x for pg in idx for x in near_changes(idx, pg, ps, ce)][:3], "sitewide_share": share, "sitewide_n": base, "impr_change": ic})})
    elif p["clicks"] < MIN_SITE_CLICKS and p["clicks"] > 0 and c["clicks"] < p["clicks"] * (1 - CLICK_DROP):
        alerts.append({"level": "note", "severity": "info", "title": "Clicks kam hue, par data bahut chhota hai - alert nahi",
                       "numbers": {"clicks_prev": p["clicks"], "clicks_now": c["clicks"]}, "sample": f"base sirf {f0(p['clicks'])} clicks (< {MIN_SITE_CLICKS})", "causes": []})
    # share for page alerts
    pages = group(gsc, lambda r: r["page"])
    base = lost = 0; per = {}
    for pg, rs in pages.items():
        a, b = agg(rs, ps, pe), agg(rs, cs, ce); per[pg] = (a, b)
        if a["clicks"] >= MIN_PAGE_CLICKS: base += 1; lost += b["clicks"] < a["clicks"] * (1 - CLICK_DROP)
    share = lost / base if base else None
    # 2. page level
    for pg, (a, b) in per.items():
        reasons = []
        if a["clicks"] >= MIN_PAGE_CLICKS and b["clicks"] < a["clicks"] * (1 - CLICK_DROP): reasons.append(f"clicks {pct(b['clicks'], a['clicks']):.0f}%")
        if a["impr"] >= MIN_PAGE_IMPR and b["impr"] >= MIN_PAGE_IMPR and a["pos"] is not None and b["pos"] is not None and b["pos"] - a["pos"] >= POS_FALL:
            reasons.append(f"position {a['pos']:.1f} -> {b['pos']:.1f}")
        if not reasons: continue
        d = pct(b["clicks"], a["clicks"]); worst = (d is not None and d <= -40) or (a["pos"] and b["pos"] and b["pos"] - a["pos"] >= 5)
        alerts.append({"level": "page", "severity": "high" if worst else "medium", "title": f"Page gira: {short(pg)} ({', '.join(reasons)})", "page": pg,
                       "numbers": {"clicks_prev": a["clicks"], "clicks_now": b["clicks"], "pos_prev": a["pos"], "pos_now": b["pos"], "impr_prev": a["impr"], "impr_now": b["impr"]},
                       "sample": f"clicks {f0(a['clicks'])}->{f0(b['clicks'])}, impressions {f0(a['impr'])}->{f0(b['impr'])}",
                       "causes": causes("page", {"changes": near_changes(idx, pg, ps - dt.timedelta(days=w['n']), ce), "sitewide_share": share, "sitewide_n": base, "impr_change": pct(b["impr"], a["impr"])})})
    # 3. keyword level (checked per device like the Rank tracking page, reported once per keyword)
    kwd = defaultdict(list)
    for (kw, dev), rs in sorted(group(rank, lambda r: (r["keyword"], r["device"])).items()):
        a, b = agg(rs, ps, pe, "day"), agg(rs, cs, ce, "day")
        if a["impr"] < MIN_KW_IMPR or b["impr"] < MIN_KW_IMPR or a["pos"] is None or b["pos"] is None: continue
        if b["pos"] - a["pos"] >= POS_FALL: kwd[kw].append((dev, a, b, next((r.get("page_url") for r in rs if r.get("page_url")), None)))
    for kw, L in kwd.items():
        dev, a, b, pg = max(L, key=lambda x: x[2]["pos"] - x[1]["pos"]); devs = "+".join(x[0] for x in L)
        out10 = " (page 1 se bahar)" if any(x[1]["pos"] <= 10 < x[2]["pos"] for x in L) else ""
        alerts.append({"level": "keyword", "severity": "high" if b["pos"] - a["pos"] >= 5 or out10 else "medium", "title": f"Keyword neeche gaya: \"{kw}\" ({devs}) {a['pos']:.1f} -> {b['pos']:.1f}{out10}",
                       "numbers": {"pos_prev": a["pos"], "pos_now": b["pos"], "impr_prev": a["impr"], "impr_now": b["impr"], "device": dev},
                       "sample": f"{dev}: impressions {f0(a['impr'])}->{f0(b['impr'])}, {a['days']}/{b['days']} din ka data",
                       "causes": causes("keyword", {"changes": near_changes(idx, pg, ps - dt.timedelta(days=w['n']), ce) if pg else [], "impr_change": pct(b["impr"], a["impr"])})})
    order = {"high": 0, "medium": 1, "info": 2}
    return sorted(alerts, key=lambda x: order[x["severity"]])


# ---------- winners / losers ----------
def winners_losers(w, gsc, rank):
    (cs, ce), (ps, pe) = w["cur"], w["prev"]; pg = []
    for page, rs in group(gsc, lambda r: r["page"]).items():
        a, b = agg(rs, ps, pe), agg(rs, cs, ce)
        if a["impr"] + b["impr"] < MIN_PAGE_IMPR: continue
        pg.append({"page": page, "clicks_prev": a["clicks"], "clicks_now": b["clicks"], "delta": b["clicks"] - a["clicks"], "pct": pct(b["clicks"], a["clicks"]),
                   "pos_prev": a["pos"], "pos_now": b["pos"], "impr_prev": a["impr"], "impr_now": b["impr"]})
    kw = []; skipped = 0
    for k, rs in group(rank, lambda r: r["keyword"]).items():
        a, b = agg(rs, ps, pe, "day"), agg(rs, cs, ce, "day")
        if a["impr"] < MIN_KW_IMPR or b["impr"] < MIN_KW_IMPR or a["pos"] is None or b["pos"] is None: skipped += 1; continue
        kw.append({"keyword": k, "pos_prev": a["pos"], "pos_now": b["pos"], "delta": a["pos"] - b["pos"], "clicks_prev": a["clicks"], "clicks_now": b["clicks"], "impr_now": b["impr"]})
    top = lambda L, key, rev, ok: sorted([x for x in L if ok(x)], key=key, reverse=rev)[:5]
    return {"pages_win": top(pg, lambda x: x["delta"], True, lambda x: x["delta"] > 0), "pages_lose": top(pg, lambda x: x["delta"], False, lambda x: x["delta"] < 0),
            "kw_win": top(kw, lambda x: x["delta"], True, lambda x: x["delta"] >= 0.5), "kw_lose": top(kw, lambda x: x["delta"], False, lambda x: x["delta"] <= -0.5),
            "pages_considered": len(pg), "kw_considered": len(kw), "kw_skipped_low_data": skipped}


# ---------- impact of changes ----------
def impact(w, gsc, rank, proposals, page_changes, tracked):
    """For each applied proposal in the previous or current period: same-length window before vs after.
    The after window starts 2 days after applied_at (Search Console lag) and stops at the report end."""
    (cs, ce), (ps, pe) = w["cur"], w["prev"]; N = w["n"]; out = []
    applied = [p for p in proposals or [] if p.get("status") == "applied" and p.get("applied_at") and ps <= D(p["applied_at"]) <= ce]
    kws = defaultdict(list)
    for t in tracked or []: kws[t.get("page_url")].append(t["keyword"])
    for p in sorted(applied, key=lambda p: p["applied_at"]):
        d0 = D(p["applied_at"]); page = p["page"]
        b0, b1 = d0 - dt.timedelta(days=N), d0 - dt.timedelta(days=1)
        a0, a1 = d0 + dt.timedelta(days=2), min(ce, d0 + dt.timedelta(days=N + 1))
        rs = [r for r in gsc if r["page"] == page]
        a_days = max((a1 - a0).days + 1, 0)
        B = agg(rs, b0, b1); A = agg(rs, a0, a1) if a_days else agg([], a0, a0)
        bd = max(B["days"], 1); ad = max(A["days"], 1)
        cpd_b, cpd_a = B["clicks"] / N, (A["clicks"] / a_days if a_days else None)
        others = [c for c in near_changes(change_index(proposals, page_changes), page, d0 - dt.timedelta(days=N), d0 + dt.timedelta(days=N)) if not (c["agent"] and f"(#{p.get('id')})" in c["what"])]
        kwl = kws.get(page, []); kp = []
        for k in kwl:
            kr = [r for r in rank if r["keyword"] == k]
            kb, ka = agg(kr, b0, b1, "day"), (agg(kr, a0, a1, "day") if a_days else None)
            if ka and kb["pos"] is not None and ka["pos"] is not None: kp.append({"keyword": k, "pos_before": kb["pos"], "pos_after": ka["pos"], "impr_before": kb["impr"], "impr_after": ka["impr"]})
        enough = a_days >= 5 and A["days"] >= 4 and B["days"] >= 4 and (B["impr"] + A["impr"]) >= 50
        verdict, why = "data kam", f"pehle ya baad ka window chhota hai (baad: {a_days} din), ya impressions kam hain"
        if enough:
            dpos = (B["pos"] - A["pos"]) if B["pos"] is not None and A["pos"] is not None else None
            dclk = pct(cpd_a, cpd_b) if cpd_b else None
            if (dpos is not None and dpos >= 1 and (pct(A["impr"] / a_days, B["impr"] / N) or 0) > -20) or (dclk is not None and dclk >= 30 and B["clicks"] >= 10):
                verdict, why = "behtar dikha", "position behtar ya clicks/din zyada"
            elif (dpos is not None and dpos <= -1.5) or (dclk is not None and dclk <= -30 and B["clicks"] >= 10):
                verdict, why = "kharab dikha", "position kharab ya clicks/din kam"
            else: verdict, why = "koi saaf badlav nahi", "badlav chhota hai"
        out.append({"proposal_id": p.get("id"), "kind": KIND.get(p.get("kind"), p.get("kind")), "page": page, "applied_on": str(d0), "verdict": verdict, "why": why,
                    "before": {"window": f"{b0}..{b1}", "clicks": B["clicks"], "impr": B["impr"], "pos": B["pos"], "days_with_data": B["days"], "clicks_per_day": cpd_b},
                    "after": {"window": f"{a0}..{a1}" if a_days else "abhi shuru nahi hua", "clicks": A["clicks"], "impr": A["impr"], "pos": A["pos"], "days_with_data": A["days"], "clicks_per_day": cpd_a, "calendar_days": a_days},
                    "tracked_keywords": kp, "mixed_with": [f"{c['date']} {c['what']}" for c in others],
                    "caveat": "Ye sirf pehle-baad ka tulna hai. Position/clicks Google khud bhi hilata hai, isliye 'ho sakta hai' is change ka asar - pakka nahi (correlation, causation nahi)."})
    return out


# ---------- build ----------
def build_report(site, period, data, today=None):
    """data: dict of lists (gsc_daily, rank_daily, tracked_keywords, proposals, page_changes, ga4 or None, pending_proposals int)."""
    w = windows(period, today); (cs, ce), (ps, pe) = w["cur"], w["prev"]; N = w["n"]
    gsc = data.get("gsc_daily") or []; rank = data.get("rank_daily") or []
    ga4 = data.get("ga4")
    idx = change_index(data.get("proposals"), data.get("page_changes"))
    cur, prev = agg(gsc, cs, ce), agg(gsc, ps, pe)
    rows = [("Clicks", prev["clicks"], cur["clicks"], pct(cur["clicks"], prev["clicks"]), "Search Console"),
            ("Impressions", prev["impr"], cur["impr"], pct(cur["impr"], prev["impr"]), "Search Console"),
            ("CTR %", prev["ctr"], cur["ctr"], pct(cur["ctr"], prev["ctr"]), "clicks / impressions")]
    for lab, dev in (("Avg position (sab devices)", None), ("Avg position desktop", "desktop"), ("Avg position mobile", "mobile")):
        a, b = agg(gsc, ps, pe, device=dev), agg(gsc, cs, ce, device=dev)
        rows.append((lab, a["pos"], b["pos"], pct(b["pos"], a["pos"]), "kam = behtar"))
    top10 = {}
    for tag, (a0, a1) in (("prev", (ps, pe)), ("cur", (cs, ce))):
        n = t = 0
        for k, rs in group(rank, lambda r: r["keyword"]).items():
            x = agg(rs, a0, a1, "day")
            if x["pos"] is not None and x["impr"] >= MIN_KW_IMPR: n += 1; t += x["pos"] <= 10
        top10[tag] = (t, n)
    rows.append(("Tracked keywords top 10 me", top10["prev"][0], top10["cur"][0], pct(top10["cur"][0], top10["prev"][0]),
                 f"{top10['prev'][1]} / {top10['cur'][1]} keywords me data (>= {MIN_KW_IMPR} impressions)"))
    if ga4:
        for lab, k in (("GA4 sessions", "sessions"), ("GA4 conversions", "conversions")):
            a = sum(float(r.get(k) or 0) for r in ga4 if ps <= D(r["date"]) <= pe); b = sum(float(r.get(k) or 0) for r in ga4 if cs <= D(r["date"]) <= ce)
            rows.append((lab, a, b, pct(b, a), "GA4"))
    else:
        rows += [("GA4 sessions", NA, NA, None, "GA4 connect nahi hai"), ("GA4 conversions", NA, NA, None, "GA4 connect nahi hai")]
    table = [{"metric": m, "last": a, "now": b, "change_pct": c, "note": n} for m, a, b, c, n in rows]
    alerts = build_alerts(w, gsc, rank, idx, cur, prev)
    wl = winners_losers(w, gsc, rank)
    imp = impact(w, gsc, rank, data.get("proposals"), data.get("page_changes"), data.get("tracked_keywords"))
    # opportunities (proposal candidates only)
    opp = []
    for page, rs in group(gsc, lambda r: r["page"]).items():
        x = agg(rs, cs, ce)
        if x["impr"] >= 100 and x["pos"] is not None and 4 <= x["pos"] <= 15: opp.append({"page": page, "impr": x["impr"], "pos": x["pos"], "ctr": x["ctr"]})
    opp = sorted(opp, key=lambda o: -o["impr"])[:3]
    cov = (cur["days"] + prev["days"]) / (2 * N)
    total = cur["clicks"] + prev["clicks"]
    conf = "High" if total >= 500 and cov >= 0.8 else "Medium" if total >= 100 and cov >= 0.5 else "Low"
    sample = {"clicks_last": prev["clicks"], "clicks_now": cur["clicks"], "impressions_now": cur["impr"], "gsc_days_with_data": f"{cur['days']}/{N} (now), {prev['days']}/{N} (last)",
              "gsc_rows": cur["rows"] + prev["rows"], "tracked_keywords_with_data": top10["cur"][1], "pages_with_data": wl["pages_considered"], "changes_measured": len(imp)}
    actions = build_actions(alerts, imp, opp, sample, conf, data, ga4)
    rep = {"site_id": site["id"], "domain": site.get("domain", site["id"]), "period": period,
           "period_start": str(cs), "period_end": str(ce), "compared_with": f"{ps}..{pe}", "generated_on": str(today or dt.date.today()),
           "table": table, "winners_losers": wl, "alerts": alerts, "impact": imp, "actions": actions, "opportunities": opp, "sample": sample,
           "confidence": conf, "ga4_connected": bool(ga4)}
    rep["summary"] = template_summary(rep)
    return rep


def build_actions(alerts, imp, opp, sample, conf, data, ga4):
    a = []
    hi = [x for x in alerts if x["severity"] in ("high", "medium") and x["level"] != "note"]
    for x in hi[:3]: a.append(f"Alert dekho: {x['title']}. Uska possible-cause checklist (upar) check karo. Koi bhi fix 'proposed change' hoga, approval ke baad hi live.")
    if len(hi) > 3: a.append(f"Baaki {len(hi) - 3} alerts bhi dekho.")
    early = [i for i in imp if i["verdict"] == "data kam"]
    if early: a.append(f"{len(early)} change ka asar abhi judge mat karo (data kam). 1-2 hafte baad dobara report dekho.")
    bad = [i for i in imp if i["verdict"] == "kharab dikha"]
    for i in bad: a.append(f"Change #{i['proposal_id']} ({short(i['page'])}) ke baad numbers kharab dikhe. Ho sakta hai ye us change ki wajah se na ho; phir bhi review karo, zaroorat ho to dashboard se rollback request (approval ke saath).")
    for o in opp[:2]: a.append(f"{short(o['page'])}: position {o['pos']:.1f}, impressions {f0(o['impr'])} - title/meta ka proposal banane layak (sirf proposal, approval ke baad).")
    pend = data.get("pending_proposals")
    if pend: a.append(f"{pend} proposals approval ke intezaar me hain (Proposals page).")
    if not data.get("tracked_keywords"): a.append("Tracked keywords nahi hain: clients ke important keywords Rank tracking me add karo.")
    if conf == "Low": a.append("Data kam hai (confidence Low): bade nishkarsh mat nikalo, 2-4 hafte aur data jama hone do.")
    if not ga4: a.append("GA4 connect nahi hai: sessions/conversions abhi DATA NOT AVAILABLE hain. Connect karne par report me apne aap aa jayenge.")
    return a[:8] or ["Koi khaas action nahi. Routine tracking jaari rakho."]


def template_summary(r):
    t = {x["metric"]: x for x in r["table"]}; c = t["Clicks"]; n = {"week": "pichhle 7 din", "month": "pichhle 28 din"}[r["period"]]
    if c["last"] in (None, 0) and c["now"] in (None, 0): return f"{n} me Search Console data nahi mila (DATA NOT AVAILABLE) - kuch bhi kehna mushkil hai."
    s = f"{n} me clicks {f0(c['now'])} rahe (pehle {f0(c['last'])}, {fp(c['change_pct'])})"
    p = t["Avg position (sab devices)"]
    if p["now"] is not None: s += f", average position {f1(p['now'])}"
    hi = len([a for a in r["alerts"] if a["severity"] in ("high", "medium") and a["level"] != "note"])
    s += f". {hi} alert hain." if hi else ". Koi bada alert nahi."
    if r["confidence"] == "Low": s += " Data kam hai, isliye ise sirf ek jhalak samjho."
    return s


def llm_summary(rep):
    """Optional: Claude rephrases the template. Rejected if it contains any number not already in the report's own numbers."""
    import os
    try:
        import anthropic
        base = rep["summary"]
        ok = set(re.findall(r"\d+(?:[.,]\d+)?", json.dumps({"s": base, "t": rep["table"], "n": rep["sample"]}, default=str)))
        r = anthropic.Anthropic().messages.create(model=os.environ.get("SEO_MODEL", "claude-haiku-5-5"), max_tokens=300,
            system="Rewrite this SEO summary as ONE simple Hinglish sentence-pair for a non-technical clinic owner. Do not add any new number, cause or claim. Keep 'ho sakta hai' wording. Reply with the text only.",
            messages=[{"role": "user", "content": base}])
        t = "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()
        new = set(re.findall(r"\d+(?:[.,]\d+)?", t))
        return t if t and new <= ok and len(t) < 400 else base
    except Exception as e:
        print("llm summary skipped:", str(e)[:80]); return rep["summary"]


# ---------- markdown ----------
def _cell(x, dec=0):
    if isinstance(x, str): return x
    return "-" if x is None else (f"{x:,.1f}" if dec else f"{x:,.0f}")


def to_markdown(r):
    L = [f"# SEO report - {r['domain']} ({'Weekly' if r['period'] == 'week' else 'Monthly'})",
         f"Period: {r['period_start']} se {r['period_end']} | Compare: {r['compared_with']} | Source: Search Console (Supabase)", "",
         "## 1. Ek line me haal", r["summary"], "", "## 2. Key numbers", "| Metric | Last | Now | Change % | Note |", "|---|---:|---:|---:|---|"]
    for x in r["table"]:
        dec = 1 if ("position" in x["metric"] or "CTR" in x["metric"]) else 0
        L.append(f"| {x['metric']} | {_cell(x['last'], dec)} | {_cell(x['now'], dec)} | {fp(x['change_pct']) if x['change_pct'] is not None else '-'} | {x['note']} |")
    wl = r["winners_losers"]
    L += ["", "## 3. Winners / Losers", f"_Sample: {wl['pages_considered']} pages, {wl['kw_considered']} keywords tulna me aaye ({wl['kw_skipped_low_data']} keywords data kam hone se chhode)._", "", "**Winner pages (clicks badhe)**"]
    L += ([f"- {short(x['page'])}: clicks {f0(x['clicks_prev'])} -> {f0(x['clicks_now'])} ({fp(x['pct'])}), position {f1(x['pos_prev'])} -> {f1(x['pos_now'])}" for x in wl["pages_win"]] or ["- Koi nahi (ya data kam)"])
    L += ["", "**Loser pages (clicks ghate)**"]
    L += ([f"- {short(x['page'])}: clicks {f0(x['clicks_prev'])} -> {f0(x['clicks_now'])} ({fp(x['pct'])}), position {f1(x['pos_prev'])} -> {f1(x['pos_now'])}" for x in wl["pages_lose"]] or ["- Koi nahi (ya data kam)"])
    L += ["", "**Winner keywords (position behtar)**"]
    L += ([f"- {x['keyword']}: {f1(x['pos_prev'])} -> {f1(x['pos_now'])} ({x['delta']:+.1f} jagah)" for x in wl["kw_win"]] or ["- Koi nahi (ya data kam)"])
    L += ["", "**Loser keywords (position kharab)**"]
    L += ([f"- {x['keyword']}: {f1(x['pos_prev'])} -> {f1(x['pos_now'])} ({x['delta']:+.1f} jagah)" for x in wl["kw_lose"]] or ["- Koi nahi (ya data kam)"])
    L += ["", "## 4. Alerts", f"_Rule: clicks >{int(CLICK_DROP * 100)}% gire ya position ~{POS_FALL:.0f}+ jagah gira. Chhota data (base clicks < {MIN_SITE_CLICKS} site, < {MIN_PAGE_CLICKS} page) par alert nahi lagta._", ""]
    if not r["alerts"]: L.append("Koi alert nahi.")
    for i, a in enumerate(r["alerts"], 1):
        L.append(f"**{i}. [{a['severity'].upper()}] {a['title']}**  \nSample: {a['sample']}")
        if a["causes"]: L += ["Possible causes (checklist, pakka nahi):"] + [f"- [ ] {c}" for c in a["causes"]]
        L.append("")
    L += ["### Pichhle changes aur kya hila", "_Har change ka before/after same-length window me. Ye correlation hai, causation nahi._", ""]
    if not r["impact"]: L.append("Is period me koi applied change nahi mila (ya DATA NOT AVAILABLE).")
    for i in r["impact"]:
        b, a = i["before"], i["after"]
        L.append(f"- **#{i['proposal_id']} {i['kind']}** | {short(i['page'])} | applied {i['applied_on']} | **{i['verdict']}** ({i['why']})  \n  Pehle ({b['window']}): clicks {f0(b['clicks'])}, impressions {f0(b['impr'])}, position {f1(b['pos'])} ({b['days_with_data']} din ka data)  \n  Baad ({a['window']}): clicks {f0(a['clicks'])}, impressions {f0(a['impr'])}, position {f1(a['pos'])} ({a['days_with_data']} din ka data)")
        if i["tracked_keywords"]: L.append("  Tracked keywords: " + "; ".join(f"{k['keyword']} {f1(k['pos_before'])} -> {f1(k['pos_after'])}" for k in i["tracked_keywords"]))
        if i["mixed_with"]: L.append("  Mixed result: usi page par aur change bhi: " + "; ".join(i["mixed_with"]))
        L.append("  " + i["caveat"] + "  ")
    L += ["", "## 5. Agle hafte kya karna hai" if r["period"] == "week" else "## 5. Agle mahine kya karna hai"] + [f"{n}. {x}" for n, x in enumerate(r["actions"], 1)]
    s = r["sample"]
    L += ["", "---", f"**Sample size:** clicks last {f0(s['clicks_last'])} / now {f0(s['clicks_now'])}; impressions now {f0(s['impressions_now'])}; GSC data days {s['gsc_days_with_data']}; {s['pages_with_data']} pages; {s['tracked_keywords_with_data']} tracked keywords; {s['changes_measured']} changes measured.",
          f"**Confidence:** {r['confidence']}",
          f"**Evidence:** Supabase tables gsc_daily (page/device/date), rank_daily, tracked_keywords, proposals (status=applied, applied_at), page_changes{', ga4_daily' if r['ga4_connected'] else ''}; periods {r['period_start']}..{r['period_end']} vs {r['compared_with']}. GA4: {'connected' if r['ga4_connected'] else NA}. Algorithm-update calendar, crawl/index status, last-year seasonality: {NA}."]
    return "\n".join(L) + "\n"


# ---------- IO ----------
def fetch(table, page=1000, cap=200, **params):
    from . import sb
    out = []
    for i in range(cap):
        rows = sb.select(table, **params, limit=str(page), offset=str(i * page))
        out += rows
        if len(rows) < page: break
    return out


def load(site_id, period, today=None):
    w = windows(period, today); since = w["prev"][0] - dt.timedelta(days=w["n"])     # extra window for 'before' of changes
    until = w["cur"][1]
    rng = lambda col: {"and": f"({col}.gte.{since},{col}.lte.{until})"}
    d = {"gsc_daily": fetch("gsc_daily", site_id=f"eq.{site_id}", select="date,page,device,clicks,impressions,position", order="date", **rng("date")),
         "rank_daily": fetch("rank_daily", site_id=f"eq.{site_id}", select="keyword,day,device,position,impressions,clicks", order="day", **rng("day")),
         "tracked_keywords": fetch("tracked_keywords", site_id=f"eq.{site_id}", active="eq.true", select="keyword,page_url"),
         "proposals": fetch("proposals", site_id=f"eq.{site_id}", status="eq.applied", select="id,page,kind,applied_at,status", applied_at=f"gte.{since}"),
         "page_changes": fetch("page_changes", site_id=f"eq.{site_id}", select="page_url,modified_at", modified_at=f"gte.{since}")}
    d["pending_proposals"] = len(fetch("proposals", site_id=f"eq.{site_id}", status="eq.pending", select="id"))
    try: d["ga4"] = fetch("ga4_daily", site_id=f"eq.{site_id}", select="date,sessions,conversions", **rng("date")) or None   # optional table
    except Exception: d["ga4"] = None
    # attach the keyword's page so keyword alerts can look up changes on that page
    pg = {t["keyword"]: t.get("page_url") for t in d["tracked_keywords"]}
    for r in d["rank_daily"]: r["page_url"] = pg.get(r["keyword"])
    return d


def store(rep, md, today=None):
    from . import sb
    sb.upsert("reports", [{"site_id": rep["site_id"], "period": rep["period"], "period_start": rep["period_start"], "period_end": rep["period_end"],
                           "json": rep, "markdown": md}], "site_id,period,period_start")


def run(site_id, period="week", do_store=True, show=False, llm=False):
    from . import run as runmod
    site = runmod.sites().get(site_id) or {"id": site_id}
    rep = build_report(site, period, load(site_id, period))
    if llm: rep["summary"] = llm_summary(rep)
    md = to_markdown(rep)
    if do_store: store(rep, md); print("report saved:", site_id, period, rep["period_start"])
    if show or not do_store: print(md)
    return rep


if __name__ == "__main__":
    a = sys.argv[1:]; period = a[a.index("--period") + 1] if "--period" in a else "week"
    if period not in PERIOD_DAYS or not a or a[0].startswith("--"): raise SystemExit("usage: python -m seo_agent.report <site_id|all> [--period week|month] [--no-store] [--print] [--llm]")
    import os
    from . import run as _r
    _r.each(a[0], lambda sid: run(sid, period, "--no-store" not in a, "--print" in a, "--llm" in a or os.environ.get("REPORT_LLM") == "1"))
