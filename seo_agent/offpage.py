"""Off-page & Local SEO planner. PLANS and DRAFTS only: it never posts, emails, asks for reviews or contacts anyone. A person copies a draft and does it.
Run: python -m seo_agent.offpage <site_id|all> [--print] [--no-nap]      Prompt/rules: agents/offpage.md
Writes: offpage_tasks (max 5 new tasks per site per ISO week). backlink_log is filled by people in the dashboard (/offpage).
Flow: read site facts (niche, location, money pages, tracked keywords, GSC) -> candidate tasks scored in plain code -> top 5 -> drafts (Claude via llm.ask_json, template fallback)
 -> every draft goes through check_text() (the guidelines check) -> rejected drafts fall back to the template; if that fails too the task is dropped.
Missing data is written as DATA NOT AVAILABLE, never guessed. Medical sites: every publishable task has needs_reviewer=true; the DB refuses status 'done' until reviewed_by is set."""
import os, re, sys, json, html, datetime as dt
from collections import Counter
from urllib.parse import urlparse, urljoin
from . import sb

NA = "DATA NOT AVAILABLE"
MAX_TASKS = 5                                              # per site per ISO week, existing tasks of that week included
TYPE_CAP = {"gbp_post": 1, "review_request": 1, "citation": 1, "outreach": 2, "guest_post": 1, "pr": 1}
DUE_DAYS = {"gbp_post": 3, "review_request": 5, "citation": 7, "outreach": 6, "guest_post": 7, "pr": 7}
COOLDOWN = {"citation": 60, "outreach": 60, "guest_post": 45, "pr": 30, "review_request": 25, "gbp_post": 5}   # days before the same kind of task may be planned again
NEEDS_REVIEW = {"gbp_post", "review_request", "outreach", "guest_post", "pr"}   # citation = facts only (NAP), no reviewer needed
MEDICAL = re.compile(r"(?i)medical|clinic|doctor|surg|derma|dental|health|hospital|plastic|gyne|cosmetic|hair transplant")
UA = {"User-Agent": "Mozilla/5.0 (compatible; SEOAgentOffpage/1.0; read-only)"}

# ---------------------------------------------------------------- guidelines check (rejects spam patterns)
RULES = [
 ("paid_link", r"paid (?:link|post|guest)|buy(?:ing)? (?:a )?(?:back)?links?\b|backlinks? (?:for sale|package|packages)|sponsored (?:post|link)s?\b|pay(?:ing|ment)? (?:you )?(?:for|per) (?:a )?(?:link|backlink|placement|post)|\$\s?\d+\s*(?:per|for) (?:a )?(?:link|post)|(?:rs\.?|inr|₹)\s?\d+\s*(?:per|for) (?:a )?(?:link|post)|link (?:exchange|swap|building (?:package|service))|reciprocal link|link back (?:to you )?if|you link to (?:us|me)|\bpbn\b|private blog network|do-?follow|link farm|guest post(?:ing)? (?:fee|price|rates?)"),
 ("guarantee", r"guarantee[ds]?\b|100\s?%|assured (?:result|ranking)|risk[- ]free|no risk\b|promise (?:you )?(?:results|rank)"),
 ("ranking_claim", r"\brank(?:ing)?s?\b.{0,25}\b(?:#?1|first|top|higher|improve|boost)|(?:improve|boost|increase|raise)\s+(?:your\s+)?(?:search\s+)?rank(?:ing)?s?|first page|top of google|number one|#\s?1\b|top[- ]ranked|page one"),
 ("before_after", r"before\s*(?:and|&|/|-|vs\.?)\s*after|before-after|transformation results?"),
 ("fake_or_incentivised_review", r"\b(?:5|five)[- ]?stars?\b|positive (?:review|rating|feedback)|(?:good|great|glowing|favou?rable) (?:review|rating)|(?:discount|voucher|coupon|reward|cash|incentive|prize|gift|complimentary|free (?:consultation|treatment|session|gift|product|service))\b.{0,40}\b(?:review|rating)|\b(?:review|rating)\b.{0,40}\b(?:discount|voucher|coupon|reward|cash|incentive|prize|gift|complimentary|free (?:consultation|treatment|session|gift|product|service))|in exchange for (?:a |your )?(?:review|rating|feedback)|rate us (?:\d|high)|give us (?:a |an )?(?:\d|high|top)|we(?:'ll| will) write (?:it|the review)|write (?:the|a) review for you"),
 ("medical_claim", r"\bcures?\b|miracle|painless|no side[- ]effects?|permanent results?|safe for everyone"),
 ("superlative", r"\bbest (?:doctor|surgeon|clinic|hospital|dentist|dermatologist|agency|company)\b|\b(?:better|cheaper|superior) than\b|\bunlike (?:other|our competitors)\b"),
 ("pressure", r"!!|act now|limited time|urgent(?:ly)? (?:reply|respond)"),
]
_RX = [(c, re.compile(p, re.I)) for c, p in RULES]
STOP = set("this that with from your have will they their about what when where which there been were would could should also into than then them these those some more most such only very over just like need make help team please thank thanks hello regards best".split())

