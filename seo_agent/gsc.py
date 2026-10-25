import os, json, datetime as dt
from google.oauth2 import service_account
from googleapiclient.discovery import build
def service():
    info = json.loads(os.environ["GSC_SERVICE_ACCOUNT_JSON"])
    cred = service_account.Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/webmasters.readonly"])
    return build("searchconsole", "v1", credentials=cred, cache_discovery=False)
def pull(c, site, days=30):
    s = service(); end = dt.date.today() - dt.timedelta(days=2); start = end - dt.timedelta(days=days)
    body = {"startDate": str(start), "endDate": str(end), "dimensions": ["date","page","device"], "rowLimit": 25000}
    rows = s.searchanalytics().query(siteUrl=site["gsc_property"], body=body).execute().get("rows", [])
    for r in rows:
        d, p, dev = r["keys"]
        c.execute("INSERT OR REPLACE INTO gsc VALUES(?,?,?,?,?,?,?)",(site["id"],d,p,dev,r["clicks"],r["impressions"],r["position"]))
    c.commit(); return len(rows)
def top_queries(site, page, days=28, n=8):
    s = service(); end = dt.date.today() - dt.timedelta(days=2); start = end - dt.timedelta(days=days)
    body = {"startDate": str(start), "endDate": str(end), "dimensions": ["query"], "rowLimit": 200,
            "dimensionFilterGroups": [{"filters": [{"dimension":"page","operator":"equals","expression":page}]}]}
    rows = s.searchanalytics().query(siteUrl=site["gsc_property"], body=body).execute().get("rows", [])
    rows.sort(key=lambda r: -r["impressions"])
    return [{"q": r["keys"][0], "impr": r["impressions"], "pos": round(r["position"],1)} for r in rows[:n]]
