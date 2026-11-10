"""Publisher & Indexing agent. Run: python -m seo_agent.publisher <site_id|all>   (hourly from GitHub Actions)
  1. publish: at most `posts_per_day` posts per UTC+5:30 day, only inside the IST publish window, spaced apart, only approved drafts
     (medical / reviewer sites: reviewed_at must be set) that pass the quality gates. The real WordPress / FTP publish is content.publish_one.
  2. after publish: internal-link proposals in and out (proposals table, human approves), sitemap check, GSC task when it cannot be submitted.
  3. index check at day 1 / 3 / 7 (public fetch + GSC URL Inspection when available, else DATA NOT AVAILABLE). Not indexed after day 7 -> task.
Nothing here edits a live page except through content.publish_one (already-approved posts) and proposals (pending, need approval)."""
import os, re, sys, json, datetime as dt
from urllib.parse import urlparse
from . import sb

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UTC = dt.timezone.utc
MAX_CAP = int(os.environ.get("PUBLISHER_MAX_POSTS_PER_DAY", "5"))            # hard ceiling, whatever the site setting says
WINDOW = os.environ.get("PUBLISH_WINDOW", "9-21")                             # IST hours "start-end"; sites.publish_window_start/end win
CHECK_DAYS = (1, 3, 7)
RANKING = re.compile(r"\b(rank(s|ing)? (#?1|first|top)|top 1|number one|guaranteed? (results|ranking))\b", re.I)
now_utc = lambda: dt.datetime.now(UTC)


# ---------- time / cap ----------
def ist_day(t): return t.astimezone(IST).date()
def day_bounds(d):
    s = dt.datetime(d.year, d.month, d.day, tzinfo=IST); return s.astimezone(UTC), (s + dt.timedelta(days=1)).astimezone(UTC)
def parse_ts(s):
    if isinstance(s, dt.datetime): return s if s.tzinfo else s.replace(tzinfo=UTC)
    return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(UTC)
def cap_for(site):
    n = site.get("posts_per_day")
    try: n = 1 if n is None else int(n)
    except (TypeError, ValueError): n = 1
    return max(0, min(n, MAX_CAP))
def window_for(site):
    a, b = (int(x) for x in WINDOW.split("-"))
    a = site.get("publish_window_start") if site.get("publish_window_start") is not None else a
    b = site.get("publish_window_end") if site.get("publish_window_end") is not None else b
    return int(a), int(b)
def in_window(t, site):
    a, b = window_for(site); h = t.astimezone(IST); return a <= h.hour + h.minute / 60 < b
def gap_minutes(site, cap):
    a, b = window_for(site); return ((b - a) * 60) // cap if cap > 1 else 0


def published_today(site_id, t):
    """Posts that count against today's IST cap: content_posts published inside the IST day (covers posts the old content.publish path published too)."""
    lo, hi = day_bounds(ist_day(t))
    rows = sb.select("content_posts", site_id=f"eq.{site_id}", status="eq.published", published_at=f"gte.{lo.isoformat()}", select="id,published_at")
    return [r for r in rows if r.get("published_at") and lo <= parse_ts(r["published_at"]) < hi]


