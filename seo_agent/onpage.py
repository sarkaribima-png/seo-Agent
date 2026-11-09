"""Old-pages SEO in bulk. Walks a site's latest page audits, reads each page live, and writes PENDING proposals (source 'onpage'):
    title_meta  missing / too long / too short / duplicate / claim-heavy title or meta description
    alt_text    images with an empty alt attribute (applied through plugin kind 'alt' or the FTP adapter)
H1 problems are not auto-fixable: they are saved as findings (table onpage_findings) for a person to fix in the editor.
Nothing is applied here. A person approves in the dashboard; medical sites also need the reviewer's "Mark as reviewed".
Run: python -m seo_agent.onpage <site_id|all> [limit]        (limit = most proposals to create in this run)"""
import os, re, sys, json, html, time, datetime as dt
from urllib.parse import urlparse
import requests
from . import sb, guard
from .humanize import lint, RISKY
from .static import img_key

DEFAULT_LIMIT = int(os.environ.get("ONPAGE_LIMIT", "20")); HARD_LIMIT = 60
FETCH_CAP = int(os.environ.get("ONPAGE_FETCH_CAP", "200")); FETCH_PAUSE = float(os.environ.get("ONPAGE_PAUSE", "0.4"))
MAX_ALTS = 12
MODEL = os.environ.get("ONPAGE_MODEL") or os.environ.get("SEO_MODEL", "claude-haiku-5-5")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
CITIES = ["gurgaon", "gurugram", "delhi", "noida", "mumbai", "bangalore", "bengaluru", "pune", "hyderabad", "chennai", "kolkata", "jaipur", "chandigarh", "lucknow", "ahmedabad"]
TITLE_OK = (30, 60); META_OK = (110, 160)


