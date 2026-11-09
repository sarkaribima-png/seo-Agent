"""Content briefs for NEW pages/posts: target keyword, intent, outline, questions, entities, internal links, schema type.
A brief is a plan for a writer (or for the content writer step). It is not published text and it changes nothing on a site.
Stored in table content_briefs (status draft -> approved / rejected / used). Medical sites: every brief is flagged for the doctor.
Run: python -m seo_agent.briefs <site_id> [limit]            briefs for ideas that have none yet
     python -m seo_agent.briefs <site_id> --keyword "gynecomastia recovery time"      one brief for a keyword you type"""
import os, re, sys, json, datetime as dt
from urllib.parse import urlparse
from . import sb, guard
from .humanize import lint, RISKY
from .onpage import copy_problems

MODEL = os.environ.get("BRIEF_MODEL") or os.environ.get("CONTENT_MODEL", "claude-sonnet-5-5")
DEFAULT_LIMIT = int(os.environ.get("BRIEFS_PER_RUN", "5"))
NEVER_SCHEMA = {"aggregaterating", "review", "rating", "offer", "aggregateoffer", "reviewrating"}
SCHEMA_OK = {"Article", "BlogPosting", "FAQPage", "HowTo", "MedicalWebPage", "MedicalProcedure", "MedicalCondition", "Service", "WebPage", "ItemList"}
INTENTS = {"informational", "commercial", "transactional", "navigational", "local"}
MEDICAL_CHECKLIST = ["who the procedure or advice is for (candidacy)", "risks and side effects", "recovery timeline and restrictions", "anaesthesia and pain statements", "any cost range or factor",
                     "any statement about results"]


def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def norm(u): return (u or "").split("#")[0].rstrip("/").lower().replace("://www.", "://")
def host(u): return urlparse(u).netloc.lower().replace("www.", "")
def toks(s): return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(t) > 2}


