"""Technical SEO audit (read-only, public fetches only). Never changes a site; it only writes findings to Supabase table tech_issues.
Covers what audit.py / run.audit_all do NOT: robots.txt, sitemap, HTTPS / www, status codes, redirect chains, noindex, canonical (incl. other domain),
duplicate / missing title + meta, H1, viewport (mobile), mixed content, hreflang, JSON-LD errors, JS-rendering risk, orphan pages, broken internal links,
and Core Web Vitals through the PageSpeed Insights API (only if env PSI_API_KEY is set; skipped otherwise).
Run:  python -m seo_agent.tech_audit <site_id|all>            (saves to Supabase)
      python -m seo_agent.tech_audit --dry --base https://example.com   (print only, no database, no sites.yaml needed)
Env:  TECH_MAX_PAGES (default 150: pages crawled from the sitemap), PSI_API_KEY (optional), TECH_PSI_PAGES (default 3)."""
import os, re, sys, json, html, datetime as dt
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser
import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (compatible; SEOAgentTechAudit/1.0; read-only)", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
REDIR = (301, 302, 303, 307, 308)
SEV = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}

# code -> (severity, issue title, why it matters, effort). The exact fix is built per URL in F() below.
CAT = {
 "https_down": ("Critical", "HTTPS version of the site does not load", "Google and users need a working secure version.", "Medium"),
 "http_no_redirect": ("High", "http:// version does not redirect to https://", "Two copies of every page can be indexed and the secure signal is weak.", "Low"),
 "host_variants": ("High", "www and non-www both load without redirecting to one", "Google may see two duplicate sites and split ranking signals.", "Low"),
 "robots_blocks_all": ("Critical", "robots.txt blocks the whole site", "Googlebot is told not to crawl anything, so pages can drop from search.", "Low"),
 "robots_blocks_assets": ("Medium", "robots.txt blocks CSS/JS folders", "Google cannot render the page like a user, which can hurt mobile and quality evaluation.", "Low"),
 "robots_missing": ("Low", "robots.txt not found", "Not fatal, but it is the normal place to point Google to the sitemap.", "Low"),
 "robots_no_sitemap": ("Low", "robots.txt has no Sitemap line", "Helps Google find the sitemap without Search Console.", "Low"),
 "sitemap_missing": ("High", "sitemap.xml missing or unreadable", "Google finds new and changed pages slower without a sitemap.", "Low"),
 "sitemap_empty": ("High", "sitemap has no page URLs", "The sitemap gives Google no information.", "Low"),
 "sitemap_other_host": ("Medium", "Sitemap lists URLs from another domain", "Those URLs are ignored and it hints at a wrong site URL setting.", "Low"),
 "sitemap_too_big": ("Medium", "Sitemap file has more than 50,000 URLs", "Google reads at most 50,000 URLs per sitemap file.", "Medium"),
 "sitemap_blocked": ("High", "Sitemap URL is blocked by robots.txt", "A URL that is both submitted and blocked sends Google a mixed signal.", "Low"),
 "status_5xx": ("Critical", "Page returns a server error (5xx)", "Google cannot index the page and repeated errors reduce crawling.", "Medium"),
 "status_4xx": ("High", "Page in sitemap returns 404/4xx", "A listed page that does not exist wastes crawl budget and is a bad user experience.", "Low"),
 "redirect_in_sitemap": ("Medium", "Sitemap lists a URL that redirects", "Sitemaps should only contain final 200 URLs.", "Low"),
 "redirect_chain": ("Medium", "Redirect chain (2 or more hops)", "Each hop slows the page and leaks ranking signals.", "Low"),
 "redirect_temp": ("Low", "Temporary redirect (302/307) used on a page in the sitemap", "A permanent move should use 301/308 so Google transfers signals.", "Low"),
 "redirect_loop": ("Critical", "Redirect loop or too many redirects", "The page can never load.", "Medium"),
 "noindex": ("Critical", "Page in sitemap is set to noindex", "Google will remove or never index this page.", "Low"),
 "canonical_other_domain": ("Critical", "Canonical points to a different domain", "Google is told another website is the original, so this page may not rank at all.", "Low"),
 "canonical_other_url": ("High", "Canonical points to a different page on the same site", "Google may ignore this page and rank the other one instead.", "Low"),
 "canonical_http": ("High", "Canonical uses http:// on an https site", "Conflicts with the secure version and can split signals.", "Low"),
 "canonical_missing": ("Medium", "Canonical tag missing", "Without it Google guesses the main URL when duplicates (www, ?utm, trailing slash) exist.", "Low"),
 "canonical_multiple": ("High", "More than one canonical tag", "Google ignores conflicting canonicals.", "Low"),
 "canonical_relative": ("Low", "Canonical is a relative URL", "Valid but error-prone; absolute URLs are safer.", "Low"),
 "title_missing": ("High", "Title tag missing or empty", "Title is the main ranking and click signal.", "Low"),
 "title_duplicate": ("Medium", "Duplicate title across pages", "Google cannot tell the pages apart and they can compete with each other.", "Medium"),
 "title_long": ("Low", "Title longer than 65 characters", "Likely cut off in search results.", "Low"),
 "meta_missing": ("Medium", "Meta description missing", "Google makes up its own snippet, usually with a lower click rate.", "Low"),
 "meta_duplicate": ("Medium", "Duplicate meta description across pages", "Weak, repeated snippets lower click rate.", "Medium"),
 "meta_long": ("Low", "Meta description longer than 160 characters", "Likely cut off in search results.", "Low"),
 "h1_missing": ("Medium", "No H1 heading", "H1 tells Google and users what the page is about.", "Low"),
 "h1_multiple": ("Low", "More than one H1", "Not an error for Google, but a clear single H1 is cleaner.", "Low"),
 "viewport_missing": ("High", "No mobile viewport meta tag", "Google indexes the mobile version; without it the page can render as a tiny desktop page.", "Low"),
 "mixed_content": ("Medium", "http:// resources loaded on an https page", "Browsers block or warn about them, which breaks the page and the secure signal.", "Low"),
 "schema_invalid": ("Medium", "Structured data (JSON-LD) is not valid JSON", "Google cannot read it, so no rich results.", "Low"),
 "schema_review": ("Medium", "Self-served Review/AggregateRating schema", "Google's guidelines do not allow self-serving review markup; risk of manual action.", "Low"),
 "hreflang_bad": ("Low", "hreflang tags incomplete", "Wrong hreflang is ignored; only matters if you serve several languages/regions.", "Medium"),
 "js_thin": ("Medium", "Almost no text in raw HTML (possible JavaScript rendering problem)", "If content only appears after JavaScript, Google may index it late or not at all.", "High"),
 "orphan": ("Medium", "Orphan page (in sitemap, no internal link points to it)", "Pages without internal links get less crawling and less ranking power.", "Low"),
 "broken_link": ("High", "Internal link points to a broken page", "Users and Google hit a dead end.", "Low"),
 "cwv_lcp": ("High", "Slow Largest Contentful Paint (LCP)", "Slow loading hurts user experience and is a Google page experience signal.", "High"),
 "cwv_cls": ("Medium", "Layout shifts while loading (CLS)", "Content jumping around hurts user experience.", "Medium"),
 "cwv_inp": ("Medium", "Slow interaction response (INP)", "Slow taps/clicks hurt user experience.", "High"),
}


