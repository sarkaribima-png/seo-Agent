"""Built-in page audit: your page vs competitor pages for one keyword. No outside tool needed.
audit(keyword, own_url, competitor_urls, candidates, niche) -> report dict used by rank.py"""
import os, re, json, html as H
from urllib.parse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup

MODEL = os.environ.get("AUDIT_MODEL", "claude-haiku-5-5")
UA = {"User-Agent": "Mozilla/5.0 (compatible; KalosaSEOAudit/1.0)"}


def norm(u): return (u or "").split("#")[0].rstrip("/").lower()
def host(u): return urlparse(u).netloc.lower().replace("www.", "")


def fetch(url):
    r = requests.get(url, headers=UA, timeout=30)
    if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"): raise RuntimeError(f"{url} returned {r.status_code}")
    return r.text


def jsonld_types(soup):
    out = []
    for s in soup.find_all("script", type="application/ld+json"):
        try: d = json.loads(s.string or "")
        except ValueError: continue
        for x in (d if isinstance(d, list) else d.get("@graph", [d]) if isinstance(d, dict) else []):
            t = x.get("@type") if isinstance(x, dict) else None
            out += t if isinstance(t, list) else [t] if t else []
    return sorted(set(out))


def extract(url, raw=None):
    raw = raw or fetch(url); full = BeautifulSoup(raw, "html.parser"); types = jsonld_types(full)
    meta = lambda **k: ((full.find("meta", attrs=k) or {}).get("content") or "").strip()
    can = full.find("link", rel="canonical"); title = full.title.get_text().strip() if full.title else ""
    soup = BeautifulSoup(raw, "html.parser")
    for t in soup(["script", "style", "noscript", "nav", "footer", "header", "form", "aside"]): t.decompose()
    body = soup.find("main") or soup.find("article") or soup.body or soup
    text = re.sub(r"\s+", " ", body.get_text(" ")).strip()
    imgs = body.find_all("img"); d = host(url)
    internal = {urljoin(url, a["href"]) for a in body.find_all("a", href=True) if host(urljoin(url, a["href"])) == d}
    heads = [(h.name, re.sub(r"\s+", " ", h.get_text(" ")).strip()) for h in body.find_all(["h1", "h2", "h3"])]
    return {"url": url, "title": title, "meta": meta(name="description"), "canonical": (can.get("href") if can else "") or "",
            "h1": [t for n, t in heads if n == "h1"][:3], "h2": [t for n, t in heads if n == "h2"][:30], "h3": [t for n, t in heads if n == "h3"][:30],
            "words": len(text.split()), "images": len(imgs), "images_no_alt": sum(1 for i in imgs if not (i.get("alt") or "").strip()),
            "internal_links": len(internal), "tables": len(body.find_all("table")), "lists": len(body.find_all(["ul", "ol"])),
            "schema": types, "has_faq": "FAQPage" in types or any("faq" in t.lower() or "frequently asked" in t.lower() for n, t in heads),
            "og": bool(meta(property="og:title")), "text": text[:9000], "text_lower": text.lower()}


def kw_hits(p, kw):
    k = kw.lower(); toks = [t for t in re.findall(r"\w+", k) if len(t) > 2]
    cover = lambda s: sum(1 for t in toks if t in (s or "").lower()) / max(1, len(toks))
    return {"title": k in p["title"].lower(), "meta": k in (p["meta"] or "").lower(), "h1": any(k in h.lower() for h in p["h1"]),
            "h2_with_kw_words": sum(1 for h in p["h2"] if cover(h) >= .5), "title_cov": round(cover(p["title"]), 2),
            "body_mentions": p["text_lower"].count(k)}


