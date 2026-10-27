"""Phase 1: turn imported competitor data into content ideas. Run: python -m seo_agent.ideas <site_id>"""
import os, sys, json, re
from urllib.parse import urlparse
from . import sb

SKIP_PATH = re.compile(r"/(category|tag|author|page|feed|wp-content|wp-json)(/|$)")
MAX_IDEAS = int(os.environ.get("IDEAS_PER_RUN", "15"))

SYS = """You are a content strategist for a website. You get (a) the site profile and (b) competitor pages and keywords that
bring them search traffic. Suggest NEW blog post ideas for OUR site that target the same searches with our own angle.
Rules:
- Each idea needs one primary_keyword taken from the data, 2-5 secondary_keywords from the data, a clear reader-friendly title
  (40-70 chars), a post_type (guide, explainer, cost, comparison, faq, symptoms, recovery, how-to, list), and intent
  (informational, commercial, transactional).
- source_url = the competitor page that inspired it (copy exactly from the data), or "" if it comes only from keywords.
- Never put a competitor name, doctor name or brand in any field. Never invent statistics, prices or claims.
- If the profile is medical, finance or legal: skip topics that need promises of results or before/after proof.
- Avoid ideas already listed in existing_keywords. Prefer lower keyword difficulty and higher volume.
- 'why' is one short plain sentence on why this topic is worth writing.
Reply with a JSON array only: [{"title":"","primary_keyword":"","secondary_keywords":[],"post_type":"","intent":"","source_url":"","why":""}]"""


def page_candidates(site_id):
    rows = sb.select("competitor_pages", site_id=f"eq.{site_id}", order="traffic.desc", limit="300")
    out = []
    for r in rows:
        path = urlparse(r["url"]).path.strip("/")
        if not path or SKIP_PATH.search("/" + path + "/"): continue
        out.append({"url": r["url"], "traffic": r["traffic"], "keywords": r["keywords_count"],
                    "top_keyword": r["top_keyword"], "top_kw_volume": r["top_kw_volume"]})
    return out[:40]


def keyword_candidates(site_id):
    rows = sb.select("competitor_keywords", site_id=f"eq.{site_id}", branded="eq.false", order="volume.desc", limit="400")
    out = []
    for r in rows:
        if r.get("kd") is not None and r["kd"] > 45: continue
        if r.get("position") is not None and r["position"] > 20: continue
        if not (r.get("informational") or r.get("commercial")): continue
        out.append({"keyword": r["keyword"], "volume": r["volume"], "kd": r["kd"], "position": r["position"], "url": r["url"]})
    return out[:60]


def clean(ideas, banned, names, existing):
    keep, seen = [], set(existing)
    for i in ideas:
        pk = (i.get("primary_keyword") or "").strip().lower(); title = (i.get("title") or "").strip()
        if not pk or not title or pk in seen: continue
        blob = (title + " " + pk + " " + " ".join(i.get("secondary_keywords") or [])).lower()
        if any(b.lower() in blob for b in banned if b): continue
        if any(g in blob for g in names): continue
        seen.add(pk); keep.append(i)
    return keep


def run(site_id):
    import anthropic
    site = (sb.select("sites", id=f"eq.{site_id}") or [None])[0]
    if not site: return print("unknown site", site_id)
    pages, kws = page_candidates(site_id), keyword_candidates(site_id)
    if not pages and not kws: return print("no competitor data imported yet")
    comps = {r["competitor"] for r in sb.select("competitor_pages", site_id=f"eq.{site_id}", select="competitor")}
    comps |= {r["competitor"] for r in sb.select("competitor_keywords", site_id=f"eq.{site_id}", select="competitor")}
    names = set()   # 7-letter pieces of each competitor host, so "dr molchugh" is caught too
    for c in comps:
        host = re.sub(r"[^a-z0-9]", "", c.lower().replace("www.", "").split(".")[0])
        names |= {host[i:i + 7] for i in range(max(1, len(host) - 6))} if len(host) >= 7 else ({host} if len(host) > 3 else set())
    existing = {r["primary_keyword"].lower() for r in sb.select("content_ideas", site_id=f"eq.{site_id}", select="primary_keyword")}
    msg = {"site": {"niche": site.get("niche"), "profile": site.get("profile"), "brand_voice": site.get("brand_voice"),
                    "banned_terms": site.get("banned_terms")},
           "competitor_pages": pages, "competitor_keywords": kws, "existing_keywords": sorted(existing)[:200], "how_many": MAX_IDEAS}
    client = anthropic.Anthropic()
    prompt = json.dumps(msg)
    r = client.messages.create(model=os.environ.get("SEO_MODEL", "claude-haiku-5-5"), max_tokens=12000, system=SYS, messages=[{"role": "user", "content": prompt}])
    t = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
    if "[" not in t or "]" not in t:
        return print("Claude did not return a list. stop_reason:", r.stop_reason, "| reply start:", t[:600] or "(empty)")
    try:
        raw = json.loads(t[t.index("["): t.rindex("]") + 1])
    except ValueError as e:
        return print("Could not read Claude's list:", e, "| stop_reason:", r.stop_reason, "| reply start:", t[:600])
    ideas = clean(raw, site.get("banned_terms") or [], names, existing)[:MAX_IDEAS]
    kwmap = {k["keyword"].lower(): k for k in kws}; pgmap = {p["url"]: p for p in pages}
    rows = []
    for i in ideas:
        k = kwmap.get(i["primary_keyword"].strip().lower()) or {}; p = pgmap.get(i.get("source_url") or "") or {}
        rows.append({"site_id": site_id, "title": i["title"][:200], "primary_keyword": i["primary_keyword"].strip(),
                     "secondary_keywords": [s for s in (i.get("secondary_keywords") or [])][:6], "post_type": i.get("post_type"),
                     "intent": i.get("intent"), "source_url": i.get("source_url") or None, "volume": k.get("volume") or p.get("top_kw_volume"),
                     "kd": k.get("kd"), "est_traffic": p.get("traffic"), "why": (i.get("why") or "")[:300], "status": "suggested"})
    if rows: sb.insert("content_ideas", rows)
    print("new ideas:", len(rows))


if __name__ == "__main__":
    run(sys.argv[1])