def F(code, url, site):
    """Exact fix text for one issue on one URL, in the wording that fits the site type (WordPress or static HTML)."""
    static = site.get("type") in ("static", "html"); where = "in the page's <head> (edit the HTML file)" if static else "in AIOSEO > Search Appearance / Advanced, or the page's SEO settings"
    return {
     "https_down": "Fix the SSL certificate / hosting HTTPS settings, then confirm https:// loads with no browser warning.",
     "http_no_redirect": "Add a sitewide 301 http->https. Apache .htaccess: RewriteEngine On\\nRewriteCond %{HTTPS} off\\nRewriteRule ^(.*)$ https://%{HTTP_HOST}/$1 [L,R=301]",
     "host_variants": "Pick one host (the one in Search Console) and 301 the other to it; set the same in WordPress Settings > General (or in .htaccess).",
     "robots_blocks_all": "Remove 'Disallow: /' for User-agent: * (if the site is live). A normal file: User-agent: *\\nDisallow:\\nSitemap: " + urljoin(url, "/sitemap.xml"),
     "robots_blocks_assets": "Remove Disallow rules for /wp-content/, /wp-includes/ or *.css/*.js; Google needs them to render pages.",
     "robots_missing": "Create /robots.txt: User-agent: *\\nDisallow:\\nSitemap: " + urljoin(url, "/sitemap.xml"),
     "robots_no_sitemap": "Add this line to robots.txt: Sitemap: " + urljoin(url, "/sitemap.xml"),
     "sitemap_missing": "Generate sitemap.xml (AIOSEO > Sitemaps for WordPress; for static HTML create it by hand or via the agent) and submit it in Search Console > Sitemaps.",
     "sitemap_empty": "Make sure the sitemap lists the public pages as <url><loc>https://...</loc></url>.",
     "sitemap_other_host": "Fix the site URL setting so the sitemap lists only URLs on your own domain.",
     "sitemap_too_big": "Split into several sitemaps with a sitemap index file (max 50,000 URLs each).",
     "sitemap_blocked": "Allow this URL in robots.txt or remove it from the sitemap.",
     "status_5xx": "Check server/error logs for this URL; fix the PHP/plugin/hosting error, then re-test.",
     "status_4xx": "Either restore the page, or remove it from the sitemap and 301 it to the closest relevant page (needs approval before changing).",
     "redirect_in_sitemap": "Replace this URL in the sitemap with its final destination URL.",
     "redirect_chain": "Point the first URL directly to the final URL with one 301 and update internal links to the final URL.",
     "redirect_temp": "If the move is permanent change 302/307 to 301/308.",
     "redirect_loop": "Fix the redirect rules (.htaccess / plugin) so the URL ends at one 200 page.",
     "noindex": "If this page should rank, remove the noindex: delete <meta name=\"robots\" content=\"noindex\"> / the X-Robots-Tag header " + where + ". If it should not rank, remove it from the sitemap.",
     "canonical_other_domain": "Set the canonical to this page's own URL: <link rel=\"canonical\" href=\"" + url + "\"> " + where + ". Do this for every page copied from another site/template.",
     "canonical_other_url": "If this page should rank on its own use <link rel=\"canonical\" href=\"" + url + "\">; otherwise keep it and remove this URL from the sitemap.",
     "canonical_http": "Change canonical to the https URL: <link rel=\"canonical\" href=\"" + url.replace("http://", "https://") + "\">",
     "canonical_missing": "Add <link rel=\"canonical\" href=\"" + url + "\"> inside <head> " + where + ".",
     "canonical_multiple": "Keep exactly one <link rel=\"canonical\"> (usually an SEO plugin and the theme both add one).",
     "canonical_relative": "Use the full URL: <link rel=\"canonical\" href=\"" + url + "\">",
     "title_missing": "Add a unique <title> of about 30-60 characters that includes the page topic.",
     "title_duplicate": "Write a unique title per page (about 30-60 characters).",
     "title_long": "Shorten the title to about 60 characters, keeping the main topic first.",
     "meta_missing": "Add <meta name=\"description\" content=\"...\"> of about 120-155 characters, written for the reader.",
     "meta_duplicate": "Write a unique meta description per page.",
     "meta_long": "Shorten the meta description to about 155 characters.",
     "h1_missing": "Add one <h1> with the page's main topic.",
     "h1_multiple": "Keep one <h1>; change the others to <h2>.",
     "viewport_missing": "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> in <head>.",
     "mixed_content": "Change the http:// resource URLs to https:// (or relative URLs).",
     "schema_invalid": "Validate the JSON-LD in Google's Rich Results Test and fix the syntax (quotes, commas).",
     "schema_review": "Remove Review/AggregateRating markup that describes your own business unless it follows Google's review snippet guidelines.",
     "hreflang_bad": "Each hreflang set needs a self-referencing absolute URL, return links from the other pages and an x-default. If the site is single-language, remove hreflang.",
     "js_thin": "Check the page with Search Console > URL Inspection > View crawled page. If content is missing, render it on the server (SSR/static HTML).",
     "orphan": "Add at least one contextual link to this page from a relevant page (needs approval; the Content agent can propose the anchor).",
     "broken_link": "Update the link to a working URL or remove it.",
     "cwv_lcp": "Compress/resize the hero image (WebP, width/height set), preload it, enable caching and a CDN, defer non-critical JS.",
     "cwv_cls": "Set width and height on images and embeds; reserve space for banners/popups; avoid inserting content above existing content.",
     "cwv_inp": "Reduce/defer third-party scripts and heavy JS; break up long tasks.",
    }[code]


