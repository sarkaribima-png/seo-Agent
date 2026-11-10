"""Technical SEO Fixer: turns open rows of tech_issues (written by tech_audit.py) into exact, checkable fixes.
For each issue: classify SAFE / RISKY (by issue code, in code, never by Claude), ask Claude for the exact fix (strict JSON), validate it, fall back to a rule for the simple cases,
store it as a proposal (kinds the repo already uses: title_meta, alt_text; everything else is the new kind 'tech_fix'), and log it in tech_fixes.
After a change is applied the live URL is fetched again; only a verified fix marks the tech_issues row 'fixed'.
Run: python -m seo_agent.technical_fixer <site_id|all> [--max N] [--dry]       env: FIXER_MAX (default 10), FIXER_VERIFY_TRIES (3), FIXER_VERIFY_WAIT (5 s), FIXER_LLM_SIMPLE=0 (no Claude for rule-only fixes)
Nothing here changes a live site without an approved proposal. SAFE fixes are auto-approved only when the site has tech_auto_safe=true, is not medical / reviewer_required, and the fix is High confidence.
Rules and role: agents/technical_fixer.md."""
import os, re, sys, json, time, html, datetime as dt
from urllib.parse import urlparse, urljoin
import requests
from . import sb, llm

AGENT = "technical_fixer"
UA = {"User-Agent": "Mozilla/5.0 (compatible; SEOAgentTechFixer/1.0)", "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8"}
SEV = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
EFF = {"Low": 0, "Medium": 1, "High": 2}
VIEWPORT = '<meta name="viewport" content="width=device-width, initial-scale=1">'
TITLE_MAX, TITLE_MIN, META_MAX, META_MIN = 60, 15, 155, 70

# issue code -> (risk, fix_type). A code that is not here is RISKY by default and has no automatic fix.
SPEC = {
 "title_missing": ("SAFE", "title_meta"), "title_long": ("SAFE", "title_meta"), "meta_missing": ("SAFE", "title_meta"), "meta_long": ("SAFE", "title_meta"),
 "alt_missing": ("SAFE", "alt_text"), "viewport_missing": ("SAFE", "viewport_add"),
 "canonical_missing": ("SAFE", "canonical_set"), "canonical_relative": ("SAFE", "canonical_set"), "canonical_http": ("SAFE", "canonical_set"),
 "schema_invalid": ("SAFE", "schema_repair"), "robots_no_sitemap": ("SAFE", "robots_sitemap_line"), "mixed_content": ("SAFE", "mixed_https"),
 "noindex": ("RISKY", "noindex_remove"), "canonical_other_domain": ("RISKY", "canonical_set"), "canonical_other_url": ("RISKY", "canonical_set"), "canonical_multiple": ("RISKY", "canonical_set"),
 "robots_blocks_all": ("RISKY", "robots_unblock"), "robots_blocks_assets": ("RISKY", "robots_unblock"),
}
# RISKY codes whose fix is a server / content decision (redirects, deletions, URL changes, sitemap, new files): the exact steps are shown, never applied by the agent.
MANUAL = {"http_no_redirect", "host_variants", "https_down", "redirect_chain", "redirect_temp", "redirect_loop", "redirect_in_sitemap", "status_4xx", "status_5xx", "broken_link",
          "robots_missing", "sitemap_missing", "sitemap_empty", "sitemap_other_host", "sitemap_too_big", "sitemap_blocked", "schema_review"}
RULE_ONLY = {"viewport_add", "robots_sitemap_line", "mixed_https", "noindex_remove", "robots_unblock", "canonical_set"}   # fully decided by the page; Claude is optional for these
TEXT_TYPES = {"robots_sitemap_line", "robots_unblock"}
COPY_TYPES = {"title_meta", "alt_text"}

def classify(code):
    """SAFE or RISKY. Unknown codes are RISKY."""
    return SPEC.get(code, ("RISKY", None))[0]
def plan(code):
    """(risk, fix_type) for codes we can act on, else None (nothing to do automatically)."""
    if code in SPEC: return SPEC[code]
    return ("RISKY", "manual") if code in MANUAL else None
def is_medical(site): return site.get("profile") == "medical" or bool(site.get("reviewer_required"))
def conf(c):
    c = str(c or "").strip().lower()
    return {"high": "High", "medium": "Medium", "med": "Medium", "low": "Low"}.get(c)
def kind_of(fix_type): return fix_type if fix_type in COPY_TYPES else "tech_fix"
def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def bare(h): return (h or "").lower().replace("www.", "")
def https(u): return re.sub(r"^http://", "https://", u or "", flags=re.I).split("#")[0]

def auto_ok(site, risk, confidence, fix_type):
    """The only place that decides auto-approval. RISKY, medical / reviewer sites, manual fixes and anything but High confidence never qualify."""
    return risk == "SAFE" and fix_type != "manual" and bool(site.get("tech_auto_safe")) and not is_medical(site) and confidence == "High"


# ---------- reading the live page ----------
def fetch(url):
    return requests.get(url, params={"nc": os.urandom(3).hex()}, headers=UA, timeout=25)

CANON = re.compile(r"<link\b(?=[^>]*\brel=[\"']canonical[\"'])[^>]*>", re.I)
ROBOTS_META = re.compile(r"<meta\b(?=[^>]*\bname=[\"']robots[\"'])[^>]*>", re.I)
VIEWPORT_META = re.compile(r"<meta\b(?=[^>]*\bname=[\"']viewport[\"'])[^>]*>", re.I)
LDJSON = re.compile(r"(<script\b[^>]*application/ld\+json[^>]*>)(.*?)(</script>)", re.S | re.I)
IMG = re.compile(r"<img\b[^>]*>", re.I)
def _clean(s): return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()
def _attr(tag, name):
    m = re.search(r'\s%s\s*=\s*(?:"([^"]*)"|\'([^\']*)\')' % re.escape(name), tag, re.I)
    return None if not m else html.unescape(m.group(1) if m.group(1) is not None else m.group(2))