# ---------- quality gates ----------
def gates_for(site, p, content):
    """Every gate returns {ok, detail}. Same rules content.py enforces when it writes a draft (readability, word range, humanizer lint, banned terms)."""
    medical = site.get("profile") == "medical"; g = {}
    g["approved"] = {"ok": p.get("status") == "approved", "detail": f"status={p.get('status')}"}
    need = bool(site.get("reviewer_required") or medical)
    g["reviewer"] = {"ok": (not need) or bool(p.get("reviewed_at")), "detail": ("reviewed " + str(p.get("reviewed_at"))) if p.get("reviewed_at") else ("reviewer approval missing" if need else "not required")}
    html = p.get("html") or ""; words = len(content.plain(html).split()); tgt = p.get("target_words")
    lo, hi = (int(tgt * 0.85), int(tgt * 1.2)) if tgt else (300, 10 ** 6)
    g["word_count"] = {"ok": lo <= words <= hi, "detail": f"{words} words, allowed {lo} to {hi if tgt else 'any'}"}
    gr = content.grade(html); g["readability"] = {"ok": gr <= content.TARGET_GRADE, "detail": f"grade {gr}, max {content.TARGET_GRADE}"}
    probs = content.lint(html, site.get("banned_terms") or [], medical)
    g["humanizer_lint"] = {"ok": not probs, "detail": "; ".join(probs) or "clean"}
    text = " ".join([p.get("title") or "", p.get("meta_title") or "", p.get("meta_description") or "", content.plain(html)]).lower(); bad = []
    for n in site.get("competitor_names") or []:
        if n and n.lower() in text: bad.append(f"competitor name '{n}'")
    if RANKING.search(text): bad.append("ranking or guarantee claim")
    g["claims"] = {"ok": not bad, "detail": "; ".join(bad) or "clean"}
    g["content_present"] = {"ok": bool(html.strip() and p.get("title")), "detail": "title and html present" if html.strip() and p.get("title") else "empty title or html"}
    return g
def failed(g): return {k: v for k, v in g.items() if not v["ok"]}


# ---------- links ----------
def own_host(url): return re.sub(r"^www\.", "", urlparse(url).netloc.lower())
def internal_links(html, url):
    h = own_host(url); return [m for m in re.findall(r'<a\b[^>]*href=["\']([^"\']+)', html or "", re.I) if m.startswith("/") or own_host(m) == h]
def link_pairs(src_text, src_url, targets, linked):
    """Where an anchor the target's own keyword/title phrase already appears in src_text. Never invents text. targets: [{url, phrases:[...]}]"""
    out = []; low = src_text.lower()
    for t in targets:
        if t["url"] == src_url or t["url"] in linked: continue
        for ph in t["phrases"]:
            ph = (ph or "").strip()
            if len(ph.split()) >= 2 and re.search(r"(?<![a-z0-9])" + re.escape(ph.lower()) + r"(?![a-z0-9])", low):
                out.append((ph, t["url"])); break
    return out
def link_proposals(site, p, url, pid_of=lambda u: None):
    """Proposals (pending, human approves) for: links INTO the new post from older posts that already mention its topic, and links OUT of it to older posts."""
    sid = site["id"]; pub = sb.select("content_posts", site_id=f"eq.{sid}", status="eq.published", select="id,title,html,publish_url,wp_post_id,idea_id")
    pub = [r for r in pub if r.get("publish_url") and r["id"] != p["id"]]
    kw = {i["id"]: i.get("primary_keyword") for i in sb.select("content_ideas", site_id=f"eq.{sid}", select="id,primary_keyword")} if any(r.get("idea_id") for r in pub + [p]) else {}
    ph = lambda r: [kw.get(r.get("idea_id")), r.get("title")]
    from .humanize import plain
    new_t = {"url": url, "phrases": ph(p)}; made = {"in": 0, "out": 0}; have = {(x["page"], x["new"].get("url")) for x in sb.select("proposals", site_id=f"eq.{sid}", kind="eq.internal_link", select="page,new")}
    def add(page, post_id, anchor, target, why):
        if (page, target) in have: return
        sb.insert("proposals", [{"site_id": sid, "page": page, "post_id": post_id, "kind": "internal_link", "needs_approval": True, "old": {},
                                 "new": {"anchor": anchor[:80], "url": target, "placement": "paragraph that already contains the anchor"}, "reason": why, "status": "pending", "source": "publisher"}])
        have.add((page, target))
    for r in pub:   # in: older post mentions the new topic
        for a, t in link_pairs(plain(r["html"]), r["publish_url"], [new_t], set(internal_links(r["html"], r["publish_url"])))[:1]:
            add(r["publish_url"], r.get("wp_post_id") or pid_of(r["publish_url"]), a, t, "New post published on this topic; this page already uses the phrase"); made["in"] += 1
    olds = [{"url": r["publish_url"], "phrases": ph(r)} for r in pub]; mine = internal_links(p.get("html"), url)
    for a, t in link_pairs(plain(p["html"]), url, olds, set(mine))[:3]:   # out: new post mentions an older topic
        add(url, p.get("wp_post_id") or pid_of(url), a, t, "New post already uses this phrase; link to the older post on the same topic"); made["out"] += 1
    return {"existing_internal_links_out": len(mine), "proposals_in": made["in"], "proposals_out": made["out"], "low_out_links": len(mine) < 2}


