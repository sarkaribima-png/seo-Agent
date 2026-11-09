"""Keyword & SERP researcher: striking-distance queries, cannibalization, clusters + intent, content gaps -> keyword_opps.
Approved opportunities for NEW pages are fed into content_ideas (no duplicates).
Run: python -m seo_agent.keywords <site_id|all>

Everything numeric comes from Search Console (or imported Ahrefs rows). Difficulty without a KD source is a relative
estimate and is labelled so. The LLM is optional (KEYWORDS_LLM=1 + ANTHROPIC_API_KEY); rules are the fallback."""
import os, re, sys, json, datetime as dt
from collections import defaultdict
from urllib.parse import urlparse
from . import sb

LAG = 2
MIN_IMPR = int(os.environ.get("KW_MIN_IMPRESSIONS", "50"))
POS_LO, POS_HI = 8.0, 20.0
# ESTIMATE, not measured: the CTR a page might reach if the query moved to about position 5.
TARGET_CTR = float(os.environ.get("KW_TARGET_CTR_ESTIMATE", "0.07"))
MAX_OPPS = int(os.environ.get("KW_MAX_OPPS", "150"))
INTENTS = ("Informational", "Commercial", "Transactional", "Navigational")
MEDICAL = re.compile(r"health|medical|surg|clinic|doctor|dental|derma|hospital", re.I)


# ---------------- Search Console ----------------
def fetch_rows(site, days=28, device=None):
    """Rows [{query,page,clicks,impressions,position}] from GSC (query+page dimensions, paged)."""
    from . import gsc
    s = gsc.service(); end = dt.date.today() - dt.timedelta(days=LAG); start = end - dt.timedelta(days=days)
    body = {"startDate": str(start), "endDate": str(end), "dimensions": ["query", "page"], "rowLimit": 25000, "startRow": 0}
    if device: body["dimensionFilterGroups"] = [{"filters": [{"dimension": "device", "operator": "equals", "expression": device.upper()}]}]
    out = []
    for _ in range(8):   # at most 200k rows
        rows = s.searchanalytics().query(siteUrl=site["gsc_property"], body=body).execute().get("rows", [])
        out += [{"query": r["keys"][0].lower().strip(), "page": norm_url(r["keys"][1]), "clicks": r["clicks"],
                 "impressions": r["impressions"], "position": r["position"]} for r in rows]
        if len(rows) < 25000: break
        body["startRow"] += 25000
    return out


def norm_url(u):
    u = (u or "").split("#")[0].split("?")[0]
    return u.rstrip("/").lower() if urlparse(u).path.strip("/") else u.rstrip("/").lower()


def by_query(rows):
    """Collapse query+page rows to one row per query (impression-weighted position, best page = most impressions)."""
    g = defaultdict(list)
    for r in rows: g[r["query"]].append(r)
    out = {}
    for q, rs in g.items():
        impr = sum(r["impressions"] for r in rs); clicks = sum(r["clicks"] for r in rs)
        if not impr: continue
        pos = sum(r["position"] * r["impressions"] for r in rs) / impr
        top = max(rs, key=lambda r: (r["impressions"], r["clicks"]))
        out[q] = {"query": q, "impressions": int(impr), "clicks": int(clicks), "ctr": clicks / impr, "position": pos, "page": top["page"], "pages": rs}
    return out


def striking_distance(site, rows=None, days=28, min_impr=MIN_IMPR, device=None):
    """Queries at position 8-20 with enough impressions, ranked by impressions x CTR gap (gap is vs an ESTIMATED target CTR)."""
    rows = rows if rows is not None else fetch_rows(site, days, device)
    out = []
    for q in by_query(rows).values():
        if not (POS_LO <= q["position"] <= POS_HI) or q["impressions"] < min_impr: continue
        gap = max(0.0, TARGET_CTR - q["ctr"])
        out.append({**{k: q[k] for k in ("query", "impressions", "clicks", "ctr", "position", "page")}, "ctr_gap": gap, "potential_clicks": q["impressions"] * gap})
    return sorted(out, key=lambda x: -x["potential_clicks"])