def issue(code, url, detail, site, sev=None):
    s, title, why, effort = CAT[code]
    return {"code": code, "url": url, "issue": title, "severity": sev or s, "detail": detail[:500], "why": why, "fix": F(code, url, site), "effort": effort}


# ---------- network ----------
def http_get(url, timeout=25):
    """One request, no redirect following (so redirects can be measured). Tests replace this function."""
    return requests.get(url, headers=UA, timeout=timeout, allow_redirects=False)


def follow(url, max_hops=8):
    """Returns (hops [(url,status)], final_response or None, loop_or_too_many, error)."""
    hops, seen, cur, r = [], set(), url, None
    for _ in range(max_hops + 1):
        if cur in seen: return hops, r, True, ""
        seen.add(cur)
        try: r = http_get(cur)
        except requests.RequestException as e: return hops, None, False, str(e)[:120]
        hops.append((cur, r.status_code))
        loc = r.headers.get("Location")
        if r.status_code in REDIR and loc: cur = urljoin(cur, loc)
        else: return hops, r, False, ""
    return hops, r, True, ""


def bare(h): return (h or "").lower().replace("www.", "")
def norm(u): return (u or "").split("#")[0].rstrip("/").lower()


# ---------- site level ----------
def robots_checks(base, site):
    out, rp, sitemaps = [], None, []
    try: r = http_get(urljoin(base, "/robots.txt"))
    except requests.RequestException as e: return [issue("robots_missing", base, "could not fetch: " + str(e)[:80], site)], None, []
    if r.status_code != 200:
        return [issue("robots_missing", urljoin(base, "/robots.txt"), f"HTTP {r.status_code}", site)], None, []
    txt = r.text; rp = RobotFileParser(); rp.parse(txt.splitlines())
    sitemaps = [m.strip() for m in re.findall(r"(?im)^\s*sitemap:\s*(\S+)", txt)]
    ua = None; blocked = []
    for line in txt.splitlines():
        line = line.split("#")[0].strip()
        if not line: continue
        k, _, v = line.partition(":"); k, v = k.strip().lower(), v.strip()
        if k == "user-agent": ua = v.lower()
        elif k == "disallow" and ua in ("*", "googlebot"):
            if v == "/": out.append(issue("robots_blocks_all", urljoin(base, "/robots.txt"), f"User-agent {ua}: Disallow: /", site))
            elif re.search(r"(?i)/wp-content/(?!uploads)|/wp-includes/|\.css|\.js\b|/assets/|/static/", v): blocked.append(v)
    if blocked: out.append(issue("robots_blocks_assets", urljoin(base, "/robots.txt"), "Disallow: " + ", ".join(sorted(set(blocked))[:6]), site))
    if not sitemaps: out.append(issue("robots_no_sitemap", urljoin(base, "/robots.txt"), "no Sitemap: line", site))
    return out, rp, sitemaps