def check_text(text, names=()):
    """Guidelines check. Returns a list of 'code: matched text'; empty list = OK. names = competitor / banned names that must never appear."""
    t = html.unescape(str(text or "")); bad = []
    for c, rx in _RX:
        m = rx.search(t)
        if m: bad.append(f"{c}: {m.group(0)[:50].strip()}")
    for n in names or ():
        n = str(n or "").strip()
        if len(n) >= 3 and re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", t, re.I): bad.append(f"competitor_or_banned_name: {n}")
    words = [w for w in re.findall(r"[a-z]{4,}", t.lower()) if w not in STOP]
    if len(words) >= 40:
        w, n = Counter(words).most_common(1)[0]
        if n >= 6 and n / len(words) > 0.08: bad.append(f"keyword_stuffing: '{w}' x{n}")
    if len(re.findall(r"https?://", t)) > 3: bad.append("too_many_links: more than 3 URLs in one draft")
    return bad

def site_names(site): return [x for x in list(site.get("competitor_names") or []) + list(site.get("banned_terms") or []) if x]
def check_task(task, site): return check_text(f"{task.get('title') or ''}\n{task.get('draft') or ''}", site_names(site))
def invented_numbers(draft, facts):
    """Numbers in a draft that are not in the facts we gave the model (e.g. '15 years of experience')."""
    known = set(re.findall(r"\d+", json.dumps(facts, ensure_ascii=False)))
    return sorted({n for n in re.findall(r"\d+", str(draft or "")) if n not in known})
def is_medical(site): return bool(site.get("reviewer_required") or site.get("profile") == "medical" or MEDICAL.search(" ".join(str(site.get(k) or "") for k in ("niche", "profile"))))
def can_mark_done(task): return not (task.get("needs_reviewer") and not task.get("reviewed_by"))   # same rule as the DB constraint offpage_reviewer_gate

# ---------------------------------------------------------------- reading site data
def q(table, **params):
    try: return sb.select(table, **params)
    except Exception as e: q.errors[table] = str(e)[:100]; return None
q.errors = {}
def week_of(d): y, w, _ = d.isocalendar(); return f"{y}-W{w:02d}"
def as_list(v): return [x.strip() for x in v.split(",") if x.strip()] if isinstance(v, str) else list(v or [])
def path(u): return urlparse(u).path or "/"

