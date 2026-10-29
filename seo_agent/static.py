"""Plain HTML sites over FTP / FTPS / SFTP. Same methods as the WordPress client, so every agent feature works the same way.
Secrets (GitHub): <PREFIX>_FTP_HOST, <PREFIX>_FTP_USER, <PREFIX>_FTP_PASSWORD.  Site columns: ftp_protocol, ftp_port, ftp_root, blog_path, template_url.
Every change keeps a backup under <root>/.sab/ and marks its edit with <!--sab:ID--> comments, so rollback removes exactly that edit."""
import os, re, io, json, time, html, zlib, posixpath, datetime as dt
from urllib.parse import urlparse, urljoin
import requests
from .wp import WP, secret

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
RISKY = re.compile(r"(?i)\b(guarantee[ds]?|100\s?%|no risk|risk[- ]free|cure[ds]?|miracle|best (?:doctor|surgeon|clinic)|number one|#1)\b")


# ---------- transport ----------
class Remote:
    def __init__(self, proto, host, user, pw, port=None):
        self.proto = (proto or "ftp").lower(); self.host, self.user, self.pw, self.port = host, user, pw, port; self.c = None; self._sftp = None
    def connect(self):
        if self.proto == "sftp":
            import paramiko
            t = paramiko.Transport((self.host, int(self.port or 22))); t.connect(username=self.user, password=self.pw); self._sftp = paramiko.SFTPClient.from_transport(t); return self
        import ftplib
        c = (ftplib.FTP_TLS if self.proto == "ftps" else ftplib.FTP)(); c.connect(self.host, int(self.port or 21), timeout=60); c.login(self.user, self.pw)
        if self.proto == "ftps": c.prot_p()
        c.set_pasv(True); self.c = c; return self
    def read(self, path):
        try:
            if self._sftp:
                with self._sftp.open(path, "rb") as f: return f.read()
            b = io.BytesIO(); self.c.retrbinary("RETR " + path, b.write); return b.getvalue()
        except Exception as e:
            if "550" in str(e) or "No such file" in str(e) or isinstance(e, FileNotFoundError) or "not found" in str(e).lower(): return None
            raise
    def mkdirs(self, d):
        parts = [p for p in d.split("/") if p]; cur = "/" if d.startswith("/") else ""
        for p in parts:
            cur = posixpath.join(cur, p)
            try:
                if self._sftp: self._sftp.stat(cur)
                else: self.c.cwd(cur); self.c.cwd("/")
            except Exception:
                (self._sftp.mkdir if self._sftp else self.c.mkd)(cur)
    def write(self, path, data):
        self.mkdirs(posixpath.dirname(path))
        if self._sftp:
            with self._sftp.open(path, "wb") as f: f.write(data)
        else: self.c.storbinary("STOR " + path, io.BytesIO(data))
    def listdir(self, d):
        try:
            if self._sftp:
                import stat
                return [(a.filename, stat.S_ISDIR(a.st_mode)) for a in self._sftp.listdir_attr(d)]
            out = []
            for n, f in self.c.mlsd(d, facts=["type"]):
                if f.get("type") in ("file", "dir"): out.append((n, f["type"] == "dir"))
            return out
        except Exception: return []
    def close(self):
        try: (self._sftp.close() if self._sftp else self.c.quit())
        except Exception: pass


# ---------- pure HTML edits (tested without a server) ----------
def _esc(s): return html.escape(s, quote=True)
TITLE = re.compile(r"<title[^>]*>.*?</title>", re.S | re.I)
def _meta_rx(attr, name): return re.compile(r'<meta\b(?=[^>]*\b%s=["\']%s["\'])[^>]*>' % (attr, re.escape(name)), re.I)
TAGS = {"title": TITLE, "meta": _meta_rx("name", "description"), "og_title": _meta_rx("property", "og:title"), "og_desc": _meta_rx("property", "og:description"),
        "tw_title": _meta_rx("name", "twitter:title"), "tw_desc": _meta_rx("name", "twitter:description")}