def read_sitemaps(base, hint, site, limit_files=25):
    """Returns (urls, issues). Follows sitemap index files."""
    out, urls, queue, seen = [], [], list(dict.fromkeys(hint + [urljoin(base, "/sitemap.xml")])), set()
    first_ok = False
    while queue and len(seen) < limit_files:
        sm = queue.pop(0)
        if sm in seen: continue
        seen.add(sm)
        try: r = http_get(sm)
        except requests.RequestException: continue
        if r.status_code in REDIR and r.headers.get("Location"): queue.insert(0, urljoin(sm, r.headers["Location"])); continue
        if r.status_code != 200 or "<loc" not in r.text.lower(): continue
        first_ok = True; locs = [html.unescape(x).strip() for x in re.findall(r"<loc>\s*(.*?)\s*</loc>", r.text, re.S | re.I)]
        if re.search(r"<sitemapindex", r.text, re.I): queue += locs
        else:
            if len(locs) > 50000: out.append(issue("sitemap_too_big", sm, f"{len(locs)} URLs", site))
            urls += locs
    if not first_ok: out.append(issue("sitemap_missing", urljoin(base, "/sitemap.xml"), "no readable sitemap at /sitemap.xml or in robots.txt", site))
    elif not urls: out.append(issue("sitemap_empty", urljoin(base, "/sitemap.xml"), "sitemap read but no <loc> page URLs", site))
    urls = [u for u in dict.fromkeys(urls) if not re.search(r"\.(jpe?g|png|webp|gif|svg|pdf|xml)(\?|$)", u, re.I)]
    foreign = [u for u in urls if bare(urlparse(u).netloc) != bare(urlparse(base).netloc)]
    if foreign: out.append(issue("sitemap_other_host", foreign[0], f"{len(foreign)} URL(s) on another domain, e.g. {foreign[0]}", site))
    return [u for u in urls if u not in foreign], out