def gather(site_id, site, today):
    q.errors = {}; since = (today - dt.timedelta(days=30)).isoformat(); st = {"site": site}
    st["gsc"] = q("gsc_daily", site_id=f"eq.{site_id}", date=f"gte.{since}", select="page,impressions", limit="5000")
    st["tracked"] = q("tracked_keywords", site_id=f"eq.{site_id}", active="eq.true", select="keyword", limit="500")
    st["rank"] = q("rank_daily", site_id=f"eq.{site_id}", day=f"gte.{(today - dt.timedelta(days=12)).isoformat()}", select="keyword,position,impressions", limit="3000")
    st["tasks"] = q("offpage_tasks", site_id=f"eq.{site_id}", select="week,type,status,dedupe_key,created_at", order="created_at.desc", limit="500") or []
    return st

def top_pages(st, n=5):
    mp = as_list(st["site"].get("money_pages"))
    if mp: return [(u, None, "sites.money_pages") for u in mp[:n]]
    agg = Counter()
    for r in st.get("gsc") or []: agg[r["page"]] += r.get("impressions") or 0
    return [(u, i, "gsc_daily impressions (30d)") for u, i in agg.most_common(n)]

def keywords(st):
    """Tracked keywords; striking-distance ones (average position 8-20 in the last 12 days) first."""
    pos = {}
    for r in st.get("rank") or []:
        if r.get("position") is not None: pos.setdefault(r["keyword"], []).append(r["position"])
    near = sorted((k for k, v in pos.items() if 8 <= sum(v) / len(v) <= 20), key=lambda k: sum(pos[k]) / len(pos[k]))
    rest = [r["keyword"] for r in st.get("tracked") or [] if r["keyword"] not in near]
    return near + rest, set(near)

# ---------------------------------------------------------------- NAP / citation check (public pages only)
def http_fetch(url):
    import requests
    try: r = requests.get(url, headers=UA, timeout=20); return r.status_code, r.text
    except Exception: return 0, ""

def _digits(p):
    d = re.sub(r"\D", "", p or ""); return d[-10:] if len(d) >= 10 else d
def _norm(s): return re.sub(r"\b(road|rd|street|st|lane|ln|floor|flr|near|opp|opposite|the|and)\b", "", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()))
def _tok(s): return set(_norm(s).split())

def extract_nap(htm):
    from bs4 import BeautifulSoup
    s = BeautifulSoup(htm or "", "html.parser"); phones, addr, names = set(), set(), set()
    for a in s.select('a[href^="tel:"]'):
        d = _digits(a["href"][4:]); phones.add(d) if len(d) >= 8 else None
    for sc in s.find_all("script", type="application/ld+json"):
        try: data = json.loads(sc.string or "")
        except ValueError: continue
        for o in (data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []):
            if not isinstance(o, dict): continue
            if o.get("telephone"): phones.add(_digits(str(o["telephone"])))
            a = o.get("address")
            if isinstance(a, dict) and a.get("streetAddress"): addr.add(" ".join(str(a.get(k) or "") for k in ("streetAddress", "addressLocality", "postalCode")).strip())
            if o.get("name") and re.search(r"(?i)business|clinic|physician|dentist|hospital|organization|store|medical|practice", json.dumps(o.get("@type"))): names.add(str(o["name"]).strip())
    for a in s.find_all("address"):
        t = " ".join(a.get_text(" ").split())
        if len(t) > 10: addr.add(t)
    for tag in s.select('[itemprop="streetAddress"]'):
        t = " ".join(tag.get_text(" ").split())
        if t: addr.add(t)
    og = s.find("meta", property="og:site_name")
    if og and og.get("content"): names.add(og["content"].strip())
    for line in s.get_text("\n").splitlines():
        if re.search(r"(?i)\b(?:phone|call|tel|mobile|whatsapp)\b", line):
            for m in re.findall(r"\+?\d[\d\s().-]{8,16}\d", line):
                d = _digits(m)
                if len(d) >= 10: phones.add(d)
    return {"phones": sorted(p for p in phones if p), "addresses": sorted(addr), "names": sorted(names)}

