"""Daily rank tracking from Search Console, desktop and mobile separately.
Run: python -m seo_agent.track <site_id>"""
import sys, datetime as dt
from urllib.parse import urlparse
import requests
from . import sb, gsc, wp

LAG = 2  # Search Console data is about 2 days old


def pull(s, prop, kw, page, days):
    end = dt.date.today() - dt.timedelta(days=LAG); start = end - dt.timedelta(days=days)
    filters = [{"dimension": "query", "operator": "equals", "expression": kw.lower()}]
    if page: filters.append({"dimension": "page", "operator": "contains", "expression": urlparse(page).path.strip("/") or "/"})
    body = {"startDate": str(start), "endDate": str(end), "dimensions": ["date", "device"], "rowLimit": 5000, "dimensionFilterGroups": [{"filters": filters}]}
    return s.searchanalytics().query(siteUrl=prop, body=body).execute().get("rows", [])


def modified(w, page):
    """When WordPress says this page was last edited (catches manual edits too)."""
    if getattr(w, "static", False): return w.modified(page)
    try:
        pid = w.live(page).get("post_id")
        if not pid: return None
        for kind in ("pages", "posts"):
            r = requests.get(f"{w.base}/wp-json/wp/v2/{kind}/{pid}", params={"_fields": "modified_gmt"}, auth=w.auth, timeout=30)
            if r.ok and r.json().get("modified_gmt"): return r.json()["modified_gmt"] + "Z"
    except Exception as e: print("modified check failed:", page, str(e)[:80])
    return None


def run(site_id):
    from . import run as runmod
    cfg = runmod.sites()[site_id]; site = runmod.ensure_site(cfg)
    kws = sb.select("tracked_keywords", site_id=f"eq.{site_id}", active="eq.true")
    if not kws: return print("no tracked keywords")
    s = gsc.service(); w = wp.client(cfg); prop = site.get("gsc_property") or cfg["gsc_property"]; seen = set()
    for k in kws:
        have = sb.select("rank_daily", site_id=f"eq.{site_id}", keyword=f"eq.{k['keyword']}", select="day", limit="1")
        try: rows = pull(s, prop, k["keyword"], k.get("page_url"), 10 if have else 90)
        except Exception as e: print("GSC failed for", k["keyword"], str(e)[:150]); continue
        out = [{"site_id": site_id, "keyword": k["keyword"], "day": r["keys"][0], "device": r["keys"][1].lower(), "position": round(r["position"], 2),
                "impressions": int(r["impressions"]), "clicks": int(r["clicks"])} for r in rows]
        if out: sb.upsert("rank_daily", out, "site_id,keyword,day,device")
        print(k["keyword"], "->", len(out), "rows")
        pg = k.get("page_url")
        if pg and pg not in seen:
            seen.add(pg); m = modified(w, pg)
            if m: sb.upsert("page_changes", [{"site_id": site_id, "page_url": pg, "modified_at": m}], "site_id,page_url,modified_at")


if __name__ == "__main__":
    from . import run as _r
    _r.each(sys.argv[1], run)