def wp_fallback_urls(base):
    """Public WordPress REST list, used only when no sitemap could be read."""
    out = []
    for kind in ("pages", "posts"):
        try:
            r = requests.get(f"{base.rstrip('/')}/wp-json/wp/v2/{kind}", params={"per_page": 100, "_fields": "link"}, headers=UA, timeout=25)
            if r.ok: out += [x["link"] for x in r.json()]
        except Exception: pass
    return out


def https_checks(base, site):
    out, p = [], urlparse(base); host = p.netloc
    hops, r, loop, err = follow(f"https://{host}/")
    if r is None or r.status_code >= 500 or (r.status_code >= 400 and not hops[:-1]): out.append(issue("https_down", f"https://{host}/", err or f"HTTP {r.status_code}", site)); return out
    hops2, r2, _, _ = follow(f"http://{host}/")
    if r2 is not None and hops2 and not hops2[-1][0].startswith("https://"): out.append(issue("http_no_redirect", f"http://{host}/", f"http:// answers {r2.status_code} and stays on http", site))
    other = host[4:] if host.startswith("www.") else "www." + host
    h3, r3, _, _ = follow(f"https://{other}/")
    if r3 is not None and r3.status_code == 200 and h3 and bare(urlparse(h3[-1][0]).netloc) == bare(host) and urlparse(h3[-1][0]).netloc != host:
        out.append(issue("host_variants", f"https://{other}/", f"https://{other}/ ends on {h3[-1][0]} with no redirect to https://{host}/", site))
    elif r3 is not None and r3.status_code == 200 and h3 and len(h3) == 1:
        out.append(issue("host_variants", f"https://{other}/", f"https://{other}/ answers 200 itself (no redirect to https://{host}/)", site))
    return out