def nap_check(base, site, fetch=http_fetch, extra_pages=()):
    """Compare phone / address / name across public pages of the site. Status: 'consistent' | 'inconsistent' | NA."""
    cands = [base + "/"] + [urljoin(base + "/", p) for p in ("contact", "contact-us", "about", "about-us")] + list(extra_pages)[:2]
    pages = []
    for u in dict.fromkeys(cands):
        code, htm = fetch(u)
        if code == 200 and htm: pages.append({"url": u, **extract_nap(htm)})
    found = [p for p in pages if p["phones"] or p["addresses"] or p["names"]]
    out = {"pages": found, "checked": len(pages), "issues": [], "status": NA}
    if not found: return out
    ph = {x for p in found for x in p["phones"]}
    if len(ph) > 1: out["issues"].append("Different phone numbers on the site: " + ", ".join(sorted(ph)))
    ad = [(p["url"], a) for p in found for a in p["addresses"]]
    for i in range(len(ad)):
        for j in range(i + 1, len(ad)):
            a, b = _tok(ad[i][1]), _tok(ad[j][1])
            if a and b and len(a & b) / len(a | b) < 0.6: out["issues"].append(f"Address written differently: '{ad[i][1][:70]}' ({path(ad[i][0])}) vs '{ad[j][1][:70]}' ({path(ad[j][0])})"); break
        else: continue
        break
    nm = {_norm(n).strip() for p in found for n in p["names"] if n}
    if len(nm) > 1: out["issues"].append("Business name varies: " + " | ".join(sorted({n for p in found for n in p["names"]})))
    exp = site.get("nap") or {}
    if isinstance(exp, dict):
        if exp.get("phone") and ph and _digits(exp["phone"]) not in ph: out["issues"].append(f"Phone on site ({', '.join(sorted(ph))}) differs from the phone saved for this site ({exp['phone']})")
        if exp.get("address") and ad and not any(len(_tok(exp["address"]) & _tok(a)) / max(1, len(_tok(exp["address"]))) >= 0.6 for _, a in ad): out["issues"].append("Address on site does not match the address saved for this site")
    out["status"] = "inconsistent" if out["issues"] else "consistent"
    return out

def citation_draft(nap, site):
    loc = site.get("location") or NA
    lines = ["NAP / citation checklist (a person does this; nothing is submitted automatically)", f"Business: {site.get('name') or site.get('domain')}   Area: {loc}", ""]
    if nap["status"] == NA: lines.append(f"Name/Address/Phone on the website: {NA} (no phone, address or name found on {nap['checked']} public page(s) checked). Add them in text on the contact page first.")
    else:
        lines.append(f"Found on the website ({nap['checked']} pages checked): status = {nap['status']}")
        for p in nap["pages"]: lines.append(f"- {path(p['url'])}: name {', '.join(p['names']) or NA}; phone {', '.join(p['phones']) or NA}; address {'; '.join(a[:90] for a in p['addresses']) or NA}")
        lines += ["Problems to fix on the site:"] + ([f"- {i}" for i in nap["issues"]] or ["- none found"])
    lines += ["", "Then check, one by one, that name / address / phone match the website character for character (same spelling of Road/Rd, same phone format):",
              "[ ] Google Business Profile   [ ] Bing Places   [ ] Apple Business Connect   [ ] Facebook / Instagram bio   [ ] relevant local and industry directories you already use",
              "Fix wrong or duplicate listings by editing them. Do not pay for bulk listing packages. Note what you changed in this task's notes."]
    return "\n".join(lines)

# ---------------------------------------------------------------- template drafts (no-LLM fallback; no numbers, no claims)
def _ctx(site, topic=None, page=None):
    return {"brand": site.get("name") or site.get("domain") or NA, "loc": site.get("location") or "", "niche": site.get("niche") or "", "topic": topic or site.get("niche") or "our services",
            "page": page or ("https://" + site["domain"] if site.get("domain") else NA), "phone": (site.get("nap") or {}).get("phone") if isinstance(site.get("nap"), dict) else None}