def cannibalization(rows, min_impr=20, min_share=0.15):
    """Same query shown for 2+ of our pages. A page counts only if it has >= min_impr impressions and >= min_share of the query's impressions."""
    g = defaultdict(list)
    for r in rows: g[r["query"]].append(r)
    out = []
    for q, rs in g.items():
        tot = sum(r["impressions"] for r in rs)
        pages = [r for r in rs if r["impressions"] >= min_impr and r["impressions"] >= min_share * tot]
        if len(pages) < 2: continue
        pages.sort(key=lambda r: (-r["clicks"], r["position"]))   # primary = most clicks, then best position
        out.append({"query": q, "impressions": int(tot), "primary": pages[0]["page"], "position": pages[0]["position"],
                    "clicks": int(sum(r["clicks"] for r in rs)),
                    "pages": [{"page": r["page"], "impressions": int(r["impressions"]), "clicks": int(r["clicks"]), "position": round(r["position"], 1)} for r in pages]})
    return sorted(out, key=lambda x: -x["impressions"])


def tracked_mismatch(rows, tracked):
    """A tracked keyword whose Google-ranking page is not the page we want to rank (one keyword -> one page)."""
    bq = by_query(rows); out = []
    for k in tracked:
        want = norm_url(k.get("page_url")); q = bq.get((k.get("keyword") or "").lower())
        if not want or not q: continue
        share = {}
        for r in q["pages"]: share[r["page"]] = share.get(r["page"], 0) + r["impressions"]
        top = max(share, key=share.get)
        if top != want:
            out.append({"query": q["query"], "wanted": want, "ranking": top, "impressions": q["impressions"], "position": q["position"], "clicks": q["clicks"],
                        "wanted_share": share.get(want, 0) / q["impressions"]})
    return out


# ---------------- clusters + intent (rules) ----------------
STOP = set("a an the of in for to and or is are can do does how what why when which with without on at by from my me near your best top vs versus".split())
SUBTOPICS = [("cost", r"cost|price|charges|fees|affordable|cheap|emi|insurance"), ("recovery", r"recover|after surgery|post.?op|healing|scar|swelling|downtime|days"),
             ("symptoms-causes", r"symptom|cause|reason|sign|why|diagnos|test"), ("non-surgical", r"without surgery|non.?surgical|exercise|medicine|cream|diet|natural|home"),
             ("doctor-clinic", r"doctor|surgeon|clinic|hospital|specialist|centre|center|dr\b"), ("procedure", r"surgery|procedure|treatment|operation|removal|reduction")]
INFO = re.compile(r"^(what|why|how|when|can|does|do|is|are|who|which)\b|symptom|cause|recover|diet|exercise|meaning|vs\b|difference|types of|risk|side effect|without surgery|at home|before and after")
TRANS = re.compile(r"\b(book|booking|appointment|consultation|contact|phone|number|call|whatsapp|near me|open now|buy|order)\b")
COMM = re.compile(r"\b(cost|price|charges|fees|best|top|review|reviews|compare|comparison|affordable|cheap|surgeon|doctor|clinic|hospital|specialist|centre|center|package|emi|insurance)\b|\bsurgery in\b|\btreatment in\b")


def tokens(s): return [t for t in re.findall(r"[a-z0-9]+", s.lower()) if t not in STOP]
def tset(s): return frozenset(tokens(s))
def similar(a, b, th=0.8):
    a, b = tset(a), tset(b)
    return bool(a and b) and len(a & b) / len(a | b) >= th


def brand_terms(site):
    stem = re.sub(r"[^a-z0-9]", "", (site.get("domain") or site.get("id") or "").split(".")[0].lower())
    return {t.lower() for t in (site.get("brand_terms") or []) if t} | ({stem} if len(stem) > 3 else set())