# ---------- page level ----------
def analyze(url, hops, r, loop, err, site, base):
    """Returns (issues, record). record holds what the cross-page checks need."""
    out, rec = [], {"url": url, "status": None, "final": url, "links": set()}
    if err and not hops: rec["status"] = 0; out.append(issue("status_5xx", url, "request failed: " + err, site)); return out, rec
    if loop: out.append(issue("redirect_loop", url, " -> ".join(f"{u} [{s}]" for u, s in hops)[:300], site)); rec["status"] = -1; return out, rec
    rec["status"] = hops[-1][1]; rec["final"] = hops[-1][0]
    if len(hops) > 1:
        chain = " -> ".join(f"{u} [{s}]" for u, s in hops)
        out.append(issue("redirect_in_sitemap", url, chain[:300], site))
        if len(hops) > 2: out.append(issue("redirect_chain", url, f"{len(hops) - 1} hops: {chain}"[:400], site))
        if hops[0][1] in (302, 303, 307): out.append(issue("redirect_temp", url, f"first hop is {hops[0][1]}", site))
    s = rec["status"]
    if s >= 500: out.append(issue("status_5xx", url, f"HTTP {s}", site)); return out, rec
    if s >= 400: out.append(issue("status_4xx", url, f"HTTP {s}", site)); return out, rec
    if r is None or s != 200: return out, rec
    final = rec["final"]; hdr = (r.headers.get("X-Robots-Tag") or "").lower()
    soup = BeautifulSoup(r.text, "html.parser"); head = soup.head or soup
    robots = " ".join((m.get("content") or "").lower() for m in soup.find_all("meta", attrs={"name": re.compile(r"^(robots|googlebot)$", re.I)}))
    rec["noindex"] = "noindex" in robots or "noindex" in hdr
    if rec["noindex"]: out.append(issue("noindex", url, "meta robots: " + (robots or "-") + (" | X-Robots-Tag: " + hdr if hdr else ""), site))
    cans = [l.get("href", "").strip() for l in soup.find_all("link", rel=lambda v: v and "canonical" in (v if isinstance(v, list) else [v]))]
    rec["canonical"] = cans[0] if cans else ""
    if not cans: out.append(issue("canonical_missing", url, "no <link rel=canonical>", site))
    else:
        if len(set(cans)) > 1: out.append(issue("canonical_multiple", url, "found: " + " | ".join(cans[:4]), site))
        c = cans[0]; absu = urljoin(final, c); pc = urlparse(absu)
        if not urlparse(c).netloc: out.append(issue("canonical_relative", url, f"canonical=\"{c}\"", site))
        if bare(pc.netloc) != bare(urlparse(final).netloc): out.append(issue("canonical_other_domain", url, f"canonical -> {absu}", site))
        elif pc.scheme == "http" and urlparse(final).scheme == "https": out.append(issue("canonical_http", url, f"canonical -> {absu}", site))
        elif norm(absu.split("?")[0]) != norm(final.split("?")[0]): out.append(issue("canonical_other_url", url, f"canonical -> {absu}", site))
        rec["canonical_abs"] = absu
    t = soup.title.get_text(" ", strip=True) if soup.title else ""
    md = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)}); m = (md.get("content") if md else "") or ""; m = m.strip()
    rec["title"], rec["meta"] = t, m
    if not t: out.append(issue("title_missing", url, "empty/missing <title>", site))
    elif len(t) > 65: out.append(issue("title_long", url, f"{len(t)} chars: {t[:90]}", site))
    if not m: out.append(issue("meta_missing", url, "no meta description", site))
    elif len(m) > 160: out.append(issue("meta_long", url, f"{len(m)} chars", site))
    h1 = soup.find_all("h1")
    if not h1: out.append(issue("h1_missing", url, "0 <h1> in raw HTML", site))
    elif len(h1) > 1: out.append(issue("h1_multiple", url, f"{len(h1)} <h1>", site))
    if not soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)}): out.append(issue("viewport_missing", url, "no viewport meta", site))
    if urlparse(final).scheme == "https":
        bad = [x.get(a) for tag, a in (("script", "src"), ("img", "src"), ("iframe", "src")) for x in soup.find_all(tag) if (x.get(a) or "").startswith("http://")]
        bad += [x.get("href") for x in soup.find_all("link", rel=lambda v: v and "stylesheet" in (v if isinstance(v, list) else [v])) if (x.get("href") or "").startswith("http://")]
        if bad: out.append(issue("mixed_content", url, f"{len(bad)} resource(s), e.g. {bad[0]}", site))
    for sc in soup.find_all("script", type="application/ld+json"):
        raw = sc.string or sc.get_text() or ""
        try: d = json.loads(raw)
        except ValueError: out.append(issue("schema_invalid", url, "JSON-LD parse error: " + raw.strip()[:80], site)); continue
        if re.search(r'"@type"\s*:\s*"?(\[?[^\]},]*)(AggregateRating|Review)\b', json.dumps(d)): out.append(issue("schema_review", url, "Review/AggregateRating found in JSON-LD", site))
    hl = {l.get("hreflang"): l.get("href", "") for l in soup.find_all("link", rel="alternate") if l.get("hreflang")}
    if hl:
        probs = []
        if "x-default" not in hl: probs.append("no x-default")
        if not any(norm(urljoin(final, v)) == norm(final) for v in hl.values()): probs.append("no self-reference")
        if any(not urlparse(v).netloc for v in hl.values()): probs.append("relative href")
        if probs: out.append(issue("hreflang_bad", url, ", ".join(probs) + f" ({len(hl)} tags)", site))
    body = BeautifulSoup(r.text, "html.parser")
    for tg in body(["script", "style", "noscript"]): tg.decompose()
    words = len(re.sub(r"\s+", " ", body.get_text(" ")).split())
    if words < 60 and (soup.find(id=re.compile(r"^(root|__next|app|__nuxt)$")) or len(soup.find_all("script")) >= 5):
        out.append(issue("js_thin", url, f"only {words} words in raw HTML, {len(soup.find_all('script'))} script tags", site))
    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        if h.startswith(("#", "mailto:", "tel:", "javascript:")): continue
        u = urljoin(final, h).split("#")[0]
        if bare(urlparse(u).netloc) == bare(urlparse(base).netloc) and not re.search(r"\.(jpe?g|png|webp|gif|svg|pdf|zip|css|js)(\?|$)", u, re.I): rec["links"].add(u)
    return out, rec


