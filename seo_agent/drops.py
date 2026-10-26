import datetime as dt
def _agg(c, site, a, b):
    q = """SELECT page,device,SUM(clicks) clicks,SUM(impr) impr,
           SUM(pos*impr)/NULLIF(SUM(impr),0) pos FROM gsc WHERE site=? AND date>=? AND date<? GROUP BY page,device"""
    return {(r["page"], r["device"]): r for r in c.execute(q, (site, str(a), str(b)))}
def detect(c, site, today=None, min_impr=30, pos_worse=3.0, click_drop=0.30):
    today = today or dt.date.today()
    cur = _agg(c, site, today - dt.timedelta(days=9), today)
    prev = _agg(c, site, today - dt.timedelta(days=16), today - dt.timedelta(days=9))
    out = []
    for k, p in prev.items():
        n = cur.get(k)
        if not n or (p["impr"] or 0) < min_impr: continue
        dpos = (n["pos"] or 0) - (p["pos"] or 0)
        dclk = ((n["clicks"] or 0) - (p["clicks"] or 0)) / p["clicks"] if p["clicks"] else 0
        if dpos >= pos_worse or dclk <= -click_drop:
            out.append({"page": k[0], "device": k[1], "pos_prev": round(p["pos"],1), "pos_now": round(n["pos"],1),
                        "clicks_prev": p["clicks"], "clicks_now": n["clicks"], "score": dpos + abs(min(dclk,0))*10})
    return sorted(out, key=lambda x: -x["score"])
def opportunities(c, site, today=None, min_impr=100):
    """Pages already near page 1 with weak CTR: best easy wins."""
    today = today or dt.date.today()
    q = """SELECT page,SUM(clicks) clicks,SUM(impr) impr,SUM(pos*impr)/SUM(impr) pos FROM gsc
           WHERE site=? AND date>=? GROUP BY page HAVING SUM(impr)>=?"""
    rows = c.execute(q, (site, str(today - dt.timedelta(days=28)), min_impr)).fetchall()
    res = [{"page": r["page"], "pos": round(r["pos"],1), "impr": r["impr"], "ctr": (r["clicks"]/r["impr"]),
            "score": r["impr"]/(1+abs(r["pos"]-8))} for r in rows if 4 <= r["pos"] <= 25]
    return sorted(res, key=lambda x: -x["score"])