def ask(system, user, max_tokens=5000):
    import anthropic
    r = anthropic.Anthropic().messages.create(model=MODEL, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")


def candidates(site_id, limit=150):
    """Our own pages that a brief may link to: URL plus a short topic from the audit (title, first heading or the URL words)."""
    seen, out = set(), []
    for r in sb.select("page_audits", site_id=f"eq.{site_id}", select="page,raw", order="audited_at.desc", limit="600"):
        k = norm(r["page"])
        if k in seen or urlparse(r["page"]).path.strip("/") == "": continue
        seen.add(k); raw = r.get("raw") or {}; hs = raw.get("headings") or []
        topic = (raw.get("title") or (hs[0]["text"] if hs and isinstance(hs[0], dict) else "") or urlparse(r["page"]).path.strip("/").replace("-", " ").replace("/", " ")).strip()
        out.append({"url": r["page"], "topic": topic[:140]})
        if len(out) >= limit: break
    return out


def overlap(keyword, cands):
    """Pages that probably already target this keyword (all keyword words appear in the page's URL words or topic). Used to stop two pages competing."""
    k = toks(keyword)
    if not k: return []
    return [c for c in cands if k <= toks(c["topic"] + " " + urlparse(c["url"]).path.replace("-", " "))][:3]


SYS = """You are a senior SEO content strategist. Write a CONTENT BRIEF for a writer. You are not writing the page.
Use only facts in the input. Never invent statistics, prices, studies, awards, patient stories, rankings or author claims. Never name competitors. Never plan Review or AggregateRating schema.
Be useful to the reader first: the outline must answer what a real person searching this keyword wants to know, in the order they need it.
Where the topic is medical, legal or financial, do not state medical facts yourself: list what the page must cover and put every claim that needs a professional in 'claims_to_confirm'.
For E-E-A-T list only what a real person must supply (own experience, named author and reviewer, sources of a given TYPE). Never write a fake bio, quote or number.
Name the TYPE of source to cite (for example 'a national clinical guideline', 'a peer reviewed review'), never a specific paper or URL.
internal_links: link_to_url MUST be one of CANDIDATES (copy exactly). anchor_text 2 to 6 words that fit a sentence.
Reply JSON only, exactly this shape:
{"target_keyword":"","secondary_keywords":["3-8 close variants or related searches, plain phrases"],"intent":"informational|commercial|transactional|local",
 "page_type":"guide|explainer|cost|comparison|faq|symptoms|recovery|how-to|service|list","working_title":"30-60 chars","h1":"","meta_description_draft":"110-155 chars, no claims",
 "outline":[{"h2":"","h3":["optional sub points"],"must_cover":"one plain sentence on what this section answers"}],
 "questions":["real questions the page must answer, as a reader would type them"],"entities":["terms, conditions, procedures, places the page should mention for completeness"],
 "internal_links":[{"link_to_url":"","anchor_text":"","why":""}],"schema_type":"Article|BlogPosting|MedicalWebPage|FAQPage|HowTo|Service|MedicalProcedure","schema_notes":"which parts of the page the schema must match (it must match visible text)",
 "eeat":[{"need":"what real input is needed","owner":"author|doctor|editor","why":""}],"source_types":["kind of source to cite"],"claims_to_confirm":["topic-level statements a professional must confirm"],
 "avoid":["what the page must not say"],"length_note":"suggested depth in words, only as a range, or DATA NOT AVAILABLE","confidence":"high|medium|low","missing_data":["what you could not see"]}
Outline: 4 to 9 h2 sections. Questions: 4 to 10. Entities: 5 to 20. Internal links: 2 to 6."""


def validate(b, site, cands):
    """Returns (clean_brief, problems). Problems are plain sentences sent back to Claude on the retry."""
    p = []; med = site.get("profile") == "medical"; banned = list(site.get("banned_terms") or []) + list(site.get("competitor_names") or [])
    if not isinstance(b, dict): return None, ["not a JSON object"]
    b["intent"] = str(b.get("intent") or "").lower()
    if b["intent"] not in INTENTS: p.append("intent must be informational, commercial, transactional or local")
    title, meta = str(b.get("working_title") or "").strip(), str(b.get("meta_description_draft") or "").strip()
    p += guard.check_seo(title, meta, site)
    p += copy_problems(f"{title} {b.get('h1') or ''}", meta, site)   # 'best' / 'top' claims, prices, risky words
    p += lint(f"<p>{title}</p><p>{meta}</p>", banned, med)
    ol = b.get("outline") if isinstance(b.get("outline"), list) else []
    if not 4 <= len(ol) <= 9 or not all(isinstance(x, dict) and str(x.get("h2") or "").strip() for x in ol): p.append("outline needs 4 to 9 sections, each with an h2")
    qs = [str(q).strip() for q in (b.get("questions") or []) if str(q).strip()]
    if not 4 <= len(qs) <= 10: p.append("questions: 4 to 10 needed")
    ents = [str(e).strip() for e in (b.get("entities") or []) if str(e).strip()]
    if not 5 <= len(ents) <= 20: p.append("entities: 5 to 20 needed")
    st = str(b.get("schema_type") or "")
    if st not in SCHEMA_OK or st.lower() in NEVER_SCHEMA: p.append("schema_type must be one of " + ", ".join(sorted(SCHEMA_OK)))
    allowed = {norm(c["url"]) for c in cands}; links, dropped = [], 0
    for l in b.get("internal_links") or []:
        u = l.get("link_to_url", "") if isinstance(l, dict) else ""
        a = re.sub(r"\s+", " ", str(l.get("anchor_text") or "")).strip() if isinstance(l, dict) else ""
        if norm(u) in allowed and 2 <= len(a.split()) <= 6: links.append({"link_to_url": u, "anchor_text": a, "why": str(l.get("why") or "")[:200]})
        else: dropped += 1
    if len(cands) >= 2 and len(links) < 2: p.append("internal_links must use URLs copied exactly from CANDIDATES and 2 to 6 word anchors")
    blob = json.dumps({k: b.get(k) for k in ("working_title", "h1", "meta_description_draft", "outline", "questions", "eeat")}, ensure_ascii=False).lower()
    for t in banned + (RISKY if med else []):
        if t and re.search(r"(?<![a-z0-9])" + re.escape(t.lower()) + r"(?![a-z0-9])", blob):
            p.append(f"contains banned or risky term '{t}'")
    if re.search(r"\b\d{2,3}\s?%|₹\s?\d|\brs\.?\s?\d", json.dumps(b.get("outline") or [], ensure_ascii=False).lower()): p.append("outline contains a number, price or percentage that is not from the input")
    if p: return None, list(dict.fromkeys(p))[:10]
    b["internal_links"] = links; b["questions"] = qs; b["entities"] = ents; b["working_title"] = title; b["meta_description_draft"] = meta
    return b, []


def make_brief(site, keyword, idea, cands, askf, brain=None):
    """Returns (brief_dict, None) or (None, reason)."""
    keyword = (keyword or "").strip()
    if not keyword: return None, "no keyword"
    same = overlap(keyword, cands); fb = ""; med = site.get("profile") == "medical"
    for _ in range(3):
        msg = {"keyword": keyword, "idea": {k: idea.get(k) for k in ("title", "secondary_keywords", "post_type", "intent", "why")} if idea else None,
               "site": {"niche": site.get("niche"), "profile": site.get("profile"), "brand_voice": site.get("brand_voice"), "banned_terms": site.get("banned_terms")},
               "CANDIDATES": cands[:80], "pages_that_may_already_cover_this": same, "brain": brain or {}, "previous_errors": fb}
        t = askf(SYS, json.dumps(msg, ensure_ascii=False), 5000)
        try: d = json.loads(t[t.index("{"): t.rindex("}") + 1])
        except ValueError: fb = "return JSON only"; continue
        b, probs = validate(d, site, cands)
        if b is not None:
            b["target_keyword"] = keyword
            b["cannibalization"] = same        # existing pages that may already target this keyword: update one of them instead of publishing a second page
            b["recommendation"] = (f"A page on this topic may already exist ({same[0]['url']}). Improve that page instead of publishing a second one, unless the intent is clearly different." if same
                                   else "New page.")
            if med:
                b["ymyl"] = {"flag": True, "doctor_to_confirm": list(dict.fromkeys(MEDICAL_CHECKLIST + [str(x) for x in (b.get("claims_to_confirm") or [])][:12])),
                             "note": "Medical topic. A doctor must read the finished page before it goes live. Do not publish numbers, timelines or results unless the doctor has confirmed them."}
            b["serp_notes"] = "DATA NOT AVAILABLE (briefs are built from the keyword, the site's own pages and the site brain; run Rank a page for a competitor comparison)"
            return b, None
        fb = "; ".join(probs)
    return None, "brief failed checks: " + fb[:300]


def run(site_id, limit=None, keyword=None):
    from . import run as runmod
    cfg = runmod.sites()[site_id]; row = (sb.select("sites", id=f"eq.{site_id}") or [{}])[0]; site = {**cfg, **{k: v for k, v in row.items() if v not in (None, [], "")}}
    if site.get("mode") == "paused": return print("site paused from dashboard")
    cands = candidates(site_id)
    try:
        from .content import brain_context
        brain = brain_context(site_id, site.get("profile"))
    except Exception as e: brain = {}; print("note: site brain not read:", str(e)[:80])
    if keyword: todo = [(None, keyword)]
    else:
        done = {r["idea_id"] for r in sb.select("content_briefs", site_id=f"eq.{site_id}", select="idea_id") if r.get("idea_id")}
        ideas = sb.select("content_ideas", site_id=f"eq.{site_id}", status="in.(approved,suggested)", order="created_at.desc", limit="100")
        todo = [(i, i["primary_keyword"]) for i in ideas if i["id"] not in done][: int(limit or DEFAULT_LIMIT)]
    if not todo: return print("no ideas without a brief")
    made = 0
    for idea, kw in todo:
        b, err = make_brief(site, kw, idea, cands, ask, brain)
        if not b: print("no brief for", kw, "-", err); continue
        sb.insert("content_briefs", [{"site_id": site_id, "idea_id": (idea or {}).get("id"), "keyword": kw, "intent": b["intent"], "page_type": b.get("page_type"),
                                      "schema_type": b["schema_type"], "ymyl": bool(b.get("ymyl")), "brief": b, "status": "draft", "target_url": (b["cannibalization"][0]["url"] if b["cannibalization"] else None)}])
        made += 1
    print("briefs created:", made)


if __name__ == "__main__":
    from . import run as _r
    a = sys.argv[1:]; kw = None
    if "--keyword" in a: i = a.index("--keyword"); kw = " ".join(a[i + 1:]); a = a[:i]
    lim = int(a[1]) if len(a) > 1 and a[1].isdigit() else None
    _r.each(a[0], lambda sid: run(sid, lim, kw), kinds=("wordpress", "static", "html"))
