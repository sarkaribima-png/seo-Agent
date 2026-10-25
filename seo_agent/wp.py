import os, re, requests
class WP:
    def __init__(self, site):
        p = site["secrets_prefix"]; self.base = site["wp_base"].rstrip("/")
        self.auth = (os.environ[f"{p}_WP_USER"], os.environ[f"{p}_WP_APP_PASSWORD"])
    def live(self, url):
        h = requests.get(url, params={"nc": os.urandom(3).hex()}, timeout=30).text
        g = lambda rx: (re.search(rx, h, re.S | re.I) or [None, ""])[1].strip()
        return {"title": g(r"<title>(.*?)</title>"),
                "meta": g(r'<meta name="description" content="(.*?)"'),
                "h1": re.sub(r"<[^>]+>", "", g(r"<h1[^>]*>(.*?)</h1>")),
                "words": len(re.sub(r"<[^>]+>", " ", h).split()),
                "post_id": g(r"page-id-(\d+)") or g(r"postid-(\d+)")}
    def set_seo(self, post_id, title, meta):
        """AIOSEO REST. Test on ONE page in dry-run first; payload may need full settings on some versions."""
        r = requests.post(f"{self.base}/wp-json/aioseo/v1/post", auth=self.auth, timeout=30,
                          json={"postId": int(post_id), "title": title, "description": meta})
        r.raise_for_status(); return r.json()