def page_ctx(url, fetcher=None):
    r = (fetcher or fetch)(url)
    if getattr(r, "status_code", 200) != 200: raise RuntimeError(f"HTTP {r.status_code} for {url}")
    h = r.text; ctx = {"url": url, "html": h, "host": bare(urlparse(url).netloc), "headers": dict(getattr(r, "headers", {}) or {})}
    head = h[:h.lower().find("</head>") + 7] if "</head>" in h.lower() else h
    t = re.search(r"<title[^>]*>(.*?)</title>", head, re.S | re.I); ctx["title"] = _clean(t.group(1)) if t else ""
    mt = re.search(r'<meta\b(?=[^>]*\bname=["\']description["\'])[^>]*>', head, re.I); ctx["meta"] = (_attr(mt.group(0), "content") or "").strip() if mt else ""
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", h, re.S | re.I); ctx["h1"] = _clean(h1.group(1)) if h1 else ""
    body = re.sub(r"(?is)<(script|style|noscript|nav|header|footer|head)[^>]*>.*?</\1>", " ", h)
    ctx["paras"] = [p for p in (_clean(x) for x in re.findall(r"<p[^>]*>(.*?)</p>", body, re.S | re.I)) if len(p) >= 60][:4]
    ctx["canonicals"] = CANON.findall(h); ctx["robots_meta"] = [x for x in ROBOTS_META.findall(h) if "noindex" in x.lower()]
    ctx["has_viewport"] = bool(VIEWPORT_META.search(h))
    ctx["ld_bad"] = [m.group(2) for m in LDJSON.finditer(h) if not _json_ok(m.group(2))]
    ctx["imgs_no_alt"] = [{"src": _attr(i, "src") or _attr(i, "data-src") or "", "tag": i} for i in IMG.findall(h) if not (_attr(i, "alt") or "").strip() and (_attr(i, "src") or _attr(i, "data-src"))]
    return ctx

def _json_ok(s):
    try: json.loads(s); return True
    except ValueError: return False


# ---------- pure repair helpers ----------
def repair_json(text):
    """Fix the usual JSON-LD slips (smart quotes, comments, trailing commas, missing commas between lines, single quotes). Returns a compact-pretty JSON string or None."""
    t = (text or "").strip().lstrip("﻿")
    steps = [lambda s: s,
             lambda s: s.translate({0x201c: '"', 0x201d: '"', 0x2018: "'", 0x2019: "'"}),
             lambda s: re.sub(r"/\*.*?\*/", "", re.sub(r"(?m)^\s*//.*$", "", s), flags=re.S),
             lambda s: re.sub(r",(\s*[}\]])", r"\1", s),
             lambda s: re.sub(r'(["\d\]}el])(\s*\n\s*)(")', r"\1,\2\3", s),
             lambda s: re.sub(r"'([^'\n]*)'", lambda m: json.dumps(m.group(1)), s)]
    for i in range(len(steps)):
        t2 = t
        for st in steps[:i + 1]: t2 = st(t2)
        try: return json.dumps(json.loads(t2), ensure_ascii=False, indent=1)
        except ValueError: continue
    return None

def _strings(o):
    if isinstance(o, str): yield o
    elif isinstance(o, dict):
        for k, v in o.items(): yield from _strings(v)
    elif isinstance(o, list):
        for v in o: yield from _strings(v)

def _bad_schema(o):
    try:
        from .static import _bad_schema as b; return b(o)
    except ImportError:
        return bool(re.search(r"(?i)aggregaterating|\"review\"", json.dumps(o)))