# ---------- sitemap / index ----------
def fetch(url):
    import requests
    r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0 (compatible; SEOAgentPublisher)"}, allow_redirects=True)
    return {"status": r.status_code, "text": r.text if r.status_code == 200 else "", "headers": dict(r.headers)}
def norm(u): return u.split("#")[0].rstrip("/").lower()
def sitemap_lists(base, url, get=fetch):
    """Look for url in the site's sitemap (index files are followed one level). Returns {listed: True/False/None, sitemap}. None = no readable sitemap."""
    seen = []; found = None
    for path in ("/sitemap.xml", "/wp-sitemap.xml", "/sitemap_index.xml"):
        try: r = get(base.rstrip("/") + path)
        except Exception: continue
        if r.get("status") != 200 or "<loc>" not in r.get("text", ""): continue
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", r["text"], re.S); seen.append(path); found = False
        subs = [l for l in locs if l.lower().endswith(".xml")]
        if norm(url) in {norm(l) for l in locs}: return {"listed": True, "sitemap": path}
        for s in subs[:20]:
            try: sr = get(s)
            except Exception: continue
            if sr.get("status") == 200 and norm(url) in {norm(l) for l in re.findall(r"<loc>\s*(.*?)\s*</loc>", sr.get("text", ""), re.S)}: return {"listed": True, "sitemap": s}
    return {"listed": found, "sitemap": seen[0] if seen else None}
def public_check(url, get=fetch):
    try: r = get(url)
    except Exception as e: return {"http": None, "error": str(e)[:120], "blocked": True}
    t = r.get("text", ""); xr = {k.lower(): v for k, v in (r.get("headers") or {}).items()}.get("x-robots-tag", "")
    meta = re.search(r'<meta[^>]+name=["\']robots["\'][^>]*content=["\']([^"\']*)', t, re.I)
    noindex = "noindex" in (xr or "").lower() or bool(meta and "noindex" in meta.group(1).lower())
    can = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]*href=["\']([^"\']+)', t, re.I)
    return {"http": r.get("status"), "noindex": noindex, "canonical_self": (norm(can.group(1)) == norm(url)) if can else None, "blocked": r.get("status") != 200 or noindex}
def gsc_inspect(site, url):
    """GSC URL Inspection through gsc.service() (read-only scope). None = not available (no credentials / no property / call failed)."""
    if not (os.environ.get("GSC_SERVICE_ACCOUNT_JSON") and site.get("gsc_property")): return None
    try:
        from . import gsc
        r = gsc.service().urlInspection().index().inspect(body={"inspectionUrl": url, "siteUrl": site["gsc_property"]}).execute().get("inspectionResult", {}).get("indexStatusResult", {})
        return {"verdict": r.get("verdict"), "coverage": r.get("coverageState"), "last_crawl": r.get("lastCrawlTime"), "google_canonical": r.get("googleCanonical")}
    except Exception as e: return {"error": str(e)[:160]}
def decide(pub, g):
    if g and g.get("verdict"): return "indexed" if g["verdict"] == "PASS" else "not_indexed"
    return "blocked" if pub.get("blocked") else "DATA NOT AVAILABLE"