def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def norm(u): return (u or "").split("#")[0].rstrip("/").lower().replace("://www.", "://")
def clean(s): return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def ask(system, user, max_tokens=3000):
    import anthropic
    r = anthropic.Anthropic().messages.create(model=MODEL, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")


def select_all(table, max_rows=6000, **params):
    """Supabase returns at most 1000 rows per call, so page through the result."""
    out = []
    while len(out) < max_rows:
        rows = sb.select(table, **{**params, "limit": "1000", "offset": str(len(out))})
        out += rows
        if len(rows) < 1000: break
    return out


# ---------- reading a live page ----------
def fetch_page(url):
    """Live title, meta, headings and images WITHOUT alt text. Returns None when the page cannot be used (error, redirect, other canonical, noindex)."""
    from bs4 import BeautifulSoup
    try: r = requests.get(url, headers=UA, timeout=30, allow_redirects=True)
    except requests.RequestException: return None
    if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"): return None
    if norm(r.url) != norm(url): return None
    return parse_page(r.text, url)


def parse_page(raw, url):
    from bs4 import BeautifulSoup
    full = BeautifulSoup(raw, "html.parser")
    meta = lambda **k: ((full.find("meta", attrs=k) or {}).get("content") or "").strip()
    can = full.find("link", rel="canonical"); can = (can.get("href") if can else "") or ""
    robots = meta(name="robots").lower()
    if "noindex" in robots or (can and norm(can) != norm(url)): return None
    h1 = [clean(h.get_text(" ")) for h in full.find_all("h1")]
    heads = [(h.name, clean(h.get_text(" "))) for h in full.find_all(["h1", "h2", "h3"]) if clean(h.get_text(" "))]
    body = BeautifulSoup(raw, "html.parser")
    for t in body(["script", "style", "noscript", "header", "nav", "footer", "aside"]): t.decompose()
    root = body.find("main") or body.find("article") or body.body or body
    imgs, seen = [], set()
    for im in root.find_all("img"):
        if (im.get("alt") or "").strip(): continue
        if im.get("role") == "presentation" or im.get("aria-hidden") == "true": continue
        src = next((im.get(a) for a in ("data-src", "data-lazy-src", "data-original") if im.get(a)), None) or im.get("src") or ""
        if not src or src.startswith("data:"): continue
        try:
            if max(int(re.sub(r"\D", "", str(im.get("width") or "999")) or 999), int(re.sub(r"\D", "", str(im.get("height") or "999")) or 999)) <= 2: continue
        except ValueError: pass
        k = img_key(src)
        if not k or k in seen: continue
        seen.add(k)
        prev = im.find_previous(["h1", "h2", "h3", "h4"]); fig = im.find_parent("figure"); cap = fig.find("figcaption") if fig else None
        par = im.find_parent(["p", "div", "li", "figure"])
        imgs.append({"src": src, "key": k, "file": os.path.basename(urlparse(src).path), "section": clean(prev.get_text(" "))[:120] if prev else "",
                     "caption": clean(cap.get_text(" "))[:150] if cap else "", "title_attr": (im.get("title") or "")[:100], "near": clean(par.get_text(" "))[:200] if par else ""})
    return {"url": url, "title": clean(full.title.get_text()) if full.title else "", "meta": clean(meta(name="description")), "h1": h1,
            "h2": [t for n, t in heads if n == "h2"][:25], "images": imgs}


# ---------- finding problems ----------
def city_set(*texts):
    low = " ".join(t or "" for t in texts).lower()
    return {c for c in CITIES if re.search(r"\b" + c + r"\b", low)}


def copy_problems(title, meta, site):
    """Claim and wording problems in existing or proposed copy (shared by diagnosis and by checking Claude's output)."""
    out = []; low = f"{title} {meta}".lower()
    if re.search(r"\bbest\b|\btop[- ]?(1|rated)\b|\bleading\b|\bno\.? ?1\b", low): out.append("'best'/'top' style claim")
    if re.search(r"\b(affordable|cheap(est)?|trusted|proven|top choice|safe and effective)\b", low): out.append("unproven claim word (affordable, trusted, proven...)")
    if re.search(r"₹|\brs\.?\s?\d|\binr\b|\bstarting (at|from|price)\b", low): out.append("price in the text")
    for b in RISKY:
        if re.search(r"(?<![a-z0-9])" + re.escape(b) + r"(?![a-z0-9])", low) and b not in ("before and after", "before & after"): out.append(f"risky word '{b}'")
    for t in (site.get("banned_terms") or []) + (site.get("competitor_names") or []):
        if t and t.lower() in low: out.append(f"banned term '{t}'")
    return list(dict.fromkeys(out))


def diagnose(page, dup_of, site):
    """page: fetch_page() result. dup_of: URL of the page that keeps this title, or ''. Returns a list of issue codes."""
    t, m, i = page["title"], page["meta"], []
    if not t: i.append("title_missing")
    elif len(t) > TITLE_OK[1]: i.append("title_long")
    elif len(t) < TITLE_OK[0]: i.append("title_short")
    if not m: i.append("meta_missing")
    elif len(m) > META_OK[1]: i.append("meta_long")
    elif len(m) < META_OK[0]: i.append("meta_short")
    if dup_of: i.append("duplicate_title")
    if copy_problems(t, m, site): i.append("risky_copy")
    return i


def h1_finding(page):
    n = len(page["h1"])
    if n == 1: return None
    if n == 0: return {"issue": "h1_missing", "detail": "No H1 found on the live page. Add one H1 that names the page topic in plain words.", "found": []}
    return {"issue": f"h1_count_{n}", "detail": f"{n} H1 tags found. Keep one H1 for the page topic and change the others to H2.", "found": page["h1"][:5]}


# ---------- alt text ----------
ALT_BAD_START = re.compile(r"^(image|picture|photo|photograph|graphic|icon|screenshot) (of|showing)\b|^click here|^(img|dsc|image|photo)[-_ ]?\d+", re.I)


def check_alt(alt, site):
    """Errors in one alt text. Empty list = acceptable. Shared with the FTP adapter, which checks again before it writes."""
    a = (alt or "").strip(); e = []
    if len(a) < 4: return ["too short"]
    if len(a) > 125: e.append("longer than 125 characters")
    if ALT_BAD_START.search(a): e.append("starts with 'image of' or looks like a file name")
    if re.search(r"\.(jpe?g|png|webp|gif|svg)\b", a, re.I): e.append("contains a file extension")
    if re.search(r"[—–]", a): e.append("contains a dash character")
    words = [w for w in re.findall(r"[a-z]+", a.lower()) if len(w) > 3]
    if any(words.count(w) > 2 for w in set(words)) or a.count(",") > 3: e.append("looks like keyword stuffing")
    e += copy_problems(a, "", site)
    if re.search(r"\b(results?|success|cured?|transformation|before|after|recovered|years? old|age \d+)\b|\d\s?%", a, re.I): e.append("result or patient claim")
    e += [x for x in lint(f"<p>{a}</p>", site.get("banned_terms") or [], site.get("profile") == "medical") if "call to action" not in x]
    return list(dict.fromkeys(e))


ALT_SYS = """You write image alt text for web pages. You can NOT see the images. You only get the file name, the nearest heading, a caption and nearby text.
Rules:
- Describe what the image most likely shows, using only those clues. If the clues do not tell you what the image shows, set "skip": true and "alt": "". Never guess.
- 5 to 15 words, at most 125 characters, plain words. Do not start with 'image of', 'picture of' or 'photo of'.
- No claims: no results, outcomes, before or after, success, percentages, prices, ages, patient names, awards, 'best', 'top'.
- No keyword stuffing. Do not repeat the page title. Each alt must be different from the others. Do not add a city unless the clues show the image is of that place.
- No em dashes or en dashes (use commas or words). No competitor names.
Reply with JSON only: [{"i":0,"alt":"","skip":false}]"""


def gen_alts(page, imgs, site, askf):
    """Returns [{'src','alt','section'}] for the images where Claude gave a usable, valid, unique alt."""
    imgs = imgs[:MAX_ALTS]; fb = ""; good = {}
    for _ in range(2):
        todo = [(n, im) for n, im in enumerate(imgs) if n not in good]
        if not todo: break
        msg = {"page_h1": (page["h1"] or [""])[0], "page_title": page["title"], "problems_in_last_try": fb,
               "images": [{"i": n, "file": im["file"], "section": im["section"], "caption": im["caption"], "title_attr": im["title_attr"], "nearby_text": im["near"]} for n, im in todo]}
        t = askf(ALT_SYS, json.dumps(msg), 2500)
        try: arr = json.loads(t[t.index("["): t.rindex("]") + 1])
        except ValueError: fb = "return a JSON array only"; continue
        probs = []
        for x in arr if isinstance(arr, list) else []:
            n = x.get("i"); alt = re.sub(r"\s+", " ", str(x.get("alt") or "")).strip()
            if not isinstance(n, int) or n < 0 or n >= len(imgs) or x.get("skip") or not alt: continue
            e = check_alt(alt, site)
            if alt.lower() in {v.lower() for v in good.values()}: e.append("same alt as another image on the page")
            if e: probs.append(f"i={n}: {'; '.join(e)}")
            else: good[n] = alt
        fb = " | ".join(probs)
    return [{"src": imgs[n]["src"], "alt": a, "section": imgs[n]["section"]} for n, a in sorted(good.items())]


# ---------- title and meta ----------
TM_SYS = """You are an SEO editor for an existing page. Fix the title tag and meta description named in 'issues'. Write ONE title and ONE meta description.
Rules: title 30-60 characters; meta 110-155 characters. Use only facts that appear in the input (current title, meta, h1, h2). Never invent anything.
Never claim rankings, guarantees, percentages, prices, 'best', 'top', 'leading', FDA or NABH approval, 'painless', 'scarless', or competitor names.
Keep the main topic and any city already in the current title or h1; never add a city that is not in the input. The title must be different from every title in other_titles.
If the current title already ends with the brand name and there is room, keep it. Plain words, no dashes, no emojis, no all caps, no call to action like 'book now'.
Meta: say what the page covers (from the h2 list) as the reason to click. Reply JSON only: {"title":"","meta":"","why":""}"""


def gen_title_meta(page, issues, taken, site, askf, queries=None, brand=""):
    fb = ""; med = site.get("profile") == "medical"; cities_ok = city_set(page["title"], page["meta"], " ".join(page["h1"]), " ".join(page["h2"]), page["url"])
    for _ in range(3):
        msg = {"url": page["url"], "issues": issues, "current_title": page["title"], "current_meta": page["meta"], "h1": page["h1"][:2], "h2": page["h2"][:15],
               "top_queries": queries or [], "brand": brand, "other_titles": sorted(taken)[:12], "medical_site": med, "previous_errors": fb}
        t = askf(TM_SYS, json.dumps(msg), 1500)
        try: d = json.loads(t[t.index("{"): t.rindex("}") + 1])
        except ValueError: fb = "return JSON only"; continue
        ti, me = clean(d.get("title")), clean(d.get("meta")); e = guard.check_seo(ti, me, site)
        e += copy_problems(ti, me, site); e += lint(f"<p>{ti}</p><p>{me}</p>", site.get("banned_terms") or [], med)
        if ti.lower() in {x.lower() for x in taken}: e.append("title is already used by another page")
        new_c = city_set(ti, me)
        if new_c - cities_ok: e.append("city not present on the page: " + ", ".join(sorted(new_c - cities_ok)))
        old_c = city_set(page["title"]) - city_set(ti)
        if old_c: e.append("the city in the current title was removed: " + ", ".join(sorted(old_c)))
        if not e: return {"title": ti, "meta": me, "why": clean(d.get("why"))[:300]}
        fb = "; ".join(dict.fromkeys(e))
    return None


# ---------- the batch ----------
ISSUE_TEXT = {"title_missing": "no title tag", "title_long": "title over 60 characters", "title_short": "title under 30 characters", "meta_missing": "no meta description",
              "meta_long": "meta over 160 characters", "meta_short": "meta under 110 characters", "duplicate_title": "same title as another page", "risky_copy": "claim, price or banned word in the copy"}


def build(site_id, site, audits, fetcher, askf, impressions=None, existing=(), limit=DEFAULT_LIMIT, queries_fn=None, pause=0.0):
    """audits: latest page_audits rows. Returns (proposals, findings, notes). Pure apart from the fetcher / askf callables it is given."""
    limit = max(1, min(int(limit or DEFAULT_LIMIT), HARD_LIMIT)); impressions = impressions or {}; notes = []; ymyl = site.get("profile") == "medical" or bool(site.get("reviewer_required"))
    rows = sorted(audits, key=lambda a: -impressions.get(norm(a["page"]), 0))[:FETCH_CAP]; pages = []
    for a in rows:
        if "?" in a["page"] or re.search(r"\.(pdf|jpe?g|png|webp|xml)$", a["page"], re.I): continue
        p = fetcher(a["page"])
        if p is None: notes.append(f"skipped (error, redirect, noindex or other canonical): {a['page']}")
        else: pages.append((a, p))
        if pause: time.sleep(pause)
    first = {}; dups = {}   # duplicate titles: the page with most impressions keeps the title, the others get a new one
    for a, p in pages:
        k = p["title"].lower()
        if not k: continue
        if k in first: dups[norm(p["url"])] = first[k]
        else: first[k] = p["url"]
    taken = {p["title"] for _, p in pages if p["title"]}
    brand = site.get("brand_name") or site.get("brand") or (urlparse("//" + (site.get("domain") or "")).netloc.replace("www.", "").split(".")[0].title() if site.get("domain") else "")
    have = {(norm(x["page"]), x["kind"]) for x in existing}; props, findings = [], []
    base = {"site_id": site_id, "needs_approval": True, "status": "pending", "source": "onpage"}
    flag = " DOCTOR TO CONFIRM before applying (medical site)." if ymyl else ""
    for a, p in pages:
        f = h1_finding(p)
        if f: findings.append({"site_id": site_id, "page": p["url"], "status": "open", **f})
        if len(props) >= limit: continue
        issues = diagnose(p, dups.get(norm(p["url"]), ""), site); pid = a.get("post_id")
        if issues and (norm(p["url"]), "title_meta") not in have and len(props) < limit:
            q = None
            try: q = queries_fn(p["url"]) if queries_fn else None
            except Exception: q = None
            d = gen_title_meta(p, issues, taken - {p["title"]}, site, askf, q, brand)
            if d and (d["title"], d["meta"]) != (p["title"], p["meta"]):
                taken.add(d["title"])
                new = {"title": d["title"], "meta": d["meta"], "issues": issues}
                if ymyl: new["review"] = "DOCTOR TO CONFIRM"
                props.append({**base, "page": p["url"], "post_id": pid, "kind": "title_meta", "old": {"title": p["title"], "meta": p["meta"]}, "new": new,
                              "reason": "On-page fix: " + ", ".join(ISSUE_TEXT[x] for x in issues) + ". " + d["why"] + flag})
            else: notes.append(f"no safe title/meta could be written for {p['url']} ({', '.join(issues)})")
        if p["images"] and (norm(p["url"]), "alt_text") not in have and len(props) < limit:
            alts = gen_alts(p, p["images"], site, askf)
            if alts:
                new = {"images": alts, "basis": "file name, nearest heading, caption and nearby text (the images were not looked at)"}
                if ymyl: new["review"] = "DOCTOR TO CONFIRM"
                props.append({**base, "page": p["url"], "post_id": pid, "kind": "alt_text", "old": {"images": [{"src": x["src"], "alt": ""} for x in alts]}, "new": new,
                              "reason": f"{len(alts)} of {len(p['images'])} images without alt text. Written from the file name and the text around each image, so check a few before approving." + flag})
    return props, findings, notes


def impressions_28d(site_id):
    since = str(dt.date.today() - dt.timedelta(days=30)); out = {}
    try:
        for r in select_all("gsc_daily", site_id=f"eq.{site_id}", date=f"gte.{since}", select="page,impressions"):
            k = norm(r["page"]); out[k] = out.get(k, 0) + (r.get("impressions") or 0)
    except Exception as e: print("note: impressions not available, pages are taken in audit order:", str(e)[:80])
    return out


def latest_audits(site_id):
    seen, out = set(), []
    for r in select_all("page_audits", site_id=f"eq.{site_id}", select="page,post_id,audited_at,h1,images_no_alt", order="audited_at.desc", max_rows=5000):
        k = norm(r["page"])
        if k not in seen: seen.add(k); out.append(r)
    return out


def run(site_id, limit=None):
    from . import run as runmod
    cfg = runmod.sites()[site_id]; row = (sb.select("sites", id=f"eq.{site_id}") or [{}])[0]; site = {**cfg, **{k: v for k, v in row.items() if v not in (None, [], "")}}
    if site.get("mode") == "paused": return print("site paused from dashboard")
    audits = latest_audits(site_id)
    if not audits: return print("no page audits yet: run 'audit-all' for this site first")
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=21)).isoformat()
    existing = select_all("proposals", site_id=f"eq.{site_id}", status="in.(pending,approved)", kind="in.(title_meta,alt_text)", select="page,kind") + \
               select_all("proposals", site_id=f"eq.{site_id}", status="eq.applied", applied_at=f"gte.{since}", kind="in.(title_meta,alt_text)", select="page,kind")
    qf = None
    if os.environ.get("GSC_SERVICE_ACCOUNT_JSON"):
        from . import gsc
        qf = lambda u: gsc.top_queries(site, u)
    props, findings, notes = build(site_id, site, audits, fetch_page, ask, impressions_28d(site_id), existing, limit or DEFAULT_LIMIT, qf, FETCH_PAUSE)
    if props: sb.insert("proposals", props)
    if findings:
        try:
            old = {(norm(x["page"]), x["issue"]) for x in select_all("onpage_findings", site_id=f"eq.{site_id}", status="eq.open", select="page,issue")}
            new = [f for f in findings if (norm(f["page"]), f["issue"]) not in old]
            if new: sb.insert("onpage_findings", new)
        except Exception as e: print("note: findings not saved (run schema_v19.sql):", str(e)[:100])
    for n in notes[:30]: print(" -", n)
    print(f"pages audited: {len(audits)} | proposals created: {len(props)} | h1 findings: {len(findings)}")


if __name__ == "__main__":
    from . import run as _r
    lim = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else None
    _r.each(sys.argv[1], lambda sid: run(sid, lim), kinds=("wordpress", "static", "html"))