def shorten(text, limit, floor=0):
    """Cut at a sentence end, else a word end, never mid-word; no ellipsis."""
    t = re.sub(r"\s+", " ", text or "").strip()
    if len(t) <= limit: return t
    cut = t[:limit + 1]; m = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if m >= floor: return cut[:m + 1].strip()
    cut = cut[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-–—|")
    return cut

STOP = {"and", "or", "the", "of", "in", "for", "to", "a", "an", "with", "on", "at", "by", "vs"}
def _trim(t, limit):
    """Word-boundary cut that never ends on a dangling stop word or punctuation."""
    c = t[:limit + 1]; c = c[:limit] if len(t) > limit and t[limit] != " " and " " in c else c[:limit]
    c = c.rsplit(" ", 1)[0] if len(t) > limit and t[limit] != " " else c
    ws = c.rstrip(" ,;:-–—|").split(" ")
    while len(ws) > 1 and ws[-1].lower().rstrip(",;:") in STOP: ws.pop()
    return " ".join(ws).rstrip(" ,;:-–—|")

def shorten_title(t, limit=TITLE_MAX):
    t = re.sub(r"\s+", " ", t or "").strip()
    if len(t) <= limit: return t
    parts = re.split(r"\s+[|–—-]\s+", t)
    if len(parts) > 1:
        brand = parts[-1]
        for k in range(len(parts) - 1, 0, -1):
            c = " | ".join(parts[:k] + ([brand] if k < len(parts) - 1 else []))
            if TITLE_MIN <= len(c) <= limit: return c
        room = limit - len(brand) - 3
        if room >= 20: return _trim(parts[0], room) + " | " + brand
    return _trim(parts[0] if len(parts) > 1 and len(parts[0]) > TITLE_MIN else t, limit)


# ---------- building the exact edit (pure: text in, text out + revert pairs) ----------
# pairs = [{"old": ..., "new": ...}]: static.revert_alt() puts old back where new is, so the standard rollback_mod works for these edits too. 'new' is never empty.
def _need(cond, msg):
    if not cond: raise RuntimeError(msg)

def build_edit(fix_type, text, fix, host=""):
    """Returns (new_text, pairs). Raises RuntimeError with a plain message if the file does not look as expected (then nothing is written)."""
    after, before = fix.get("after") or "", fix.get("before") or ""
    if fix_type == "viewport_add":
        _need(not VIEWPORT_META.search(text), "a viewport tag is already in this file")
        _need("</head>" in text.lower(), "no </head> in this file"); add = after.strip() + "\n"; i = text.lower().rindex("</head>")
        return text[:i] + add + text[i:], [{"old": "", "new": add}]
    if fix_type == "canonical_set":
        tags = CANON.findall(text); want = after.strip(); pairs = []
        _need(want.startswith("<link") and CANON.fullmatch(want) is not None, "bad canonical tag")
        if not tags:
            _need("</head>" in text.lower(), "no </head> in this file"); i = text.lower().rindex("</head>"); add = want + "\n"
            return text[:i] + add + text[i:], [{"old": "", "new": add}]
        new = text.replace(tags[0], want, 1); pairs.append({"old": tags[0], "new": want})
        for k, extra in enumerate(tags[1:], 1):
            c = f"<!--sab:removed duplicate canonical {k}-->"; new = new.replace(extra, c, 1); pairs.append({"old": extra, "new": c})
        _need(new != text, "the canonical tag is already correct"); return new, pairs
    if fix_type == "noindex_remove":
        tags = [t for t in ROBOTS_META.findall(text) if "noindex" in t.lower()]; _need(tags, "no noindex meta tag in this file (it may come from a server header)")
        new, pairs = text, []
        for k, t in enumerate(tags):
            c = f"<!--sab:removed noindex {k}-->"; new = new.replace(t, c, 1); pairs.append({"old": t, "new": c})
        return new, pairs
    if fix_type == "schema_repair":
        _need(before.strip() and after.strip(), "no JSON-LD text to repair"); json.loads(after)
        for m in LDJSON.finditer(text):
            if m.group(2).strip() == before.strip() and not _json_ok(m.group(2)):
                lead = re.match(r"\s*", m.group(2)).group(0); tail = re.search(r"\s*$", m.group(2)).group(0); new_inner = lead + after.strip() + tail
                return text[:m.start(2)] + new_inner + text[m.end(2):], [{"old": m.group(2), "new": new_inner}]
        raise RuntimeError("the invalid JSON-LD block was not found in this file (already fixed, or it is added by a plugin)")
    if fix_type == "mixed_https":
        h = bare(urlparse(after).netloc or after.replace("https://", "")); pairs = {}
        rx = re.compile(r"""(?<=[\s"'(,])(?:src|href|data-src|data-srcset|srcset|poster|content|action)\s*=\s*(["'])[^"']*?\1""", re.I)
        rh = re.compile(r"http://((?:www\.)?%s)(?=[/\"'\s?#,]|$)" % re.escape(h), re.I)
        def attr(m):
            v = m.group(0); n = rh.sub(lambda x: "https://" + x.group(1), v)
            if n != v: pairs[v] = n
            return n
        new = rx.sub(attr, text); _need(new != text, "no http:// link to the site's own files found in this file")
        return new, [{"old": o, "new": n} for o, n in pairs.items()]
    if fix_type == "robots_sitemap_line":
        _need(re.match(r"(?i)sitemap:\s*https://\S+$", after.strip()), "bad Sitemap line"); _need(not re.search(r"(?im)^\s*sitemap:", text), "robots.txt already has a Sitemap line")
        add = ("\n" if text and not text.endswith("\n") else "") + after.strip() + "\n"; return text + add, [{"old": "", "new": add}]
    if fix_type == "robots_unblock":
        lines = [l for l in before.split("\n") if l.strip()]; _need(lines, "no robots.txt lines to remove"); new, pairs = text, []
        for l in lines:
            _need(re.match(r"(?i)\s*disallow:", l), "only Disallow lines can be removed")
            rx = re.compile(r"(?m)^[ \t]*" + re.escape(l.strip()) + r"[ \t]*$"); _need(rx.search(new), f"line not found in robots.txt: {l.strip()}")
            c = f"# sab:removed {l.strip()}"; new = rx.sub(c, new, count=1); pairs.append({"old": l, "new": c})
        return new, pairs
    raise RuntimeError("no automatic edit for fix type " + str(fix_type))

def verify_text(fix_type, fix, text, headers=None):
    """True when the fix is visible in the freshly fetched live text."""
    after = fix.get("after") or ""; headers = headers or {}
    if fix_type == "viewport_add": return bool(VIEWPORT_META.search(text))
    if fix_type == "canonical_set":
        tags = CANON.findall(text); want = _attr(after, "href")
        return len(tags) == 1 and (_attr(tags[0], "href") or "").strip().rstrip("/") == (want or "").rstrip("/")
    if fix_type == "noindex_remove":
        return not any("noindex" in t.lower() for t in ROBOTS_META.findall(text)) and "noindex" not in str(headers.get("X-Robots-Tag", "")).lower()
    if fix_type == "schema_repair":
        try: want = json.loads(after)
        except ValueError: return False
        blocks = [m.group(2) for m in LDJSON.finditer(text)]
        return all(_json_ok(b) for b in blocks) and any(_json_ok(b) and json.loads(b) == want for b in blocks)
    if fix_type == "mixed_https":
        h = bare(urlparse(after).netloc or after.replace("https://", "")); return not re.search(r"(?i)\b(src|href|srcset|poster|data-src)\s*=\s*[\"'][^\"']*http://(www\.)?%s" % re.escape(h), text)
    if fix_type == "robots_sitemap_line": return after.strip().lower() in [l.strip().lower() for l in text.splitlines()]
    if fix_type == "robots_unblock": cur = {l.strip().lower() for l in text.splitlines()}; return not any(l.strip().lower() in cur for l in (fix.get("before") or "").split("\n") if l.strip())
    if fix_type == "title_meta":
        a = page_ctx_from_html(text); n = lambda s: re.sub(r"\s+", " ", s or "").strip()
        return n(a["title"]) == n(fix["new"].get("title")) and n(a["meta"]) == n(fix["new"].get("meta"))
    if fix_type == "alt_text":
        from .static import img_key
        want = {img_key(i["src"]): re.sub(r"\s+", " ", i["alt"]).strip() for i in fix["new"].get("images", []) if img_key(i.get("src"))}; hit = 0
        for tag in IMG.findall(text):
            ks = {img_key(_attr(tag, a)) for a in ("src", "data-src") if _attr(tag, a)} & set(want)
            if ks:
                if re.sub(r"\s+", " ", _attr(tag, "alt") or "").strip() != want[next(iter(ks))]: return False
                hit += 1
        return hit > 0
    return False

def page_ctx_from_html(h):
    class R: status_code = 200; text = h; headers = {}
    return page_ctx("https://x.invalid/", lambda u: R)


# ---------- generating the fix: rules ----------
def own_http_hosts(ctx):
    seen = {}
    for m in re.finditer(r"""(?i)\b(?:src|href|srcset|poster|data-src)\s*=\s*["'][^"']*?(http://((?:www\.)?[a-z0-9.-]+))""", ctx["html"]):
        if bare(m.group(2)) == ctx["host"]: seen.setdefault(m.group(2).lower(), 0); seen[m.group(2).lower()] += 1
    return seen

def rule_fix(code, fix_type, issue, ctx, site, fetcher=None):
    """The simple cases with no Claude. Returns a fix dict or None when no safe rule applies."""
    url = ctx["url"]; ev = f"tech_issues #{issue.get('id')} {code}: {str(issue.get('detail') or '')[:120]}; live page fetched {url}"
    R = lambda before, after, why, c="High": {"fix_type": fix_type, "target_url": url, "before": before, "after": after, "rationale": why, "confidence": c, "evidence": ev, "source": "rule"}
    if fix_type == "viewport_add": return R("", VIEWPORT, "Mobile viewport tag is missing, so phones can show the page as a tiny desktop page.") if not ctx["has_viewport"] else None
    if fix_type == "canonical_set":
        want = https(url); c0 = ctx["canonicals"][0] if ctx["canonicals"] else ""
        return R(c0, f'<link rel="canonical" href="{want}">', "Canonical points to the page's own https URL.")
    if fix_type == "noindex_remove":
        return R(ctx["robots_meta"][0], "", "Remove the noindex robots meta tag so the page can be indexed.") if ctx["robots_meta"] else None
    if fix_type == "schema_repair":
        if not ctx["ld_bad"]: return None
        raw = ctx["ld_bad"][0]; fixed = repair_json(raw)
        return R(raw.strip(), fixed, "Only syntax slips (commas, quotes) were corrected; no value was added or changed.") if fixed else None
    if fix_type == "mixed_https":
        hosts = own_http_hosts(ctx)
        if not hosts: return None
        top = max(hosts, key=hosts.get); return R("http://" + top, "https://" + top, f"{sum(hosts.values())} link(s) to the site's own files use http:// on an https page.")
    if fix_type == "robots_sitemap_line":
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        try: r = (fetcher or fetch)(base + "/sitemap.xml"); ok = r.status_code == 200 and "<loc" in r.text
        except requests.RequestException: ok = False
        return R("", f"Sitemap: {base}/sitemap.xml", "Points Google to the sitemap (it loads and lists URLs).") if ok else None
    if fix_type == "robots_unblock":
        rx = re.compile(r"(?i)^[ \t]*disallow:[ \t]*/[ \t]*$") if code == "robots_blocks_all" else re.compile(r"(?i)^[ \t]*disallow:[ \t]*\S*(?:/wp-content/(?!uploads)|/wp-includes/|\.css|\.js\b|/assets/|/static/)")
        lines = [l.strip() for l in ctx["html"].splitlines() if rx.match(l.split("#")[0])]
        return R("\n".join(dict.fromkeys(lines)), "", "These Disallow lines stop Google from crawling " + ("the whole site." if code == "robots_blocks_all" else "CSS/JS it needs to render pages."), "Medium") if lines else None
    if fix_type == "title_meta":
        t, m = ctx["title"], ctx["meta"]; nt, nm = t, m; ws = []; codes = set(ctx.get("codes") or [code])
        if codes & {"title_long", "title_missing"}:
            cand = shorten_title(t if t else ctx["h1"]); nt = cand; ws.append("title")
        if codes & {"meta_long", "meta_missing"}:
            src = m if m else " ".join(ctx["paras"][:2]); nm = shorten(src, META_MAX, META_MIN); ws.append("meta description")
        if (nt, nm) == (t, m): return None
        return R(json.dumps({"title": t, "meta": m}, ensure_ascii=False), json.dumps({"title": nt, "meta": nm}, ensure_ascii=False),
                 "Rule-based: " + " and ".join(ws) + " " + ("shortened at a clean word boundary from the existing text." if (t if "title" in ws else m) else "taken from the page's own " + ("H1." if "title" in ws else "first paragraph.")), "Medium")
    if fix_type == "alt_text":
        items = []
        for i in ctx["imgs_no_alt"][:10]:
            f = os.path.basename(urlparse(i["src"]).path); f = re.sub(r"\.[a-z0-9]{2,5}$", "", f, flags=re.I); f = re.sub(r"-\d{2,4}x\d{2,4}$", "", f)
            words = re.sub(r"[-_]+", " ", f).strip()
            if len(words) >= 6 and not re.search(r"\d{3,}", words) and " " in words: items.append({"src": i["src"], "alt": words[:1].upper() + words[1:]})
        return R("[]", json.dumps(items, ensure_ascii=False), "Alt text built from descriptive image file names only.", "Low") if items else None
    return None


# ---------- generating the fix: Claude ----------
SYS = """You are the Technical SEO Fixer. You write ONE exact fix for ONE technical SEO issue on ONE page and answer with STRICT JSON only (no prose, no markdown).
Reply keys: fix_type, target_url, before, after, rationale, confidence (High|Medium|Low), evidence.
Rules: use ONLY facts in the page data given; never invent facts, numbers, names or claims; if the data is not enough, reply {"fix_type":"none","rationale":"DATA NOT AVAILABLE: <what is missing>","confidence":"Low","evidence":"-"}.
fix_type MUST be exactly the required_fix_type, target_url exactly the given url. Never add a competitor name, a ranking promise, a guarantee, a price or a result claim.
before/after by type: viewport_add: before "" after the full <meta name="viewport"> tag. canonical_set: before the current canonical tag or "", after ONE full <link rel="canonical" href="SELF"> where SELF is the page's own https URL.
noindex_remove: before the exact robots meta tag, after "". schema_repair: before the exact invalid JSON text, after the corrected valid JSON text; fix syntax only, add no new values.
mixed_https: before "http://HOST", after "https://HOST" (the site's own host only). robots_sitemap_line: before "", after "Sitemap: https://HOST/sitemap.xml". robots_unblock: before the exact Disallow line(s) to remove, after "".
title_meta: before and after are objects {"title","meta"}; change only the field that has the issue and keep the other as it is; title at most 60 characters, meta at most 155, written for readers, main topic first, no keyword stuffing.
alt_text: before [] , after a list of {"src","alt"} for images that have no alt; alt 4-12 words, describes what the image shows using the nearby text, no "image of", no file names, no medical result claims.
Medical sites: no outcome, safety or superiority claims. Confidence High only when the fix follows directly from the page data."""

def ask_json(system, user, **kw): return llm.ask_json(system, user, **kw)

def llm_fix(code, fix_type, issue, ctx, site, askf=None):
    page = {"title": ctx["title"], "meta": ctx["meta"], "h1": ctx["h1"], "first_paragraphs": ctx["paras"][:3], "canonical_tags": ctx["canonicals"][:3], "noindex_tags": ctx["robots_meta"][:2],
            "has_viewport": ctx["has_viewport"], "invalid_jsonld": [b.strip()[:1500] for b in ctx["ld_bad"][:1]], "images_without_alt": [i["src"] for i in ctx["imgs_no_alt"][:10]]}
    if fix_type in TEXT_TYPES: page["robots_txt"] = ctx["html"][:2000]
    if fix_type == "mixed_https": page["own_http_hosts"] = list(own_http_hosts(ctx))
    user = {"required_fix_type": fix_type, "target_url": ctx["url"], "issue": {"code": code, "all_codes_for_this_url": ctx.get("codes") or [code], "detail": issue.get("detail"), "audit_hint": issue.get("fix")},
            "site": {"domain": site.get("domain"), "medical": is_medical(site)}, "page": page}
    r = (askf or ask_json)(SYS, json.dumps(user, ensure_ascii=False), agent=AGENT, site_id=site.get("id"), max_tokens=1500)
    if not isinstance(r, dict): raise ValueError("reply is not a JSON object")
    return r

def _obj(v):
    if isinstance(v, str):
        try: return json.loads(v)
        except ValueError: return v
    return v

def normalise(r, fix_type, ctx):
    """Strict shape check of a Claude reply. Returns a fix dict or raises ValueError."""
    for k in ("fix_type", "target_url", "before", "after", "rationale", "confidence", "evidence"):
        if k not in r: raise ValueError("missing key " + k)
    if r["fix_type"] != fix_type: raise ValueError(f"fix_type {r['fix_type']!r} is not {fix_type!r}")
    if str(r["target_url"]).rstrip("/") != ctx["url"].rstrip("/"): raise ValueError("target_url differs from the issue url")
    c = conf(r["confidence"])
    if not c: raise ValueError("bad confidence")
    if not str(r["rationale"]).strip() or not str(r["evidence"]).strip() or str(r["evidence"]).strip() == "-": raise ValueError("rationale / evidence empty")
    b, a = r["before"], r["after"]
    if b is None and fix_type not in COPY_TYPES: b = ""
    if a is None and fix_type not in COPY_TYPES: a = ""
    if fix_type in COPY_TYPES: b, a = _obj(b), _obj(a)
    elif not isinstance(b, str) or not isinstance(a, str): raise ValueError("before/after must be text")
    if fix_type == "title_meta":
        if not (isinstance(a, dict) and isinstance(a.get("title"), str) and isinstance(a.get("meta"), str)): raise ValueError("title_meta.after must be {title, meta}")
        b = b if isinstance(b, dict) else {"title": ctx["title"], "meta": ctx["meta"]}
        b, a = json.dumps({"title": b.get("title", ctx["title"]), "meta": b.get("meta", ctx["meta"])}, ensure_ascii=False), json.dumps({"title": a["title"].strip(), "meta": a["meta"].strip()}, ensure_ascii=False)
    if fix_type == "alt_text":
        if not (isinstance(a, list) and a and all(isinstance(i, dict) and i.get("src") and i.get("alt") for i in a)): raise ValueError("alt_text.after must be a list of {src, alt}")
        b, a = json.dumps(b if isinstance(b, list) else [], ensure_ascii=False), json.dumps([{"src": str(i["src"]), "alt": str(i["alt"])} for i in a][:10], ensure_ascii=False)
    return {"fix_type": fix_type, "target_url": ctx["url"], "before": b, "after": a, "rationale": str(r["rationale"]).strip(), "confidence": c, "evidence": str(r["evidence"]).strip(), "source": "llm"}


# ---------- validation (applies to Claude output AND rule output) ----------
def validate(fix, ctx, site):
    """List of problems; empty list = acceptable. This is what stops Claude from inventing, escalating or breaking a page."""
    ft, a, b, e = fix["fix_type"], fix["after"], fix["before"], []
    host = ctx["host"]
    if ft == "viewport_add":
        if not (VIEWPORT_META.fullmatch(a.strip()) and "width=device-width" in a): e.append("not a valid viewport tag")
    elif ft == "canonical_set":
        if not CANON.fullmatch(a.strip()): e.append("not a single canonical link tag")
        else:
            href = (_attr(a, "href") or "").strip()
            if href.rstrip("/") != https(ctx["url"]).rstrip("/"): e.append("canonical must be the page's own https URL")
    elif ft == "noindex_remove":
        if a.strip(): e.append("noindex fix must only remove the tag")
        if not (ROBOTS_META.fullmatch(b.strip()) and "noindex" in b.lower()): e.append("before is not a noindex robots tag")
    elif ft == "schema_repair":
        try:
            d = json.loads(a)
            if _bad_schema(d): e.append("Review / AggregateRating schema is not allowed")
            raw = b.replace("\\/", "/")
            for s in _strings(d):
                if s not in raw and json.dumps(s, ensure_ascii=False)[1:-1] not in raw: e.append(f"value not in the original markup: {s[:40]}"); break
        except ValueError: e.append("repaired JSON-LD does not parse")
        if b.strip() not in [x.strip() for x in ctx["ld_bad"]]: e.append("before is not an invalid JSON-LD block of this page")
    elif ft == "mixed_https":
        if not (re.fullmatch(r"http://[a-z0-9.-]+", b, re.I) and a == "https://" + b[7:] and bare(urlparse(b).netloc) == host): e.append("only the site's own host may be changed from http to https")
    elif ft == "robots_sitemap_line":
        if not re.fullmatch(r"(?i)sitemap:\s*https://\S+", a.strip()): e.append("bad Sitemap line")
        elif bare(urlparse(a.split(":", 1)[1].strip()).netloc) != host: e.append("Sitemap must be on the site's own host")
    elif ft == "robots_unblock":
        if a.strip(): e.append("robots fix must only remove Disallow lines")
        ls = [l for l in b.split("\n") if l.strip()]
        if not ls or not all(re.match(r"(?i)\s*disallow:", l) and l.strip() in ctx["html"] for l in ls): e.append("lines are not Disallow lines of this robots.txt")
    elif ft == "title_meta":
        try: new, old = json.loads(a), json.loads(b)
        except ValueError: return ["title_meta is not JSON"]
        t, m = new.get("title", ""), new.get("meta", "")
        if t != old.get("title") and not (TITLE_MIN <= len(t) <= 65): e.append(f"title length {len(t)} (need {TITLE_MIN}-65)")
        if m != old.get("meta") and not (META_MIN <= len(m) <= 160): e.append(f"meta length {len(m)} (need {META_MIN}-160)")
        if (t, m) == (old.get("title"), old.get("meta")): e.append("no change")
        try:
            from .onpage import copy_problems
            cp = copy_problems(t if t != old.get("title") else "", m if m != old.get("meta") else "", site)
        except ImportError: cp = []
        e += cp
        if re.search(r"(?i)\b(rank(s|ing)?|top 1|number one|guarantee[ds]?)\b", f"{t} {m}") and not re.search(r"(?i)\b(rank|ranking|guarantee)", f"{old.get('title')} {old.get('meta')}"): e.append("ranking / guarantee claim")
    elif ft == "alt_text":
        try: items = json.loads(a)
        except ValueError: return ["alt_text is not JSON"]
        from .onpage import check_alt
        known = {i["src"] for i in ctx["imgs_no_alt"]}
        for i in items:
            if i["src"] not in known: e.append(f"image not found without alt: {i['src'][-40:]}")
            e += [f"{i['src'][-30:]}: {x}" for x in check_alt(i["alt"], site)]
    else: e.append("unknown fix type")
    return list(dict.fromkeys(e))

def generate(issue, ctx, site, askf=None, fetcher=None):
    """Claude first (when available), then validation, then the rule for the simple cases. Returns (fix | None, notes)."""
    code = issue["code"]; risk, ft = plan(code); notes = []
    cands = []
    if not (ft in RULE_ONLY and os.environ.get("FIXER_LLM_SIMPLE") == "0"):
        try:
            f = normalise(llm_fix(code, ft, issue, ctx, site, askf), ft, ctx); cands.append(f)
        except llm.LLMUnavailable as e: notes.append("no Claude: " + str(e)[:60])
        except (ValueError, KeyError, TypeError) as e: notes.append("Claude reply unusable: " + str(e)[:100])
        except Exception as e: notes.append("Claude call failed: " + str(e)[:100])
    codes = ctx.get("codes") or [code]
    for f in cands:
        if ft == "title_meta": f = _only_issue_fields(f, codes)
        errs = validate(f, ctx, site)
        if not errs: return {**f, "risk": risk, "code": code}, notes
        notes.append("Claude fix rejected: " + "; ".join(errs)[:160])
    try: f = rule_fix(code, ft, issue, ctx, site, fetcher)
    except Exception as e: f = None; notes.append("rule failed: " + str(e)[:80])
    if f:
        if ft == "title_meta": f = _only_issue_fields(f, codes)
        errs = validate(f, ctx, site)
        if not errs: return {**f, "risk": risk, "code": code}, notes
        notes.append("rule fix rejected: " + "; ".join(errs)[:160])
    else: notes.append("no safe rule for this page")
    return None, notes


# ---------- proposals + tech_fixes ----------
def usage_mark():
    try: r = sb.select("llm_usage", agent=f"eq.{AGENT}", select="id", order="id.desc", limit="1"); return int(r[0]["id"]) if r else 0
    except Exception: return None
def usage_since(mark):
    """(last llm_usage id, cost in USD) of the Claude calls made since `mark`; (None, None) when it cannot be read."""
    if mark is None: return None, None
    try:
        r = sb.select("llm_usage", agent=f"eq.{AGENT}", id=f"gt.{mark}", select="id,cost_usd")
        return (max(int(x["id"]) for x in r), round(sum(float(x["cost_usd"] or 0) for x in r), 6)) if r else (None, 0)
    except Exception: return None, None

def proposal_for(site_id, site, fix, ctx, post_id):
    ft, risk, c = fix["fix_type"], fix["risk"], fix["confidence"]
    auto = auto_ok(site, risk, c, ft)
    if risk == "RISKY": auto = False
    why = f"{fix['rationale']} | Risk: {risk} | Confidence: {c} | Evidence: {fix['evidence']}"
    base = {"site_id": site_id, "page": fix["target_url"], "post_id": post_id, "kind": kind_of(ft), "needs_approval": not auto, "status": "approved" if auto else "pending", "reason": why[:1500]}
    if auto: base.update(decided_at=now(), decided_by="auto:" + AGENT)
    base["source"] = "onpage:tech_fixer" if (ft == "title_meta" and is_medical(site)) else "tech_fixer"   # 'onpage' prefix keeps the reviewer gate on medical sites
    if ft == "title_meta":
        old, new = json.loads(fix["before"]), json.loads(fix["after"]); base.update(old={"title": old["title"], "meta": old["meta"]}, new={"title": new["title"], "meta": new["meta"]})
    elif ft == "alt_text": base.update(old={}, new={"images": json.loads(fix["after"]), "basis": fix["rationale"]})
    else: base.update(old={"before": fix["before"]}, new={"fix_type": ft, "target_url": fix["target_url"], "before": fix["before"], "after": fix["after"], "code": fix["code"], "risk": risk})
    return base

def fix_rows(site_id, issues, fix, proposal, cost):
    st = proposal["status"] if proposal else "proposed"
    return [{"issue_id": i.get("id"), "site_id": site_id, "url": fix["target_url"], "code": i["code"], "fix_type": fix["fix_type"], "risk": fix["risk"], "status": "approved" if st == "approved" else "proposed",
             "manual": fix["fix_type"] == "manual", "before": str(fix["before"])[:4000], "after": str(fix["after"])[:4000], "rationale": fix["rationale"][:1000], "confidence": fix["confidence"],
             "evidence": fix["evidence"][:1000], "source": fix["source"], "proposal_id": (proposal or {}).get("id"), "llm_usage_id": cost[0], "llm_cost_usd": cost[1]} for i in issues]

def manual_fix(issue):
    risk, _ = plan(issue["code"])
    return {"fix_type": "manual", "target_url": issue["url"], "before": str(issue.get("detail") or ""), "after": str(issue.get("fix") or ""), "rationale": str(issue.get("why") or "")[:500],
            "confidence": "High", "evidence": f"tech_issues #{issue.get('id')} {issue['code']}: {str(issue.get('detail') or '')[:120]}", "source": "audit", "risk": risk, "code": issue["code"]}

def _ts(x):
    try: return dt.datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    except ValueError: return None

def select_issues(site_id):
    """Open issues that have a fix path and no fix in flight (or two failed tries), worst severity and lowest effort first.
    A rejected or rolled-back fix is never proposed again (a person said no). A verified fix only blocks an issue the audit found before the fix was verified; if the next audit finds it again, it is tried again."""
    rows = sb.select("tech_issues", site_id=f"eq.{site_id}", status="eq.open", limit="3000")
    fx = sb.select("tech_fixes", site_id=f"eq.{site_id}", status="in.(proposed,approved,applied,verified,rejected,rolled_back,failed)", select="url,code,status,verified_at")
    busy, nfail, ver = set(), {}, {}
    for x in fx:
        k = (x["url"], x["code"])
        if x["status"] == "failed": nfail[k] = nfail.get(k, 0) + 1
        elif x["status"] == "verified":
            v = _ts(x.get("verified_at"))
            if v and (k not in ver or v > ver[k]): ver[k] = v
            elif k not in ver: ver[k] = None
        else: busy.add(k)
    def blocked(r):
        k = (r["url"], r["code"]); v = ver.get(k)
        return k in busy or nfail.get(k, 0) >= 2 or (k in ver and (v is None or (_ts(r.get("found_at")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)) <= v))
    out = [r for r in rows if plan(r.get("code")) and not blocked(r)]
    out.sort(key=lambda r: (SEV.get(r.get("severity"), 9), EFF.get(r.get("effort"), 1), r["url"]))
    return out


# ---------- applying ----------
def can_apply(site, w, ft):
    """What this site's connector can write. WordPress needs plugin kind 'tech' (see PATCHES.md) for the tag-level fixes."""
    if ft in COPY_TYPES: return True
    if getattr(w, "static", False): return True
    return bool(site.get("tech_plugin_kind")) and ft not in TEXT_TYPES

def post_id_for(w, url):
    if getattr(w, "static", False):
        from .static import Static; return Static.pid(url)
    pid = (w.live(url) or {}).get("post_id"); return int(pid) if pid else None

def _static_apply(w, p):
    """Edit one file through the FTP adapter's own load/backup/save, and record revert pairs so the normal rollback_mod undoes it."""
    import posixpath
    fx = {**p["new"]}; url = p["page"]; pid = w._reg(url); u, f, h, enc = w._load(pid)
    new, pairs = build_edit(fx["fix_type"], h, fx, w.host); mid = int(time.time() * 1000)
    w._save(f, new, enc, h.encode(enc, "replace"), mid); w._rem().write(posixpath.join(w.root, ".sab", f"{mid}.alt.json"), json.dumps(pairs).encode())
    return {"mod_id": mid}

def apply_tech_fix(site, w, p, fetcher=None, sleep=time.sleep):
    """One approved tech_fix proposal: write, fetch the live URL again, keep only if visible. Returns (ok, message)."""
    fx = p["new"]; ft = fx["fix_type"]
    if p.get("decided_by", "").startswith("auto:") and fx.get("risk") != "SAFE": raise RuntimeError("RISKY fix was auto-approved: refused")
    if not can_apply(site, w, ft): raise RuntimeError("this site's connector cannot write this fix type yet; do it by hand (see the fix text) or install plugin kind 'tech'")
    r = _static_apply(w, p) if getattr(w, "static", False) else w.add_mod(p["post_id"], "tech", {**fx, "url": p["page"]})
    mid = r["mod_id"]
    if _live_ok(ft, fx, p["page"], fetcher, sleep): return True, mid
    try: (w.rollback_mod(p["post_id"], mid))
    except Exception as e: return False, f"change saved but not visible on the live page, and it could not be removed: {str(e)[:120]}"
    return False, "change was saved but did not show on the live page (cache, or the tag comes from a plugin). It was removed again."

def _live_ok(ft, fx, url, fetcher=None, sleep=time.sleep, tries=None, wait=None):
    tries = tries or int(os.environ.get("FIXER_VERIFY_TRIES") or 3); wait = float(os.environ.get("FIXER_VERIFY_WAIT") or 5) if wait is None else wait
    for k in range(tries):
        try:
            r = (fetcher or fetch)(url)
            if getattr(r, "status_code", 200) == 200 and verify_text(ft, fx, r.text, getattr(r, "headers", None)): return True
        except requests.RequestException: pass
        if k < tries - 1: sleep(wait)
    return False

def _set_issue_fixed(fix_row):
    if fix_row.get("issue_id"): sb.update("tech_issues", {"id": fix_row["issue_id"]}, {"status": "fixed"})

def apply_all(site_id, site, w, fetcher=None, sleep=time.sleep):
    """Apply approved proposals made by this agent. title_meta / alt_text reuse the repo's own apply path (reviewer gate and mod_visible included)."""
    from . import run as runmod
    row = (sb.select("sites", id=f"eq.{site_id}") or [{}])[0]; n = 0
    for p in sb.select("proposals", site_id=f"eq.{site_id}", status="eq.approved", kind="in.(title_meta,alt_text,tech_fix)", source="in.(tech_fixer,onpage:tech_fixer)"):
        try:
            if p["kind"] == "tech_fix":
                ok, m = apply_tech_fix(site, w, p, fetcher, sleep)
                if not ok: sb.update("proposals", {"id": p["id"]}, {"status": "failed", "reason": m[:300]}); print("FAILED", p["id"], m); continue
                sb.update("proposals", {"id": p["id"]}, {"status": "applied", "applied_at": now(), "backup_id": m})
            elif p["kind"] == "title_meta":
                if runmod.needs_reviewer({**row, **{"reviewer_required": row.get("reviewer_required") or site.get("reviewer_required")}}, p): print("waiting for reviewer on proposal", p["id"]); continue
                r = w.set_seo(p["post_id"], p["new"]["title"], p["new"]["meta"]); sb.update("proposals", {"id": p["id"]}, {"status": "applied", "applied_at": now(), "backup_id": r.get("backup_id")})
            else: runmod.apply_mod(site, {**row, "reviewer_required": row.get("reviewer_required") or site.get("reviewer_required")}, w, p)
            n += 1
        except Exception as e:
            sb.update("proposals", {"id": p["id"]}, {"status": "failed", "reason": str(e)[:300]}); print("FAILED", p["id"], e)
    return n

def apply_rollbacks(site_id, w):
    """Rollbacks of this agent's tech_fix proposals (also handles files that are not pages, like robots.txt)."""
    n = 0
    for p in sb.select("proposals", site_id=f"eq.{site_id}", status="eq.rollback_requested", kind="eq.tech_fix"):
        try:
            if getattr(w, "static", False): w._reg(p["page"])
            w.rollback_mod(p["post_id"], p["backup_id"]); sb.update("proposals", {"id": p["id"]}, {"status": "rolled_back"}); n += 1
            print("rolled back tech fix", p["id"], p["page"])
        except Exception as e: sb.update("proposals", {"id": p["id"]}, {"status": "failed", "reason": "rollback: " + str(e)[:250]}); print("ROLLBACK FAILED", p["id"], e)
    return n

def sync(site_id, fetcher=None, sleep=time.sleep):
    """Bring tech_fixes in line with the proposals (approve / reject / apply / rollback / failure) and verify applied fixes on the live URL.
    Only a verified fix marks its tech_issues row 'fixed'. Unverified fixes stay 'applied' for up to 3 runs, then become 'failed' with the issue still open."""
    done = 0
    for f in sb.select("tech_fixes", site_id=f"eq.{site_id}", status="in.(proposed,approved,applied,verified)", manual="eq.false", limit="1000"):
        if not f.get("proposal_id"): continue
        ps = sb.select("proposals", id=f"eq.{f['proposal_id']}")
        if not ps: continue
        p = ps[0]; st = p["status"]
        if f["status"] == "verified" and st != "rolled_back": continue
        if st == "rejected": sb.update("tech_fixes", {"id": f["id"]}, {"status": "rejected"}); continue
        if st == "approved" and f["status"] == "proposed": sb.update("tech_fixes", {"id": f["id"]}, {"status": "approved"}); continue
        if st == "failed": sb.update("tech_fixes", {"id": f["id"]}, {"status": "failed", "reason": (p.get("reason") or "")[:300]}); continue
        if st == "rolled_back":
            sb.update("tech_fixes", {"id": f["id"]}, {"status": "rolled_back"}); _reopen(f); continue
        if st == "applied":
            if f["status"] != "applied": sb.update("tech_fixes", {"id": f["id"]}, {"status": "applied", "applied_at": p.get("applied_at") or now(), "backup_id": p.get("backup_id")})
            fx = _fix_payload(f, p)
            if _live_ok(f["fix_type"], fx, f["url"], fetcher, sleep, tries=1 if f["status"] == "applied" else 3):
                sb.update("tech_fixes", {"id": f["id"]}, {"status": "verified", "verified_at": now()}); _set_issue_fixed(f); done += 1
            else:
                tries = int(f.get("verify_tries") or 0) + 1
                sb.update("tech_fixes", {"id": f["id"]}, {"verify_tries": tries, **({"status": "failed", "reason": "applied but not visible on the live page after 3 checks; issue stays open"} if tries >= 3 else {})})
    return done

def _fix_payload(f, p):
    if f["fix_type"] in COPY_TYPES: return {"after": f.get("after"), "before": f.get("before"), "new": p.get("new") or {}}
    return {**(p.get("new") or {}), "after": (p.get("new") or {}).get("after", f.get("after")), "before": (p.get("new") or {}).get("before", f.get("before"))}

def _reopen(f):
    if f.get("issue_id"): sb.update("tech_issues", {"id": f["issue_id"], "status": "fixed"}, {"status": "open"})


# ---------- one site ----------
def fixer_max(n=None):
    try: return max(1, int(n if n is not None else os.environ.get("FIXER_MAX") or 10))
    except ValueError: return 10

def build(site_id, site, issues, w, limit, fetcher=None, askf=None, dry=False):
    """Make up to `limit` fixes from open issues. Returns (list of (fix, proposal | None, issues, (llm_usage_id, cost)), notes)."""
    out, notes, used, groups = [], [], 0, {}
    for i in issues:
        ft = plan(i["code"])[1]; groups.setdefault((i["url"], "title_meta" if ft == "title_meta" else i["code"]), []).append(i)
    for grp in groups.values():
        if used >= limit: break
        i0 = grp[0]; risk, ft = plan(i0["code"])
        if ft == "manual": out.append((manual_fix(i0), None, grp, (None, None))); continue
        mark = usage_mark()
        try: ctx = page_ctx(i0["url"], fetcher)
        except Exception as e: notes.append(f"{i0['url']}: cannot read page ({str(e)[:60]})"); continue
        ctx["codes"] = [g["code"] for g in grp]
        fix, n = generate({**i0, "detail": "; ".join(str(g.get("detail") or "") for g in grp)}, ctx, site, askf, fetcher)
        notes += [f"{i0['url']} {i0['code']}: {x}" for x in n]
        if not fix: continue
        cost = usage_since(mark)
        if not can_apply(site, w, ft):
            m = manual_fix(i0); m.update(after=str(fix["after"]), rationale=fix["rationale"][:400] + " (this site's connector cannot apply it automatically; do it by hand)"); out.append((m, None, grp, cost)); used += 1; continue
        pid = None
        if not dry:
            try: pid = post_id_for(w, i0["url"])
            except Exception as e: notes.append(f"{i0['url']}: no post id ({str(e)[:60]})")
            if pid is None: notes.append(f"{i0['url']}: skipped, no post id"); continue
        used += 1; out.append((fix, proposal_for(site_id, site, fix, ctx, pid), grp, cost))
    return out, notes

def _only_issue_fields(fix, codes):
    """One title_meta fix may cover a title issue and a meta issue of the same URL; only the fields named by the open issues are changed."""
    codes = set(codes); old, new = json.loads(fix["before"]), json.loads(fix["after"])
    if not codes & {"title_missing", "title_long"}: new["title"] = old["title"]
    if not codes & {"meta_missing", "meta_long"}: new["meta"] = old["meta"]
    return {**fix, "after": json.dumps(new, ensure_ascii=False)}

def run(site_id, maxn=None, dry=False, fetcher=None, askf=None, client=None, sleep=time.sleep):
    from . import run as runmod
    cfg = runmod.sites()[site_id]; row = (sb.select("sites", id=f"eq.{site_id}") or [{}])[0]; site = {**cfg, **{k: v for k, v in row.items() if v not in (None, [], "")}}; site["id"] = site_id
    if site.get("mode") == "paused": return print("site paused from dashboard")
    w = client
    if w is None:
        from . import wp; w = wp.client(site)
    if not dry:
        apply_rollbacks(site_id, w); sync(site_id, fetcher, sleep)
    issues = select_issues(site_id)
    made, notes = build(site_id, site, issues, w, fixer_max(maxn), fetcher, askf, dry)
    for n in notes[:40]: print(" -", n)
    nprop = nauto = nman = 0
    for fix, prop, grp, cost in made:
        if dry:
            print(json.dumps({k: fix[k] for k in ("fix_type", "risk", "code", "target_url", "before", "after", "confidence", "source")}, ensure_ascii=False)[:600]); continue
        ins = None
        if prop: ins = sb.insert("proposals", [prop])[0]; nprop += 1; nauto += prop["status"] == "approved"
        else: nman += 1
        sb.insert("tech_fixes", fix_rows(site_id, grp, fix, ins, cost))
    if not dry:
        apply_all(site_id, site, w, fetcher, sleep); sync(site_id, fetcher, sleep)
    print(f"{site_id}: open issues with a fix path: {len(issues)} | proposals: {nprop} (auto-approved {nauto}) | manual instructions: {nman}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a: raise SystemExit("usage: python -m seo_agent.technical_fixer <site_id|all> [--max N] [--dry]")
    from . import run as _r
    mx = int(a[a.index("--max") + 1]) if "--max" in a else None
    _r.each(a[0], lambda sid: run(sid, mx, "--dry" in a), kinds=("wordpress", "static", "html"))