def get_tags(h): return {k: (m.group(0) if (m := rx.search(h)) else None) for k, rx in TAGS.items()}
def _set_content(tag, val):
    if re.search(r'\bcontent=["\']', tag, re.I): return re.sub(r'(\bcontent=)(["\']).*?\2', lambda m: f'{m.group(1)}"{_esc(val)}"', tag, count=1, flags=re.S | re.I)
    return tag.replace("<meta", f'<meta content="{_esc(val)}"', 1)
def put_tags(h, tags):
    for k, raw in tags.items():
        rx = TAGS[k]
        if raw is None: h = rx.sub("", h, count=1)
        elif rx.search(h): h = rx.sub(lambda m: raw, h, count=1)
        elif k == "meta": h = TITLE.sub(lambda m: m.group(0) + f"\n{raw}", h, count=1) if TITLE.search(h) else h.replace("</head>", raw + "\n</head>", 1)
    return h
def new_seo(h, title, meta):
    old = get_tags(h); new = {}
    new["title"] = TITLE.sub(lambda m: f"<title>{_esc(title)}</title>", old["title"] or "<title></title>", count=1)
    new["meta"] = _set_content(old["meta"], meta) if old["meta"] else f'<meta name="description" content="{_esc(meta)}">'
    for k, v in (("og_title", title), ("tw_title", title), ("og_desc", meta), ("tw_desc", meta)):
        if old[k]: new[k] = _set_content(old[k], v)
    return old, put_tags(h, new)

def _mark(mid, inner): return f"<!--sab:{mid}-->{inner}<!--/sab:{mid}-->"
def _bad_schema(o):
    if isinstance(o, dict):
        t = o.get("@type"); ts = t if isinstance(t, list) else [t]
        if any(isinstance(x, str) and re.search(r"(?i)rating|review", x) for x in ts) or any(re.search(r"(?i)rating|review", k) for k in o if k != "@type" and k.lower().startswith(("aggregate", "review", "rating"))): return True
        return any(_bad_schema(v) for v in o.values())
    if isinstance(o, list): return any(_bad_schema(v) for v in o)
    return False
def add_schema(h, mid, json_ld):
    data = json_ld if not isinstance(json_ld, str) else json.loads(json_ld)
    if _bad_schema(data): raise RuntimeError("Rating or Review schema is not allowed")
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    tag = _mark(mid, f'<script type="application/ld+json" data-sab="{mid}">{blob}</script>')
    if "</head>" not in h.lower(): raise RuntimeError("no </head> in this file")
    i = h.lower().rindex("</head>"); return h[:i] + tag + "\n" + h[i:]

SKIP = {"a", "script", "style", "title", "button", "textarea", "select", "option", "head", "h1", "h2", "h3", "h4", "h5", "h6", "svg", "noscript", "label"}
def add_link(h, mid, anchor, url, own_host):
    u = urlparse(url)
    if u.netloc and u.netloc.lower().replace("www.", "") != own_host.replace("www.", ""): raise RuntimeError("links to other sites are not allowed")
    body0 = re.search(r"<body\b[^>]*>", h, re.I); start = body0.end() if body0 else 0
    toks = re.split(r"(<!--.*?-->|<[^>]+>)", h[start:], flags=re.S); stack = []; rx = re.compile(re.escape(anchor.strip()), re.I)
    for n, t in enumerate(toks):
        if t.startswith("<!--"): continue
        if t.startswith("<"):
            m = re.match(r"<(/?)([a-zA-Z0-9]+)", t)
            if m and not t.endswith("/>") and m.group(2).lower() not in ("br", "img", "hr", "input", "meta", "link"):
                nm = m.group(2).lower()
                if m.group(1):
                    if nm in stack: del stack[len(stack) - 1 - stack[::-1].index(nm):]
                else: stack.append(nm)
            continue
        if SKIP & set(stack) or not t.strip(): continue
        m = rx.search(t)
        if m:
            toks[n] = t[:m.start()] + _mark(mid, f'<a href="{_esc(url)}" data-sab="{mid}">{m.group(0)}</a>') + t[m.end():]
            return h[:start] + "".join(toks)
    raise RuntimeError(f"'{anchor}' is not in normal body text (it may sit in a heading or an existing link)")

