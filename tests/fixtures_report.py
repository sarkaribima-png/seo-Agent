"""SYNTHETIC fixture data for tests / sample report. Not real client data."""
import datetime as dt
TODAY = dt.date(2026, 3, 16)           # Monday -> current week 2026-03-08..2026-03-14, previous 2026-03-01..2026-03-07
CUR = [dt.date(2026, 3, 8) + dt.timedelta(days=i) for i in range(7)]
PREV = [dt.date(2026, 3, 1) + dt.timedelta(days=i) for i in range(7)]


def gsc_rows(page, prev_clicks_per_day, cur_clicks_per_day, prev_pos, cur_pos, prev_impr=100, cur_impr=100):
    rows = []
    for days, c, pos, imp in ((PREV, prev_clicks_per_day, prev_pos, prev_impr), (CUR, cur_clicks_per_day, cur_pos, cur_impr)):
        for d in days:
            rows.append({"date": str(d), "page": page, "device": "MOBILE", "clicks": c, "impressions": imp, "position": pos})
    return rows


def rank_rows(kw, prev_pos, cur_pos, impr=10, page="https://x.in/a"):
    rows = []
    for days, pos in ((PREV, prev_pos), (CUR, cur_pos)):
        for d in days:
            for dev in ("desktop", "mobile"):
                rows.append({"keyword": kw, "day": str(d), "device": dev, "position": pos, "impressions": impr, "clicks": 1, "page_url": page})
    return rows


def synthetic(drop=0.5):
    """Page /a loses clicks, /b stable, /c position falls by 4, /d grows. Plus one applied change on /d."""
    A, B, C, Dd = "https://x.in/a", "https://x.in/b", "https://x.in/c", "https://x.in/d"
    gsc = (gsc_rows(A, 10, 10 * (1 - drop), 5.0, 5.2) + gsc_rows(B, 8, 8, 6.0, 6.0) + gsc_rows(C, 6, 6, 4.0, 8.0) + gsc_rows(Dd, 4, 9, 12.0, 9.0))
    rank = rank_rows("tummy tuck cost", 5.0, 9.5, page=A) + rank_rows("rhinoplasty", 12.0, 8.0, page=Dd) + rank_rows("rare kw", 9.0, 15.0, impr=0)
    return {"gsc_daily": gsc, "rank_daily": rank, "tracked_keywords": [{"keyword": "tummy tuck cost", "page_url": A}, {"keyword": "rhinoplasty", "page_url": Dd}],
            "proposals": [{"id": 11, "page": Dd, "kind": "title_meta", "applied_at": "2026-03-01T04:00:00+00:00", "status": "applied"},
                          {"id": 12, "page": A, "kind": "schema", "applied_at": "2026-03-12T04:00:00+00:00", "status": "applied"}],
            "page_changes": [{"page_url": A, "modified_at": "2026-03-06T10:00:00Z"}], "ga4": None, "pending_proposals": 2}
