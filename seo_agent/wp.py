import os, re, html, requests
class WP:
    def __init__(self, site):
        p = site["secrets_prefix"]; self.base = site["wp_base"].rstrip("/")
        self.auth = (os.environ[f"{p}_WP_USER"], os.environ[f"{p}_WP_APP_PASSWORD"])
    def live(self, url):
        h = requests.get(url, params={"nc": os.urandom(3).hex()}, timeout=30).text
        g = lambda rx: (re.search(rx, h, re.S | re.I) or [None, ""])[1].strip()
        clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
        h2 = [clean(x) for x in re.findall(r"<h2[^>]*>(.*?)</h2>", h, re.S | re.I)]
        return {"title": html.unescape(g(r"<title>(.*?)</title>")),
                "meta": html.unescape(g(r'<meta name="description" content="(.*?)"')),
                "h1": clean(g(r"<h1[^>]*>(.*?)</h1>")),
                "h2": [x for x in h2 if x][:25],
                "words": len(re.sub(r"<[^>]+>", " ", h).split()),
                "post_id": g(r"page-id-(\d+)") or g(r"postid-(\d+)")}
    def set_seo(self, post_id, title, meta):
        """AIOSEO REST. Test on ONE page in dry-run first; payload may need full settings on some versions."""
        r = requests.post(f"{self.base}/wp-json/aioseo/v1/post", auth=self.auth, timeout=30,
                          json={"postId": int(post_id), "title": title, "description": meta})
        r.raise_for_status(); return r.json()