def cross_checks(recs, urls, full, site):
    out = []
    for key, code in (("title", "title_duplicate"), ("meta", "meta_duplicate")):
        g = defaultdict(list)
        for r in recs:
            if r["status"] == 200 and r.get(key) and not r.get("noindex") and norm(r.get("canonical_abs", r["final"])) == norm(r["final"]): g[r[key].strip().lower()].append(r["url"])
        for k, us in g.items():
            if len(us) > 1:
                for u in us: out.append(issue(code, u, f"same {key} on {len(us)} pages: “{k[:80]}”; also {[x for x in us if x != u][0]}", site))
    status = {norm(r["url"]): r for r in recs}; status.update({norm(r["final"]): r for r in recs})
    seen = set()
    for r in recs:
        for l in r["links"]:
            t = status.get(norm(l))
            if t and t["status"] and (t["status"] >= 400) and (r["url"], l) not in seen:
                seen.add((r["url"], l)); out.append(issue("broken_link", r["url"], f"links to {l} which returns {t['status']}", site))
    if full:   # orphan check is only valid when every sitemap URL was crawled
        inbound = defaultdict(set)
        for r in recs:
            for l in r["links"]: inbound[norm(l)].add(norm(r["url"]))
        home = {norm(u) for u in urls[:1]} | {norm(site.get("base", ""))}
        for r in recs:
            if r["status"] == 200 and norm(r["final"]) not in home and not (inbound.get(norm(r["url"]), set()) - {norm(r["url"])}) and not (inbound.get(norm(r["final"]), set()) - {norm(r["final"])}):
                out.append(issue("orphan", r["url"], "0 internal links from the other crawled sitemap pages", site))
    return out


# ---------- Core Web Vitals (optional) ----------
def psi(url, key, site):
    try:
        r = requests.get("https://www.googleapis.com/pagespeedonline/v5/runPagespeed", params={"url": url, "strategy": "mobile", "category": "performance", "key": key}, timeout=120)
        if not r.ok: return [], f"PSI {r.status_code} for {url}"
        d = r.json()
    except (requests.RequestException, ValueError) as e: return [], f"PSI failed for {url}: {str(e)[:80]}"
    fm = (d.get("loadingExperience") or {}).get("metrics") or {}; lab = (d.get("lighthouseResult") or {}).get("audits") or {}; out = []
    def val(fkey, lkey, div=1):
        if fkey in fm and fm[fkey].get("percentile") is not None: return fm[fkey]["percentile"] / div, "field data (real users, 28 days)"
        if lkey and lkey in lab and lab[lkey].get("numericValue") is not None: return lab[lkey]["numericValue"] / (div if lkey != "cumulative-layout-shift" else 1), "lab data (single test)"
        return None, ""
    lcp, src = val("LARGEST_CONTENTFUL_PAINT_MS", "largest-contentful-paint", 1000)
    if lcp is not None and lcp > 2.5: out.append(issue("cwv_lcp", url, f"LCP {lcp:.1f}s, {src}, mobile; good is 2.5s or less", site, "Critical" if lcp > 6 else "High" if lcp > 4 else "Medium"))
    cls, src = val("CUMULATIVE_LAYOUT_SHIFT_SCORE", "cumulative-layout-shift", 100)
    if cls is not None and cls > 0.1: out.append(issue("cwv_cls", url, f"CLS {cls:.2f}, {src}, mobile; good is 0.1 or less", site, "High" if cls > 0.25 else "Medium"))
    inp, src = val("INTERACTION_TO_NEXT_PAINT", None)
    if inp is not None and inp > 200: out.append(issue("cwv_inp", url, f"INP {inp:.0f}ms, {src}; good is 200ms or less", site, "High" if inp > 500 else "Medium"))
    return out, ""