def tpl_gbp(site, topic, page):
    c = _ctx(site, topic, page); med = is_medical(site); where = f" in {c['loc']}" if c["loc"] else ""
    t = (f"{c['topic'].capitalize()}{where}: what to know before you decide\n\nThinking about {c['topic']}{where}? At a first consultation we explain what is involved, who it may suit, what recovery or follow-up can look like for different people, and the questions worth asking any provider.\n"
         f"Learn more: {c['page']}\nTo ask about your own situation, contact {c['brand']}" + (f" on {c['phone']}." if c["phone"] else " (add phone / booking link before posting)."))
    return t + ("\n\nThis is general information, not medical advice. Individual results vary." if med else "")
def tpl_review(site):
    c = _ctx(site); link = site.get("gbp_review_link") or "[review link: a person copies it from the Google Business Profile dashboard]"
    return (f"Version 1 (WhatsApp / SMS, send only to people who actually visited, one message each):\nHi [first name], thank you for visiting {c['brand']}. If you have a couple of minutes, we would value your honest feedback on Google, whatever your experience was: {link}\nIt also helps us see what to improve. Thank you!\n\n"
            f"Version 2 (email):\nSubject: Your feedback on your visit to {c['brand']}\nHello [first name],\nThank you for choosing {c['brand']}. We are always trying to improve, and honest feedback from patients and customers is the best way to know how. If you would like to share yours, you can do it here: {link}\nIt is completely optional. Thank you for your time.\n\n"
            "Version 3 (at the desk, spoken): \"If you would like to share your experience on Google, we have a link on our card. It is entirely up to you.\"" +
            ("\n\nMedical site: do not ask for or reply with any health details in reviews; replies stay general." if is_medical(site) else ""))
def tpl_outreach(site, topic, page, publisher="[publisher / site name]"):
    c = _ctx(site, topic, page)
    return (f"Subject: A resource your readers might find useful\n\nHello [editor name],\n\nI read [a specific article of theirs: add title] on {publisher}. We have a page on {c['topic']} that may be useful to your readers: {c['page']}\n"
            f"If it fits, it could complement that article. If you think it is not a fit, no problem at all. We are not asking for anything in return and are not offering payment; link only if you find it useful.\n\nThank you for your time,\n[sender name], {c['brand']}")
def tpl_guest(site, topic, page):
    c = _ctx(site, topic, page)
    return (f"Subject: Article idea for [publication]: {c['topic']}\n\nHello [editor name],\n\nI follow [publication] and noticed [specific recent article: add title]. I would like to propose an article on {c['topic']} for your readers: [2-3 line outline written by the author].\n"
            f"It would be written fresh for you, not published elsewhere, and any mention of {c['brand']} would be limited to an author bio. If the topic does not suit, I am happy to adapt it or step aside. No payment is involved on either side.\n\nThank you,\n[author name], {c['brand']}")
def tpl_pr(site, topic, page):
    c = _ctx(site, topic, page); where = f" in {c['loc']}" if c["loc"] else ""
    return (f"Subject: Local story idea{where}: [news hook]\n\nHello [journalist name],\n\nStory angle: [NEWS HOOK: {NA}. The owner must supply something genuinely new, e.g. a community event, health camp or awareness drive that is really happening.]\n"
            f"Who: {c['brand']}{where}. Background on {c['topic']}: {c['page']}\nA [doctor / owner name] is available for comment on [date]. Facts and figures will be shared only if they can be verified.\n\nThank you,\n[sender name]")

# ---------------------------------------------------------------- candidates and selection
def cand(type_, key, title, target, prio, evidence, conf, draft, facts, signals):
    return {"type": type_, "dedupe_key": key, "title": title, "target": target, "prio": prio, "evidence": evidence, "confidence": conf, "draft": draft, "facts": facts, "signals": signals}