def score(p, kw):
    s = 0; h = kw_hits(p, kw)
    s += 12 if h["title"] else int(12 * h["title_cov"]); s += 8 if h["meta"] else 0; s += 8 if h["h1"] else 0
    s += min(10, h["h2_with_kw_words"] * 3); s += min(15, p["words"] // 100); s += 8 if 30 <= len(p["title"]) <= 62 else 0
    s += 6 if 100 <= len(p["meta"] or "") <= 165 else 0; s += 6 if len(p["h2"]) >= 4 else 0; s += 6 if p["has_faq"] else 0
    s += min(8, len(p["schema"]) * 3); s += 5 if p["images"] and p["images_no_alt"] == 0 else 0; s += min(8, p["internal_links"])
    s += 4 if p["tables"] or p["lists"] else 0
    return min(100, s)


def wp_pages(base, own, n=30):
    """All public pages/posts of the site, used as internal-link candidates when Search Console is unavailable."""
    out = []
    for kind in ("pages", "posts"):
        try:
            r = requests.get(f"{base.rstrip('/')}/wp-json/wp/v2/{kind}", params={"per_page": n, "_fields": "link,title", "orderby": "modified"}, headers=UA, timeout=30)
            for x in r.json() if r.ok else []:
                t = H.unescape(re.sub(r"<[^>]+>", "", (x.get("title") or {}).get("rendered", ""))).strip()
                if t and norm(x["link"]) != norm(own) and urlparse(x["link"]).path.strip("/"): out.append({"url": x["link"], "topic": t[:150]})
        except Exception: pass
    return out[:20]


SYS = """You are a senior SEO analyst. Compare OWN page with COMPETITOR pages for one keyword and say what OWN is missing to rank.
Use only facts in the input. Never invent statistics, prices, awards or claims. Never name competitors in suggested text. Never suggest Review or AggregateRating schema.
Reply JSON only, this exact shape:
{"hypothesis":["why competitors may rank above, 3-5 short sentences"],
 "recommendations":[{"priority":"high|medium|low","action":"one concrete change to OWN"}],
 "content_gaps":[{"topic":"subject competitors cover and OWN lacks"}],
 "content_briefs":[{"topic":"","points":["facts-free outline points to cover"]}],
 "schema":[{"schema_type":"FAQPage|MedicalWebPage|BreadcrumbList|Article|...","rationale":"","json_ld":{"@context":"https://schema.org","@type":""}}],
 "internal_links":[{"link_to_url":"must be one of CANDIDATES","anchor_text":"2-5 natural words","placement_context":"which section of OWN it fits"}],
 "confidence":"low|medium|high","missing_data":["what you could not see"]}
Max 6 recommendations, 4 content_gaps, 2 content_briefs, 3 schema items (only types OWN lacks; json_ld must use only facts visible in OWN text; FAQPage only if you can write answers from OWN text), 5 internal_links."""


def digest(p):
    return {k: p[k] for k in ("url", "title", "meta", "h1", "h2", "h3", "words", "images", "images_no_alt", "internal_links", "tables", "lists", "schema", "has_faq")} | {"text_start": p["text"][:2500]}


def audit(keyword, own_url, competitor_urls, candidates, niche="", ask=None):
    own = extract(own_url); comps = []; missing = []
    for u in competitor_urls:
        try: comps.append(extract(u))
        except Exception as e: missing.append(f"could not read competitor page {u} ({str(e)[:60]})")
    if not comps: raise RuntimeError("none of the competitor pages could be read; try another competitor URL")
    for p in [own] + comps: p["kw"] = kw_hits(p, keyword); p["score"] = score(p, keyword)
    msg = {"keyword": keyword, "niche": niche, "OWN": digest(own), "COMPETITORS": [digest(c) for c in comps], "CANDIDATES": candidates}
    t = ask(SYS, json.dumps(msg), 5000); d = json.loads(t[t.index("{"): t.rindex("}") + 1])
    allowed = {norm(c["url"]) for c in candidates}
    return {
        "scores": [{"url": p["url"], "own": p is own, "score": p["score"], "words": p["words"], "h2": len(p["h2"]), "faq": p["has_faq"], "schema": p["schema"], "kw": p["kw"]} for p in [own] + comps],
        "ranking_hypothesis": [{"statement": s} for s in d.get("hypothesis", [])],
        "recommendations": [{"page_url": own_url, "priority": r.get("priority", "medium"), "action": r.get("action", "")} for r in d.get("recommendations", [])],
        "content_briefs": [{**b, "target_page_urls": [own_url]} for b in d.get("content_briefs", [])],
        "confidence": d.get("confidence", "medium"), "missing_data": missing + d.get("missing_data", []),
        "audit": {"schema_analysis": [{"url": own_url, "schema": own["schema"]}],
                  "schema_recommendations": [{"url": own_url, "recommendations": d.get("schema", [])}],
                  "internal_link_suggestions": [{"url": own_url, "suggestions": [s for s in d.get("internal_links", []) if norm(s.get("link_to_url")) in allowed]}],
                  "page_comparison": {"content_gaps": [{**g, "missing_on": [own_url]} for g in d.get("content_gaps", [])]}}}