# ---------- orchestration ----------
def audit_site(base, site, max_pages=None, psi_key=None):
    """Full read-only audit of one site. Returns (issues, notes)."""
    max_pages = max_pages or int(os.environ.get("TECH_MAX_PAGES", "150")); base = base.rstrip("/"); site = {**site, "base": base + "/"}
    issues, notes = https_checks(base, site), []
    if any(i["code"] == "https_down" for i in issues):   # nothing else can be trusted; also avoids false findings when the runner itself has no network
        return issues, ["https:// home page could not be fetched, so all other checks were skipped. Confirm from a normal browser before acting: if the site loads there, the problem is the runner's network, not the site."]
    ri, rp, smaps = robots_checks(base, site); issues += ri
    urls, si = read_sitemaps(base, smaps, site); issues += si
    if not urls:
        urls = [u for u in dict.fromkeys(wp_fallback_urls(base))] if site.get("type", "wordpress") == "wordpress" else []
        if urls: notes.append("sitemap unreadable; used the public WordPress REST list instead")
    if not urls: urls = [base + "/"]; notes.append("no page list found: only the home page was checked")
    full = len(urls) <= max_pages; sample = urls[:max_pages]
    if not full: notes.append(f"sitemap has {len(urls)} URLs; first {max_pages} crawled, orphan check skipped (needs a full crawl)")
    if rp:
        for u in sample:
            if not rp.can_fetch("Googlebot", u): issues.append(issue("sitemap_blocked", u, "robots.txt disallows Googlebot", site))
    def one(u):
        hops, r, loop, err = follow(u); return analyze(u, hops, r, loop, err, site, base)
    with ThreadPoolExecutor(6) as ex: res = list(ex.map(one, sample))
    recs = []
    for o, rec in res: issues += o; recs.append(rec)
    issues += cross_checks(recs, urls, full, site)
    notes.append(f"pages crawled: {len(sample)} of {len(urls)} in sitemap")
    if psi_key:
        for u in sample[:int(os.environ.get("TECH_PSI_PAGES", "3"))]:
            o, n = psi(u, psi_key, site); issues += o
            if n: notes.append(n)
    else: notes.append("Core Web Vitals not checked: PSI_API_KEY not set (DATA NOT AVAILABLE)")
    seen, uniq = set(), []
    for i in issues:
        k = (i["code"], i["url"], i["detail"])
        if k not in seen: seen.add(k); uniq.append(i)
    return sorted(uniq, key=lambda i: (SEV[i["severity"]], i["code"], i["url"])), notes


def summarize(issues):
    """Group by issue: [{issue, severity, count, examples[5], why, fix, effort}] sorted by severity, then count. Same shape the dashboard shows."""
    g = defaultdict(list)
    for i in issues: g[i["code"]].append(i)
    rows = [{"issue": v[0]["issue"], "code": c, "severity": v[0]["severity"], "count": len(v), "examples": [x["url"] for x in v[:5]], "why": v[0]["why"], "fix": v[0]["fix"], "effort": v[0]["effort"]} for c, v in g.items()]
    return sorted(rows, key=lambda r: (SEV[r["severity"]], -r["count"]))


def run(site_id):
    from . import run as R, sb
    site = R.sites()[site_id]; base = (site.get("wp_base") or "https://" + site["domain"]).rstrip("/")
    issues, notes = audit_site(base, site, psi_key=os.environ.get("PSI_API_KEY"))
    for n in notes: print("note:", n)
    ignored = {(x["url"], x["issue"]) for x in sb.select("tech_issues", site_id=f"eq.{site_id}", status="eq.ignored", select="url,issue")}
    sb.update("tech_issues", {"site_id": site_id, "status": "open"}, {"status": "superseded"})   # old open findings are replaced by this run
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    rows = [{"site_id": site_id, "url": i["url"], "code": i["code"], "issue": i["issue"], "severity": i["severity"], "detail": i["detail"], "why": i["why"],
             "fix": i["fix"], "effort": i["effort"], "found_at": now, "status": "open"} for i in issues if (i["url"], i["issue"]) not in ignored]
    for k in range(0, len(rows), 200): sb.insert("tech_issues", rows[k:k + 200])
    print(f"{site_id}: {len(rows)} issue rows saved;", ", ".join(f"{s['count']}x {s['issue']} ({s['severity']})" for s in summarize(issues)[:5]) or "no issues found")


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--dry" in a:
        base = a[a.index("--base") + 1] if "--base" in a else None
        if not base: raise SystemExit("--dry needs --base https://your-site")
        iss, notes = audit_site(base, {"type": "static" if "--static" in a else "wordpress"}, psi_key=os.environ.get("PSI_API_KEY"))
        print(json.dumps({"base": base, "notes": notes, "summary": summarize(iss), "issues": iss}, indent=1, ensure_ascii=False))
    else:
        from . import run as _r
        _r.each(a[0], run)