def candidates(site, st, today, nap=None):
    """All possible tasks with a score. Pure function (the NAP result is passed in). Returns (cands, gaps)."""
    gaps, out = [], []; wk = week_of(today); loc = site.get("location"); kws, near = keywords(st); pages = top_pages(st)
    if not loc: gaps.append(f"location: {NA} (set sites.location to get Google Business Profile, review and citation tasks)")
    if st.get("gsc") is None and not as_list(site.get("money_pages")): gaps.append(f"gsc_daily/money_pages: {NA}")
    if st.get("tracked") is None: gaps.append(f"tracked_keywords: {NA}")
    page0 = pages[0][0] if pages else None
    if loc:
        topic = (kws[today.isocalendar()[1] % len(kws)] if kws else site.get("niche")) or NA
        tpage = next((u for u, _, _ in pages if topic and any(w in u.lower() for w in re.findall(r"[a-z]{4,}", topic.lower()))), page0)
        ev = f"sites.location={loc}; topic from " + ("tracked_keywords" if kws else "sites.niche") + (f"; page {path(tpage)} from {pages[0][2]}" if tpage else f"; page: {NA}")
        out.append(cand("gbp_post", f"gbp:{wk}", f"Google Business Profile post: {topic}", "Google Business Profile > Add update", 80, ev, "Medium" if kws else "Low", tpl_gbp(site, topic, tpage),
                        {"topic": topic, "page": tpage, "location": loc}, {"rule": "weekly GBP post", "topic": topic}))
        out.append(cand("review_request", f"review:{today.strftime('%Y-%m')}", "Ask recent customers for honest Google reviews", "Recent customers (a person chooses who)", 70,
                        f"sites.location={loc}; review link " + ("saved" if site.get("gbp_review_link") else f"{NA} (paste it in sites.gbp_review_link)"), "Medium", tpl_review(site), {"review_link_known": bool(site.get("gbp_review_link"))}, {"rule": "monthly review ask"}))
    if nap is not None or loc:
        bad = nap and nap["status"] in ("inconsistent", NA)
        ev = "public pages fetched: " + (f"{nap['checked']} page(s), status {nap['status']}" if nap else f"not fetched ({NA})")
        out.append(cand("citation", "nap-audit", "NAP / citation consistency check" + (f" ({len(nap['issues'])} issue(s) on the site)" if nap and nap["issues"] else ""), "Website + Google Business Profile + directories",
                        95 if nap and nap["issues"] else 75 if bad else 55, ev, ("High" if nap and nap["status"] != NA and len(nap["pages"]) > 1 else "Medium" if nap and nap["status"] != NA else "Low"),
                        citation_draft(nap, site) if nap else tpl_gbp_nonap(site), {"nap_status": nap["status"] if nap else NA}, {"rule": "NAP audit", "nap_status": nap["status"] if nap else NA}))
    for u, imp, src in pages[:3]:
        topic = kws[0] if kws else site.get("niche") or NA
        out.append(cand("outreach", f"outreach:{path(u)}", f"Outreach: ask a relevant publisher to consider {path(u)}", f"{NA}: a person picks a relevant publisher (no prospect list exists)", 60 + min(15, int((imp or 0) ** 0.5 / 4)),
                        f"{src}: {path(u)}" + (f" {imp} impressions" if imp is not None else ""), "Medium", tpl_outreach(site, site.get("niche") or topic, u), {"topic": site.get("niche"), "page": u}, {"rule": "outreach for a money page", "page": u, "impressions": imp}))
    for k in kws[:2]:
        out.append(cand("guest_post", f"guest:{k}", f"Guest article pitch: {k}", f"{NA}: a person picks a relevant publication", 65 if k in near else 45,
                        f"tracked_keywords '{k}'" + (" (average position 8-20: striking distance)" if k in near else ""), "Medium" if k in near else "Low", tpl_guest(site, k, page0), {"topic": k, "page": page0}, {"rule": "guest pitch", "keyword": k, "striking": k in near}))
    if loc: out.append(cand("pr", f"pr:{today.strftime('%Y-%m')}", "Local PR pitch (only if the owner has a real news hook)", f"{NA}: a person picks a local outlet", 35, f"sites.location={loc}; news hook {NA}", "Low", tpl_pr(site, site.get("niche"), page0), {"niche": site.get("niche"), "page": page0}, {"rule": "local PR"}))
    return out, gaps