def tag_intent(q, brands=()):
    ql = q.lower()
    if any(b and b in ql.replace(" ", "") for b in brands): return "Navigational"
    if re.search(r"\.(com|in|org)\b|\blogin\b", ql): return "Navigational"
    if TRANS.search(ql): return "Transactional"
    if INFO.search(ql): return "Informational"
    if COMM.search(ql): return "Commercial"
    return "Informational"


def subtopic(q):
    for name, rx in SUBTOPICS:
        if re.search(rx, q.lower()): return name
    return "general"


def cluster_keywords(queries):
    """topic = the most shared 2-word (else 1-word) phrase across queries; sub-topic = rule class. Returns {query: 'topic > sub-topic'}."""
    grams = defaultdict(set)
    for q in queries:
        t = tokens(q)
        for n in (2, 1):
            for i in range(len(t) - n + 1): grams[" ".join(t[i:i + n])].add(q)
    ranked = sorted(grams, key=lambda g: (-len(grams[g]), -len(g.split()), g))
    out = {}
    for q in queries:
        best = next((g for g in ranked if q in grams[g] and len(grams[g]) >= 2 and g not in SUBTOPIC_WORDS), None)
        if not best: t = tokens(q); best = " ".join(t[:2]) or q
        out[q] = f"{best} > {subtopic(q)}"
    return out


SUBTOPIC_WORDS = {"cost", "price", "surgery", "treatment", "doctor", "clinic", "recovery", "charges", "procedure", "hospital", "delhi", "gurgaon", "india", "in delhi", "in gurgaon"}


def llm_label(site, queries):
    """Optional: let Claude name clusters and intent. Returns {query: {cluster,intent}}; {} on any problem (rules stay in charge)."""
    if os.environ.get("KEYWORDS_LLM") != "1" or not os.environ.get("ANTHROPIC_API_KEY") or not queries: return {}
    try:
        import anthropic
        sysm = ("Group search queries into topic > sub-topic clusters and tag intent (Informational, Commercial, Transactional or Navigational). "
                "Use only the queries given. Never add competitor names. Reply JSON only: {\"<query>\": {\"cluster\": \"topic > subtopic\", \"intent\": \"...\"}}")
        r = anthropic.Anthropic().messages.create(model=os.environ.get("SEO_MODEL", "claude-haiku-5-5"), max_tokens=8000, system=sysm,
                                                  messages=[{"role": "user", "content": json.dumps({"niche": site.get("niche"), "queries": queries[:120]})}])
        t = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        d = json.loads(t[t.index("{"): t.rindex("}") + 1])
        return {q: v for q, v in d.items() if q in queries and isinstance(v, dict) and v.get("intent") in INTENTS and v.get("cluster")}
    except Exception as e:
        print("LLM labelling skipped:", str(e)[:120]); return {}


# ---------------- difficulty / priority ----------------
def difficulty(pos, kd=None):
    """(label, source). KD from an imported Ahrefs row if we have one, otherwise a rough estimate from how far we already are."""
    if kd is not None: return ("Low" if kd <= 30 else "Med" if kd <= 60 else "High", "ahrefs_kd")
    if pos is None: return ("Med", "estimate")
    return ("Low" if pos <= 12 else "Med" if pos <= 16 else "High", "estimate")


