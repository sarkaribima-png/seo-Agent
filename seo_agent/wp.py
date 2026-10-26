import os, re, html, requests
class WP:
    def __init__(self, site):
        p = site["secrets_prefix"]; self.base = site["wp_base"].rstrip("/")
        self.auth = (os.environ[f"{p}_WP_USER"], os.environ[f"{p}_WP_APP_PASSWORD"])
        self.api = self.base + "/wp-json/seo-agent/v1"
    def _req(self, m, path, **kw):
        r = requests.request(m, self.api + path, auth=self.auth, timeout=60, **kw); r.raise_for_status(); return r.json()
    def live(self, url):
        h = requests.get(url, params={"nc": os.urandom(3).hex()}, timeout=30).text
        g = lambda rx: (re.search(rx, h, re.S | re.I) or [None, ""])[1].strip()
        clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
        h2 = [clean(x) for x in re.findall(r"<h2[^>]*>(.*?)</h2>", h, re.S | re.I)]
        return {"title": html.unescape(g(r"<title>(.*?)</title>")),
                "meta": html.unescape(g(r'<meta name="description" content="(.*?)"')),
                "h1": clean(g(r"<h1[^>]*>(.*?)</h1>")), "h2": [x for x in h2 if x][:25],
                "post_id": g(r"page-id-(\d+)") or g(r"postid-(\d+)")}
    def audit(self, post_id): return self._req("GET", f"/audit/{int(post_id)}")
    def set_seo(self, post_id, title, meta):
        return self._req("POST", f"/seo/{int(post_id)}", json={"title": title, "description": meta})
    def rollback(self, post_id, backup_id):
        return self._req("POST", f"/rollback/{int(post_id)}", json={"backup_id": int(backup_id)})
    def all_ids(self):
        ids = []
        for kind in ("pages", "posts"):
            n = 1
            while True:
                r = requests.get(f"{self.base}/wp-json/wp/v2/{kind}", auth=self.auth, timeout=60,
                                 params={"per_page": 100, "page": n, "status": "publish", "_fields": "id"})
                if r.status_code != 200 or not r.json(): break
                ids += [x["id"] for x in r.json()]; n += 1
        return ids