def tpl_gbp_nonap(site): return f"NAP check was not run. Run: python -m seo_agent.offpage {site.get('id')} (without --no-nap) or check the contact page by hand."

def select_tasks(cs, st, today):
    """Max MAX_TASKS new tasks per site per week, counting tasks already planned this week; skip anything inside its cooldown or already open."""
    wk, have = week_of(today), st.get("tasks") or []
    room = MAX_TASKS - sum(1 for t in have if t["week"] == wk and t["status"] != "skipped"); chosen, per = [], Counter(t["type"] for t in have if t["week"] == wk and t["status"] != "skipped")
    def blocked(c):
        for t in have:
            if t["dedupe_key"] != c["dedupe_key"] or t["type"] != c["type"]: continue
            if t["status"] in ("todo", "doing"): return True
            try: age = (today - dt.date.fromisoformat(str(t["created_at"])[:10])).days
            except ValueError: age = 0
            if age < COOLDOWN.get(c["type"], 30) and t["status"] in ("done", "skipped", "todo", "doing"): return True
        return False
    for c in sorted(cs, key=lambda c: -c["prio"]):
        if len(chosen) >= max(0, room): break
        if per[c["type"]] >= TYPE_CAP.get(c["type"], 1) or blocked(c): continue
        chosen.append(c); per[c["type"]] += 1
    return chosen

# ---------------------------------------------------------------- drafting (Claude, with template fallback)
SYSTEM = """You write DRAFTS for an off-page / local SEO planner. A human reviews and sends them; you never contact anyone.
Rules: use ONLY the facts given. Invent no numbers, years, awards, prices, names, results or reviews; where a fact is missing write a [placeholder]. No guarantees, no ranking promises, no before/after claims,
no competitor names, no paid links or link exchange (say plainly no payment is involved), no fake or incentivised reviews and never tell anyone what rating to give (ask for honest feedback only).
Google Business Profile posts: informative, one clear next step, no stuffing of keywords. Outreach/guest: short, polite, honest value for the publisher's readers, easy to decline. Medical sites: general information only, no outcome promises.
Reply with JSON only: {"drafts":[{"id":"<task id>","title":"...","draft":"..."}]}"""

def write_drafts(site, chosen, ask_json=None):
    """Fill c['draft'] for each chosen task. Returns number of LLM drafts that passed the check. Template is kept when the LLM is unavailable or its text fails the check."""
    names = site_names(site)
    for i, c in enumerate(chosen): c["_id"] = str(i)
    drafts = {}
    if ask_json is None:
        from . import llm
        def ask_json(system, user, **kw): return llm.ask_json(system, user, **kw)
    llmx = {"unavailable": False}
    try:
        from .llm import LLMUnavailable
    except Exception: LLMUnavailable = RuntimeError
    todo = [c for c in chosen if c["type"] != "citation"]       # the citation checklist is built from fetched facts, not written by the model
    if todo:
        try:
            payload = {"site": {"brand": site.get("name") or site.get("domain"), "niche": site.get("niche"), "location": site.get("location"), "medical": is_medical(site), "profile": site.get("profile")},
                       "tasks": [{"id": c["_id"], "type": c["type"], "facts": c["facts"], "template": c["draft"]} for c in todo]}
            r = ask_json(SYSTEM, payload, agent="offpage", site_id=site.get("id"), max_tokens=3500)
            for d in (r.get("drafts") if isinstance(r, dict) else r) or []:
                if isinstance(d, dict) and isinstance(d.get("draft"), str) and str(d.get("id")) in {c["_id"] for c in todo}: drafts[str(d["id"])] = d
        except LLMUnavailable as e: llmx["unavailable"] = True; print("note: Claude not available, template drafts used:", str(e)[:80])
        except Exception as e: llmx["unavailable"] = True; print("note: Claude reply unusable, template drafts used:", str(e)[:100])
    ok = 0
    for c in chosen:
        c["llm_used"] = False; c["notes"] = ""; d = drafts.get(c["_id"])
        if d:
            probe = {"title": d.get("title") or c["title"], "draft": d["draft"]}; why = check_task(probe, site) + [f"invented_number: {n}" for n in invented_numbers(d["draft"], {**c["facts"], "t": c["draft"]})]
            if why: c["notes"] = "Claude draft rejected by guidelines check (" + "; ".join(why[:3]) + "), template used."
            else: c["draft"], c["llm_used"] = d["draft"], True; c["title"] = str(d.get("title") or c["title"])[:160]; ok += 1
        c["notes"] = (c["notes"] + " " if c["notes"] else "") + ("Template draft (no Claude)." if not c["llm_used"] and c["type"] != "citation" else "")
    return ok