def prioritise(rows):
    """High / Med / Low by rank of score (top third / middle / rest). Cannibalization and mismatches are never below Med."""
    rows.sort(key=lambda r: -(r["score"] or 0)); n = len(rows)
    for i, r in enumerate(rows):
        r["priority"] = "High" if i < max(1, n // 3) else "Med" if i < max(2, 2 * n // 3) else "Low"
        if r["source"] in ("cannibalization", "tracked_page_mismatch") and r["priority"] == "Low": r["priority"] = "Med"
    return rows


def slugify(q): return "/" + "-".join(tokens(q) or [q.replace(" ", "-")])[:60] + "/"


# ---------------- gaps from imported competitor keywords ----------------
def competitor_gaps(site_id, own_queries, banned=(), names=()):
    """Non-branded keywords a competitor ranks top-20 for (Ahrefs import) that none of our GSC queries cover. No competitor name is kept."""
    try: rows = sb.select("competitor_keywords", site_id=f"eq.{site_id}", branded="eq.false", order="volume.desc", limit="500")
    except Exception as e: print("competitor_keywords not readable:", str(e)[:100]); return []
    out, seen = [], []
    for r in rows:
        k = (r.get("keyword") or "").strip().lower()
        if not k or (r.get("position") or 99) > 20: continue
        if any(b.lower() in k for b in banned if b) or any(n and n.lower() in k for n in names): continue
        if any(similar(k, q) for q in own_queries) or any(similar(k, s) for s in seen): continue
        seen.append(k); out.append({"query": k, "volume": r.get("volume"), "kd": r.get("kd")})
    return out


# ---------------- build + write ----------------
def build(site, rows, tracked=(), gaps=(), device="all", llm=None):
    brands = brand_terms(site); opps = []
    sd, can, mm = striking_distance(site, rows), cannibalization(rows), tracked_mismatch(rows, tracked)
    queries = list({x["query"] for x in sd} | {x["query"] for x in can} | {x["query"] for x in mm} | {g["query"] for g in gaps})
    cl = cluster_keywords(queries); llm = llm if llm is not None else llm_label(site, queries)

    def meta(q): v = llm.get(q) or {}; return v.get("cluster") or cl[q], v.get("intent") or tag_intent(q, brands)
    def base(q, src, **kw):
        c, i = meta(q); return {"site_id": site["id"], "keyword": q, "cluster": c, "intent": i, "device": device, "source": src, **kw}
    for x in sd:
        d, ds = difficulty(x["position"])
        opps.append(base(x["query"], "striking_distance", position=round(x["position"], 1), impressions=x["impressions"], clicks=x["clicks"], ctr=round(x["ctr"], 4),
                         difficulty_estimate=d, difficulty_source=ds, recommended_page=x["page"], page_exists=True, score=round(x["potential_clicks"], 1),
                         notes=f"CTR gap vs estimated target {TARGET_CTR:.0%}: about {x['potential_clicks']:.0f} extra clicks per 28 days IF it reached about position 5 (estimate)."))
    for x in can:
        d, ds = difficulty(x["position"])
        opps.append(base(x["query"], "cannibalization", position=round(x["position"], 1), impressions=x["impressions"], clicks=x["clicks"],
                         ctr=round(x["clicks"] / x["impressions"], 4), difficulty_estimate=d, difficulty_source=ds, recommended_page=x["primary"], page_exists=True,
                         score=float(x["impressions"]), notes="Competing own pages: " + "; ".join(f"{p['page']} (pos {p['position']}, {p['impressions']} impr)" for p in x["pages"])
                         + ". Keep one page for this query; merge, redirect or differentiate the others (needs human approval)."))
    for x in mm:
        d, ds = difficulty(x["position"])
        opps.append(base(x["query"], "tracked_page_mismatch", position=round(x["position"], 1), impressions=x["impressions"], clicks=x["clicks"],
                         ctr=round(x["clicks"] / x["impressions"], 4), difficulty_estimate=d, difficulty_source=ds, recommended_page=x["wanted"], page_exists=True,
                         score=float(x["impressions"]), notes=f"Tracked page is not what Google shows ({x['ranking']}); wanted page has {x['wanted_share']:.0%} of impressions."))
    for g in gaps:
        d, ds = difficulty(None, g.get("kd"))
        opps.append(base(g["query"], "competitor_gap", difficulty_estimate=d, difficulty_source=ds, volume=g.get("volume"), kd=g.get("kd"), recommended_page=slugify(g["query"]),
                         page_exists=False, score=float(g.get("volume") or 0) / (1 + (g.get("kd") or 30) / 10),
                         notes="A competitor ranks top-20 for this (imported data); we have no query or page covering it. Volume/KD are from the import."))
    # one keyword -> one source row per device; priority across all
    return prioritise(opps)[:MAX_OPPS]


def write(opps):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    rows = [{**o, "updated_at": now} for o in opps]   # status / found_at left out on purpose: re-runs never reset an approval
    if rows: sb.upsert("keyword_opps", rows, "site_id,keyword,source,device")
    return len(rows)


def feed_ideas(site):
    """Approved opportunities that need a NEW page -> content_ideas (status 'suggested'), skipping anything already there."""
    sid = site["id"]
    opps = [o for o in sb.select("keyword_opps", site_id=f"eq.{sid}", status="eq.approved", page_exists="eq.false") if not o.get("idea_id")]
    if not opps: return 0
    have = [r["primary_keyword"] for r in sb.select("content_ideas", site_id=f"eq.{sid}", select="primary_keyword") if r.get("primary_keyword")]
    banned = [b.lower() for b in (site.get("banned_terms") or []) if b]; medical = bool(MEDICAL.search(" ".join(str(site.get(k) or "") for k in ("niche", "profile"))))
    n = 0
    for o in opps:
        k = o["keyword"].strip()
        if any(similar(k, h) for h in have) or any(b in k.lower() for b in banned):
            sb.update("keyword_opps", {"id": o["id"]}, {"status": "fed"}); continue   # already covered / not allowed: close it, do not duplicate
        why = "Competitors rank for this and we have no page for it (imported data)." + (" Medical topic: doctor review needed before publishing." if medical else "")
        idea = sb.insert("content_ideas", [{"site_id": sid, "title": k[:1].upper() + k[1:], "primary_keyword": k, "secondary_keywords": [], "post_type": None,
                                            "intent": (o.get("intent") or "Informational").lower(), "source_url": None, "volume": o.get("volume"), "kd": o.get("kd"),
                                            "why": why, "status": "suggested"}])
        have.append(k); n += 1
        sb.update("keyword_opps", {"id": o["id"]}, {"status": "fed", "idea_id": (idea[0] or {}).get("id") if idea else None})
    return n


def run(site_id):
    from . import run as runmod
    cfg = runmod.sites()[site_id]; site = runmod.ensure_site(cfg); site = {**cfg, **site}
    rows = fetch_rows(site, 28)
    tracked = sb.select("tracked_keywords", site_id=f"eq.{site_id}", active="eq.true")
    own = list(by_query(rows))
    gaps = competitor_gaps(site_id, own, site.get("banned_terms") or [], site.get("competitor_names") or [])
    opps = build(site, rows, tracked, gaps)
    print(f"GSC rows: {len(rows)} | striking: {sum(o['source'] == 'striking_distance' for o in opps)} | cannibalization: {sum(o['source'] == 'cannibalization' for o in opps)}"
          f" | mismatch: {sum(o['source'] == 'tracked_page_mismatch' for o in opps)} | gaps: {sum(o['source'] == 'competitor_gap' for o in opps)}")
    print("written:", write(opps)); print("fed into content_ideas:", feed_ideas(site))
    # mobile and desktop are tracked separately by the user: store the per-device striking list too
    for dev in [d for d in os.environ.get("KW_DEVICES", "desktop,mobile").split(",") if d]:
        try: r = fetch_rows(site, 28, dev)
        except Exception as e: print("device pull failed", dev, str(e)[:100]); continue
        d_opps = [o for o in build(site, r, [], [], device=dev) if o["source"] == "striking_distance"]; print(dev, "striking:", write(d_opps))


if __name__ == "__main__":
    from . import run as _r
    _r.each(sys.argv[1], run, kinds=("wordpress", "static", "html"))