def advice(site, url, status, evidence):
    """Short next-step note for the Strategist. Claude when available, fixed text otherwise. Never changes the decision."""
    fallback = {"not_indexed": "Check for thin or duplicate content, add internal links from indexed pages, then request indexing in GSC. Do not resubmit repeatedly.",
                "blocked": "The page returns an error or noindex. Fix that first, then request indexing in GSC.",
                "DATA NOT AVAILABLE": "GSC URL Inspection is not available for this site. Open GSC > URL Inspection and check the page by hand."}[status]
    try:
        from .llm import ask
        t = ask("You are an SEO publisher assistant. Give 2 short, concrete next steps (plain English, no guarantees, no spam tactics, no invented data) for a page that is not confirmed indexed. Use only the evidence given.",
                json.dumps({"url": url, "status": status, "evidence": evidence}), agent="publisher", site_id=site["id"], max_tokens=300)
        return t.strip() or fallback
    except Exception: return fallback
def task(site, url, kind, title, detail, evidence, conf="Medium", assignee="strategist"):
    if sb.select("publisher_tasks", site_id=f"eq.{site['id']}", url=f"eq.{url}", kind=f"eq.{kind}"): return False
    sb.insert("publisher_tasks", [{"site_id": site["id"], "url": url, "kind": kind, "assignee": assignee, "title": title, "detail": detail, "evidence": evidence, "confidence": conf}]); return True


def after_publish(site, row, p, url, t, get=fetch, pid_of=lambda u: None):
    """Links, sitemap, indexing task. Each part fails on its own; the publish_log row already exists."""
    upd = {}; notes = []
    try: upd["links"] = link_proposals(site, p, url, pid_of)
    except Exception as e: notes.append("links failed: " + str(e)[:100])
    base = (site.get("wp_base") or "https://" + site["domain"]).rstrip("/")
    try:
        sm = sitemap_lists(base, url, get); sm["checked_at"] = t.isoformat(); upd["sitemap"] = sm
        if sm["listed"] is not True:
            task(site, url, "sitemap_missing", "New post is not in the sitemap" if sm["listed"] is False else "No readable sitemap found", "Check the SEO plugin / sitemap.xml so the new URL appears. Then request indexing.", sm, "Medium")
    except Exception as e: notes.append("sitemap check failed: " + str(e)[:100])
    try: from . import gsc; sub = getattr(gsc, "submit_sitemap", None)
    except Exception: sub = None
    if sub:
        try: sub(site, base + "/sitemap.xml"); notes.append("sitemap submitted via GSC")
        except Exception as e: sub = None; notes.append("sitemap submit failed: " + str(e)[:100])
    if not sub:   # gsc.py has only the read-only scope; Google's sitemap ping is retired
        task(site, url, "request_indexing", "manual: request indexing in GSC", "Open GSC > URL Inspection > paste the URL > Request indexing. The agent cannot do this: the Search Console API has no request-indexing call.", {"url": url}, "High", "owner")
    if notes: upd["notes"] = "; ".join(notes)
    sb.update("publish_log", {"id": row["id"]}, upd); return upd


# ---------- main ----------
def log_row(site_id, p, url, t, gates):
    old = sb.select("publish_log", site_id=f"eq.{site_id}", post_id=f"eq.{p['id']}")
    if old: return old[0], False
    return sb.insert("publish_log", [{"site_id": site_id, "url": url, "post_id": p["id"], "published_at": t.isoformat(), "cap_day": str(ist_day(t)), "gates": gates, "index_status": "pending", "checked_at": {}}])[0], True


def reconcile(site, content, t, get, pid_of):
    """A run that died between 'published' and the log write: log those posts now (once) and finish their follow-up steps."""
    since = (t - dt.timedelta(days=2)).isoformat()
    for p in sb.select("content_posts", site_id=f"eq.{site['id']}", status="eq.published", published_at=f"gte.{since}", order="published_at.desc", limit="30"):
        if not p.get("publish_url") or not p.get("published_at"): continue
        row, new = log_row(site["id"], p, p["publish_url"], parse_ts(p["published_at"]), {"note": {"ok": True, "detail": "published outside the publisher (or log write was interrupted); gates not recorded"}})
        if new: print("logged earlier publish:", p["title"]); after_publish(site, row, p, p["publish_url"], t, get, pid_of)