def finalize(site, chosen, today, wk):
    """Guidelines check on the final text (LLM or template); build DB rows. Tasks that still fail are dropped, not posted."""
    med, rows, dropped = is_medical(site), [], []
    for c in chosen:
        why = check_task(c, site)
        if why: dropped.append((c["title"], why)); continue
        rev = med and c["type"] in NEEDS_REVIEW
        note = (c["notes"] + (" Medical site: reviewer must OK this text (record the name on the board) before it is posted or sent." if rev else "") + " A person does this; the agent never posts or contacts anyone.").strip()
        rows.append({"site_id": site["id"], "week": wk, "type": c["type"], "title": c["title"][:200], "draft": c["draft"], "target": c["target"], "status": "todo", "due": (today + dt.timedelta(days=DUE_DAYS[c["type"]])).isoformat(),
                     "notes": note, "evidence": c["evidence"], "confidence": c["confidence"], "needs_reviewer": rev, "llm_used": c["llm_used"], "dedupe_key": c["dedupe_key"],
                     "decision": {"score": c["prio"], **c["signals"], "planned": today.isoformat()}})
    return rows, dropped

def run(site_id, today=None, write=True, ask_json=None, fetch=http_fetch, do_nap=True, site=None):
    from . import run as R
    site = site or R.sites()[site_id]; site = {"id": site_id, **site}; today = today or dt.date.today(); wk = week_of(today)
    st = gather(site_id, site, today); base = (site.get("wp_base") or ("https://" + site["domain"] if site.get("domain") else "")).rstrip("/")
    nap = nap_check(base, site, fetch, [u for u, _, _ in top_pages(st, 2)]) if do_nap and base else None
    cs, gaps = candidates(site, st, today, nap); chosen = select_tasks(cs, st, today); n_llm = write_drafts(site, chosen, ask_json)
    rows, dropped = finalize(site, chosen, today, wk)
    for g in gaps: print("note:", g)
    for t, w in dropped: print("DROPPED (guidelines):", t, w)
    print(f"{site_id} {wk}: {len(rows)} task(s) planned ({n_llm} Claude drafts)" + (" [print only]" if not write else ""))
    for r in rows: print(f"- [{r['type']}] {r['title']} (confidence {r['confidence']}{', reviewer needed' if r['needs_reviewer'] else ''})")
    if write and rows: sb.insert("offpage_tasks", rows)
    return {"rows": rows, "gaps": gaps, "dropped": dropped, "nap": nap}

if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if not a: raise SystemExit("usage: python -m seo_agent.offpage <site_id|all> [--print] [--no-nap]")
    from . import run as _r
    _r.each(a[0], lambda sid: run(sid, write="--print" not in sys.argv, do_nap="--no-nap" not in sys.argv))
