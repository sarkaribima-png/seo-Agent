"""Optional: check who ranks in Google's top 10 for tracked keywords (DataForSEO). Costs money, so: opt-in per keyword + monthly budget cap.
Run: python -m seo_agent.serp <site_id>"""
import os, sys, datetime as dt
import requests
from . import sb

API = "https://api.dataforseo.com/v3/serp/google/organic/live/regular"
DEVICES = [d for d in os.environ.get("SERP_DEVICES", "desktop,mobile").split(",") if d]
INDIA = 2356


def call(auth, keyword, device, location):
    task = {"keyword": keyword, "language_code": "en", "device": device, "depth": 10}
    for loc in ({"location_name": location}, {"location_code": INDIA}):
        r = requests.post(API, auth=auth, json=[{**task, **loc}], timeout=90)
        if not r.ok: raise RuntimeError(f"DataForSEO HTTP {r.status_code}: {r.text[:300]}")
        j = r.json(); t = (j.get("tasks") or [{}])[0]
        if t.get("status_code") == 20000: return t
        if "location" not in str(t.get("status_message", "")).lower(): raise RuntimeError(f"DataForSEO: {t.get('status_code')} {t.get('status_message')}")
    raise RuntimeError("DataForSEO: location not accepted")


def parse(t):
    items = (((t.get("result") or [{}])[0]).get("items")) or []
    return [{"position": int(i["rank_group"]), "domain": (i.get("domain") or "").replace("www.", ""), "url": i.get("url") or "", "title": (i.get("title") or "")[:200]}
            for i in items if i.get("type") == "organic" and i.get("rank_group")][:10]


def spent_this_month(site_id):
    first = dt.date.today().replace(day=1).isoformat()
    return sum(float(r["cost"] or 0) for r in sb.select("serp_usage", site_id=f"eq.{site_id}", day=f"gte.{first}", select="cost"))


def run(site_id):
    from . import run as runmod
    runmod.sites()[site_id]; site = sb.select("sites", id=f"eq.{site_id}")[0]
    login, pw = os.environ.get("DATAFORSEO_LOGIN"), os.environ.get("DATAFORSEO_PASSWORD")
    if not (login and pw): return print("DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not set")
    kws = sb.select("tracked_keywords", site_id=f"eq.{site_id}", active="eq.true", serp="eq.true")
    if not kws: return print("no keywords have competitor tracking switched on")
    cap = float(site.get("serp_monthly_budget_usd") or 5); spent = spent_this_month(site_id); loc = site.get("serp_location") or "Gurgaon,Haryana,India"
    today = dt.date.today().isoformat(); done = 0
    for k in kws:
        for dev in DEVICES:
            if spent >= cap: return print(f"monthly budget reached (${spent:.2f} of ${cap:.2f}); stopped")
            try: t = call((login, pw), k["keyword"], dev, loc)
            except Exception as e: print("FAILED", k["keyword"], dev, str(e)[:150]); continue
            cost = float(t.get("cost") or 0); spent += cost
            rows = [{"site_id": site_id, "keyword": k["keyword"], "device": dev, "day": today, **r} for r in parse(t)]
            if rows: sb.upsert("serp_results", rows, "site_id,keyword,device,day,position")
            sb.insert("serp_usage", [{"site_id": site_id, "day": today, "keyword": k["keyword"], "device": dev, "cost": cost}])
            done += 1; print(k["keyword"], dev, "->", len(rows), "results, cost $%.4f" % cost)
    print(f"done: {done} checks, spent this month ${spent:.2f} of ${cap:.2f}")


if __name__ == "__main__":
    from . import run as _r
    _r.each(sys.argv[1], run)