HEAD = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.S | re.I)
def _insert_at(h, pos, block): return h[:pos] + block + h[pos:]
def _wrapper_start(h, pos):
    m = re.search(r"<(section|div)\b[^>]*>\s*$", h[:pos], re.I); return m.start() if m else pos
def _end_pos(h):
    for pat in (r"</main>", r"</article>", r"<footer\b", r"</body>"):
        ms = list(re.finditer(pat, h, re.I))
        if ms: return ms[-1].start()
    return len(h)
def add_section(h, mid, section_html, position="before_faq"):
    block = _mark(mid, f'<div data-sab="{mid}">{section_html}</div>'); heads = [(m.start(), m.end(), int(m.group(1)), re.sub(r"<[^>]+>", "", m.group(2))) for m in HEAD.finditer(h)]
    faq = next((x for x in heads if re.search(r"(?i)\bfaq|frequently asked", x[3])), None)
    if position != "end" and faq:
        if position == "after_faq":
            nxt = next((x for x in heads if x[0] > faq[0] and x[2] <= faq[2]), None)
            pos = _wrapper_start(h, nxt[0]) if nxt else _end_pos(h)
        else: pos = _wrapper_start(h, faq[0])
    else: pos = _end_pos(h)
    return _insert_at(h, pos, "\n" + block + "\n")
def remove_mod(h, mid):
    rx = re.compile(r"<!--sab:%s-->(.*?)<!--/sab:%s-->" % (mid, mid), re.S)
    def f(m):
        inner = m.group(1); a = re.fullmatch(r'<a [^>]*data-sab="%s"[^>]*>(.*?)</a>' % mid, inner, re.S)
        return a.group(1) if a else ""
    new, n = rx.subn(f, h); return new, n > 0


