"""Post performance: is each published post indexed, and what does it get in Google?
Run: python -m seo_agent.postperf <site_id>"""
import sys, datetime as dt
from urllib.parse import urlparse
from . import sb, gsc

MAX_POSTS = 60


def queries(s, prop, url, start, end):
    path = urlparse(url).path.strip("/")
    body = {"startDate": str(start), "endDate": str(end), "dimensions": ["query"], "rowLimit": 100,
            "dimensionFilterGroups": [{"filters": [{"dimension": "page", "operator": "contains", "expression": path or "/"}]}]}
    rows = s.searchanalytics().query(siteUrl=prop, body=body).execute().get("rows", [])
    rows.sort(key=lambda r: -r["impressions"]); return rows


def inspect(s, prop, url):
    try:
        r = s.urlInspection().index().inspect(body={"inspectionUrl": url, "siteUrl": prop}).execute()["inspectionResult"]["indexStatusResult"]
    except Exception as e:
        return {"coverage": "check failed: " + str(e)[:80], "indexed": None, "last_crawl": None}
    cov = r.get("coverageState") or r.get("verdict") or ""
    return {"coverage": cov, "indexed": r.get("verdict") == "PASS", "last_crawl": r.get("lastCrawlTime")}


def run(site_id):
    from . import run as runmod
    cfg = runmod.sites()[site_id]; site = sb.select("sites", id=f"eq.{site_id}")[0]; prop = site.get("gsc_property") or cfg["gsc_property"]
    posts = sb.select("content_posts", site_id=f"eq.{site_id}", status="eq.published", order="published_at.desc", limit=str(MAX_POSTS))
    posts = [p for p in posts if p.get("publish_url")]
    if not posts: return print("no published posts yet")
    s = gsc.service(); today = dt.date.today(); end = today - dt.timedelta(days=2)
    ideas = {i["id"]: i for i in sb.select("content_ideas", site_id=f"eq.{site_id}", select="id,primary_keyword")}
    for p in posts:
        pub = dt.datetime.fromisoformat(p["published_at"].replace("Z", "+00:00")).date() if p.get("published_at") else today
        start = max(pub, end - dt.timedelta(days=28))
        try: rows = queries(s, prop, p["publish_url"], start, end) if start <= end else []
        except Exception as e: print("GSC failed:", p["title"][:50], str(e)[:100]); continue
        imp = sum(r["impressions"] for r in rows); clk = sum(r["clicks"] for r in rows)
        pos = round(sum(r["position"] * r["impressions"] for r in rows) / imp, 2) if imp else None
        ins = inspect(s, prop, p["publish_url"])
        kw = (ideas.get(p.get("idea_id")) or {}).get("primary_keyword")
        top = [{"q": r["keys"][0], "impr": int(r["impressions"]), "clicks": int(r["clicks"]), "pos": round(r["position"], 1)} for r in rows[:10]]
        sb.upsert("post_perf", [{"site_id": site_id, "post_id": p["id"], "day": today.isoformat(), "coverage": ins["coverage"], "indexed": ins["indexed"], "last_crawl": ins["last_crawl"],
                                 "impressions": int(imp), "clicks": int(clk), "position": pos, "keyword": kw, "queries": top}], "post_id,day")
        print(p["title"][:55], "|", ins["coverage"], "| impr", int(imp), "| pos", pos)


if __name__ == "__main__":
    from . import run as _r
    _r.each(sys.argv[1], run)