def publish_site(site, w, content, t, get=fetch):
    sid = site["id"]; cap = cap_for(site); done = published_today(sid, t)
    if len(done) >= cap: return print(f"cap reached: {len(done)}/{cap} for IST day {ist_day(t)}")
    if not in_window(t, site): return print("outside publish window", window_for(site), "IST")
    gap = gap_minutes(site, cap)
    if done and gap and t - max(parse_ts(r["published_at"]) for r in done) < dt.timedelta(minutes=gap): return print(f"spacing: next post at least {gap} min after the last one")
    pid_of = getattr(w, "pid", lambda u: None) if getattr(w, "static", False) else (lambda u: None)
    for p in sb.select("content_posts", site_id=f"eq.{sid}", status="eq.approved", order="approved_at.asc", limit="20"):
        g = gates_for(site, p, content); bad = failed(g)
        if bad.get("reviewer") and len(bad) == 1: print("waiting for reviewer:", p["title"]); continue
        if bad:
            msgs = [f"publisher gate {k}: {v['detail']}" for k, v in bad.items()]
            sb.update("content_posts", {"id": p["id"]}, {"status": "draft", "issues": [i for i in (p.get("issues") or []) if not str(i).startswith("publisher gate")] + msgs}); print("blocked:", p["title"], msgs); continue
        url = content.publish_one(site, w, p)
        if not url: print("not published (publish_one skipped it):", p["title"]); continue
        row, _ = log_row(sid, p, url, t, g); print("published:", p["title"], url); after_publish(site, row, p, url, t, get, pid_of)
        return   # one post per run: this is what spaces the posts out


def check_index(site, t, get=fetch, inspect=gsc_inspect):
    for r in sb.select("publish_log", site_id=f"eq.{site['id']}", order="published_at.desc", limit="60"):
        if r["index_status"] == "indexed": continue
        age = (t - parse_ts(r["published_at"])).days; ck = dict(r.get("checked_at") or {})
        due = [f"d{d}" for d in CHECK_DAYS if age >= d and f"d{d}" not in ck]
        if not due: continue
        pub = public_check(r["url"], get); g = inspect(site, r["url"]); gv = g if g and not g.get("error") else None
        status = decide(pub, gv); ev = {"at": t.isoformat(), "age_days": age, "public_fetch": pub, "gsc": g if g else "DATA NOT AVAILABLE", "status": status}
        for k in due: ck[k] = ev
        upd = {"index_status": status, "checked_at": ck}
        try:
            sm = sitemap_lists((site.get("wp_base") or "https://" + site["domain"]), r["url"], get); sm["checked_at"] = t.isoformat(); upd["sitemap"] = sm
        except Exception: pass
        sb.update("publish_log", {"id": r["id"]}, upd); print("index check", due, r["url"], "->", status)
        if status == "blocked" or (status == "not_indexed" and age >= 7):
            task(site, r["url"], "blocked" if status == "blocked" else "not_indexed", f"Not indexed after {age} days" if status != "blocked" else "Published page is blocked (error or noindex)",
                 advice(site, r["url"], status, ev), ev, "High" if gv or status == "blocked" else "Medium")
        elif status == "DATA NOT AVAILABLE" and age >= 7:
            task(site, r["url"], "manual_index_check", "Check indexing by hand (GSC data not available)", advice(site, r["url"], status, ev), ev, "Low", "owner")


def run_site(site_id, t=None, get=fetch, inspect=gsc_inspect):
    from . import run as R, wp, content
    t = t or now_utc(); cfg = R.sites()[site_id]; site = {**cfg, **R.ensure_site(cfg)}
    if site.get("mode") != "paused":
        if not hasattr(content, "publish_one"): raise RuntimeError("content.py needs publish_one(); apply agents2/publisher/PATCHES.md first")
        w = wp.client(cfg); reconcile(site, content, t, get, getattr(w, "pid", lambda u: None) if getattr(w, "static", False) else (lambda u: None))
        publish_site(site, w, content, t, get)
    else: print("site paused: no publishing")
    check_index(site, t, get, inspect)


if __name__ == "__main__":
    from . import run as _r
    _r.each(sys.argv[1], run_site, kinds=("wordpress", "static", "html"))