# ---------- the client ----------
class Static(WP):
    static = True
    def __init__(self, site):
        self.site = site; self.base = site["wp_base"].rstrip("/"); self.auth = None; self.api = ""; self.host = urlparse(self.base).netloc.lower()
        p = site.get("secrets_prefix") or site["id"].upper()
        h, u, pw = secret(f"{p}_FTP_HOST"), secret(f"{p}_FTP_USER"), secret(f"{p}_FTP_PASSWORD")
        if not (h and u and pw): raise RuntimeError(f"Add the GitHub secrets {p}_FTP_HOST, {p}_FTP_USER and {p}_FTP_PASSWORD for this site")
        self.root = "/" + (site.get("ftp_root") or "public_html").strip("/"); self.r = Remote(site.get("ftp_protocol") or "ftp", h, u, pw, site.get("ftp_port")); self._on = False
        self.urls = {}; self.blog = (site.get("blog_path") or "blog").strip("/")
    def _rem(self):
        if not self._on: self.r.connect(); self._on = True
        return self.r
    # -- mapping between a page URL, an id and a file
    @staticmethod
    def pid(url): return zlib.crc32(("/" + urlparse(url).path.strip("/")).lower().encode())
    def _reg(self, url): p = self.pid(url); self.urls[p] = url; return p
    def _file(self, url):
        r = self._rem(); path = urlparse(url).path; seg = path.strip("/")
        cands = [f"{seg}/index.html", f"{seg}/index.htm", f"{seg}/index.php"] if (path.endswith("/") or not seg or "." not in posixpath.basename(seg)) else []
        cands += [seg] if seg and "." in posixpath.basename(seg) else [seg + ".html", seg + ".php", seg + ".htm"] if seg else []
        if not seg: cands = ["index.html", "index.htm", "index.php"]
        for c in cands:
            f = posixpath.join(self.root, c); d = r.read(f)
            if d is not None: return f, d
        raise RuntimeError(f"Could not find the file for {url} under {self.root} on the server (check the web-root folder).")
    def _url_of(self, pid):
        if pid not in self.urls: self.all_ids()
        if pid not in self.urls: raise RuntimeError("page id not found in the site's sitemap; run an audit first")
        return self.urls[pid]
    @staticmethod
    def _dec(b):
        try: return b.decode("utf-8"), "utf-8"
        except UnicodeDecodeError: return b.decode("latin-1"), "latin-1"
    def _load(self, pid):
        url = self._url_of(pid); f, b = self._file(url); h, enc = self._dec(b); return url, f, h, enc
    def _save(self, f, h, enc, orig_bytes, mid):
        r = self._rem(); r.write(posixpath.join(self.root, ".sab", f"{mid}.bak"), orig_bytes); r.write(f, h.encode(enc, "replace"))
    # -- read side
    def live(self, url):
        h = requests.get(url, params={"nc": os.urandom(3).hex()}, timeout=30, headers=UA).text
        g = lambda rx: (re.search(rx, h, re.S | re.I) or [None, ""])[1].strip()
        clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
        t = get_tags(h); meta = (re.search(r'content=["\'](.*?)["\']', t["meta"] or "", re.S) or [None, ""])[1]
        h2 = [clean(x) for x in re.findall(r"<h2[^>]*>(.*?)</h2>", h, re.S | re.I)]
        return {"title": html.unescape(g(r"<title[^>]*>(.*?)</title>")), "meta": html.unescape(meta), "h1": clean(g(r"<h1[^>]*>(.*?)</h1>")), "h2": [x for x in h2 if x][:25], "post_id": str(self._reg(url))}
    def modified(self, url):
        try:
            lm = requests.head(url, timeout=20, allow_redirects=True, headers=UA).headers.get("Last-Modified")
            if lm:
                import email.utils; return email.utils.parsedate_to_datetime(lm).astimezone(dt.timezone.utc).isoformat()
        except Exception: pass
        return None
    def all_ids(self):
        seen, queue, out = set(), [self.base + "/sitemap.xml"], []
        while queue and len(seen) < 40:
            sm = queue.pop(0)
            if sm in seen: continue
            seen.add(sm)
            try: x = requests.get(sm, timeout=30, headers=UA).text
            except requests.RequestException: continue
            for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", x, re.S):
                loc = html.unescape(loc)
                if loc.endswith(".xml"): queue.append(loc)
                elif urlparse(loc).netloc.lower().replace("www.", "") == self.host.replace("www.", "") and not re.search(r"\.(jpg|jpeg|png|webp|gif|pdf|svg)$", loc, re.I): out.append(loc)
        if not out: out = self._walk()
        return [self._reg(u) for u in dict.fromkeys(out)]
    def _walk(self, d=None, depth=0):
        r = self._rem(); d = d or self.root; out = []
        for n, isd in r.listdir(d):
            if n.startswith(".") or depth > 4: continue
            if isd: out += self._walk(posixpath.join(d, n), depth + 1)
            elif re.search(r"\.(html?|php)$", n, re.I):
                rel = posixpath.relpath(posixpath.join(d, n), self.root); out.append(self.base + "/" + re.sub(r"(^|/)index\.(html?|php)$", "", rel).rstrip("/") + ("/" if rel.endswith(("index.html", "index.htm", "index.php")) else ""))
        return out
    def audit(self, pid):
        url = self._url_of(pid); h = requests.get(url, params={"nc": os.urandom(3).hex()}, timeout=30, headers=UA).text
        from bs4 import BeautifulSoup
        s = BeautifulSoup(h, "html.parser"); sch = []
        for x in s.find_all("script", type="application/ld+json"):
            try: d = json.loads(x.string or ""); sch.append({"valid": True, "has_aggregate_rating": "aggregaterating" in json.dumps(d).lower()})
            except ValueError: sch.append({"valid": False, "has_aggregate_rating": False})
        for t in s(["script", "style", "noscript"]): t.decompose()
        text = re.sub(r"\s+", " ", s.get_text(" ")); imgs = s.find_all("img")
        return {"url": url, "h1": len(s.find_all("h1")), "h2": len(s.find_all("h2")), "h3": len(s.find_all("h3")), "words": len(text.split()), "images": len(imgs),
                "images_no_alt": sum(1 for i in imgs if not (i.get("alt") or "").strip()), "risky_claims": sorted({m.group(0).lower() for m in RISKY.finditer(text)}), "schema": sch}
    # -- write side
    def set_seo(self, pid, title, meta):
        url, f, h, enc = self._load(pid); orig = h.encode(enc, "replace"); old, new = new_seo(h, title, meta); mid = int(time.time() * 1000)
        self._save(f, new, enc, orig, mid); self._rem().write(posixpath.join(self.root, ".sab", f"{mid}.json"), json.dumps({"pid": pid, "tags": old}).encode())
        return {"backup_id": mid}
    def rollback(self, pid, backup_id):
        url, f, h, enc = self._load(pid); b = self._rem().read(posixpath.join(self.root, ".sab", f"{int(backup_id)}.json"))
        if b is None: raise RuntimeError("backup not found on the server")
        self._save(f, put_tags(h, json.loads(b)["tags"]), enc, h.encode(enc, "replace"), int(time.time() * 1000)); return {"ok": True}
    def add_mod(self, pid, kind, data):
        url, f, h, enc = self._load(pid); orig = h.encode(enc, "replace"); mid = int(time.time() * 1000)
        if kind == "schema": new = add_schema(h, mid, data["json_ld"])
        elif kind == "link": new = add_link(h, mid, data["anchor"], data["url"], self.host)
        elif kind == "section":
            from .content import lint
            probs = lint(data["html"], self.site.get("banned_terms") or [], self.site.get("profile") == "medical")
            if probs: raise RuntimeError("section rejected: " + "; ".join(probs[:3]))
            if re.search(r'<a\b[^>]*href=["\']https?://(?!%s)' % re.escape(self.host.replace("www.", "")), data["html"], re.I): raise RuntimeError("links to other sites are not allowed")
            new = add_section(h, mid, data["html"], data.get("position") or "before_faq")
        else: raise RuntimeError("unknown change kind " + kind)
        self._save(f, new, enc, orig, mid); return {"mod_id": mid}
    def rollback_mod(self, pid, mod_id):
        url, f, h, enc = self._load(pid); new, changed = remove_mod(h, int(mod_id))
        if not changed: raise RuntimeError("the change was not found in the file (already removed?)")
        self._save(f, new, enc, h.encode(enc, "replace"), int(time.time() * 1000)); return {"ok": True}
    # -- blog posts
    def upload_image(self, data, name):
        path = posixpath.join(self.root, self.blog, "images", name); self._rem().write(path, data); return f"{self.base}/{self.blog}/images/{name}"

    # -- blog posts: build a page from one of the site's own pages (its header, footer and styles), upload it, list it
    def _template(self):
        url = self.site.get("template_url")
        if not url:
            self.all_ids(); c = [u for u in self.urls.values() if f"/{self.blog}/" in u and u.rstrip("/").split("/")[-1] != self.blog]
            url = c[0] if c else self.base + "/"
        f, b = self._file(url); h = self._dec(b)[0]
        return url, re.sub(r"<!--sab:(\d+)-->.*?<!--/sab:\1-->", "", h, flags=re.S)
    def build_post_page(self, p, url):
        turl, h = self._template()
        def root_rel(m):
            v = m.group(2); u = urlparse(urljoin(turl, v)); return m.group(1) + u.path + (("?" + u.query) if u.query else "") + m.group(3)
        h = re.sub(r"""(\b(?:href|src)=["'])(?!https?:|//|/|#|mailto:|tel:|data:|javascript:)([^"']+)(["'])""", root_rel, h, flags=re.I)
        imgs = p.get("images") or []; date = (p.get("published_at") or dt.datetime.now(dt.timezone.utc).isoformat())[:10]
        hero = f'<img src="{_esc(imgs[0]["url"])}" alt="{_esc(imgs[0].get("alt") or p["title"])}" width="{imgs[0].get("w", 1536)}" height="{imgs[0].get("h", 1024)}" style="max-width:100%;height:auto">' if imgs and imgs[0]["url"] not in p["html"] else ""
        rev = f'<p class="sab-meta">Medically reviewed by {_esc(self.site["reviewer_name"])}</p>' if self.site.get("reviewer_required") and self.site.get("reviewer_name") else ""
        inner = f'<article class="sab-post"><h1>{_esc(p["title"])}</h1><p class="sab-meta">Published {date}</p>{rev}{hero}{p["html"]}</article>'
        m = re.search(r"<!--SAB-CONTENT-->.*?<!--/SAB-CONTENT-->", h, re.S)
        if m: h = h[:m.start()] + "<!--SAB-CONTENT-->" + inner + "<!--/SAB-CONTENT-->" + h[m.end():]
        else:
            m = re.search(r"(<main\b[^>]*>).*?(</main>)", h, re.S | re.I) or re.search(r"(<article\b[^>]*>).*?(</article>)", h, re.S | re.I)
            if not m: raise RuntimeError(f"The template page {turl} has no <main> or <article>. Put <!--SAB-CONTENT--><!--/SAB-CONTENT--> where post text should go, or set another template page for this site.")
            h = h[:m.start()] + m.group(1) + inner + m.group(2) + h[m.end():]
        meta = p.get("meta_description") or ""; _, h = new_seo(h, p.get("meta_title") or p["title"], meta)
        h = re.sub(r'<script[^>]+application/ld\+json[^>]*>.*?</script>', "", h, flags=re.S | re.I)
        can = f'<link rel="canonical" href="{_esc(url)}">'
        h = re.sub(r'<link\b[^>]*rel=["\']canonical["\'][^>]*>', can, h, count=1, flags=re.I) if re.search(r'rel=["\']canonical["\']', h, re.I) else h.replace("</head>", can + "\n</head>", 1)
        h = re.sub(r'(<meta\b[^>]*property=["\']og:url["\'][^>]*content=)(["\']).*?\2', lambda m: f'{m.group(1)}"{_esc(url)}"', h, flags=re.I)
        if imgs: h = re.sub(r'(<meta\b[^>]*property=["\']og:image["\'][^>]*content=)(["\']).*?\2', lambda m: f'{m.group(1)}"{_esc(imgs[0]["url"])}"', h, flags=re.I)
        ld = {"@context": "https://schema.org", "@type": "BlogPosting", "headline": p["title"][:110], "datePublished": date, "mainEntityOfPage": url, **({"image": imgs[0]["url"]} if imgs else {})}
        return h.replace("</head>", '<script type="application/ld+json">' + json.dumps(ld, ensure_ascii=False) + "</script>\n</head>", 1)
    def publish_post(self, p):
        """Upload the finished page, add it to sitemap.xml and the blog index. Returns (url, notes)."""
        r = self._rem(); url = f"{self.base}/{self.blog}/{p['slug']}/"; notes = []
        r.write(posixpath.join(self.root, self.blog, p["slug"], "index.html"), self.build_post_page(p, url).encode("utf-8"))
        sm = r.read(posixpath.join(self.root, "sitemap.xml"))
        if sm is not None:
            t = sm.decode("utf-8", "replace")
            if url in t: pass
            elif "</urlset>" in t:
                r.write(posixpath.join(self.root, "sitemap.xml"), t.replace("</urlset>", f"  <url><loc>{url}</loc><lastmod>{dt.date.today()}</lastmod></url>\n</urlset>", 1).encode("utf-8"))
            else: notes.append("sitemap.xml is an index file, so the post was not added to it")
        else: notes.append("no sitemap.xml found")
        ix = posixpath.join(self.root, self.blog, "index.html"); b = r.read(ix)
        if b is None: notes.append("no blog index page found")
        else:
            t, enc = self._dec(b)
            if "<!--SAB-POSTS-->" in t and url not in t:
                card = f'<!--SAB-POSTS-->\n<article class="sab-card"><h3><a href="{_esc(url)}">{_esc(p["title"])}</a></h3><p>{_esc(p.get("meta_description") or "")}</p></article>'
                r.write(ix, t.replace("<!--SAB-POSTS-->", card, 1).encode(enc, "replace"))
            elif "<!--SAB-POSTS-->" not in t: notes.append("blog index not updated: put <!--SAB-POSTS--> in the blog index page where the list should start")
        return url, notes
