"""Competitor watch: re-read competitor pages weekly and report what changed.
Run: python -m seo_agent.watch <site_id>"""
import sys, hashlib, datetime as dt
from . import sb, audit


def snap(url):
    p = audit.extract(url)
    return {"title": p["title"], "meta": p["meta"], "h1": p["h1"], "h2": p["h2"], "h3_count": len(p["h3"]), "words": p["words"], "images": p["images"],
            "internal_links": p["internal_links"], "tables": p["tables"], "schema": p["schema"], "has_faq": p["has_faq"],
            "text_hash": hashlib.sha1(p["text_lower"].encode()).hexdigest()}


def diff(old, new):
    """Human-readable list of what changed between two snapshots. Small noise is ignored."""
    out = []
    if old["title"] != new["title"]: out.append(f"Title changed: “{old['title']}” → “{new['title']}”")
    if old["meta"] != new["meta"]: out.append("Meta description changed")
    add = [h for h in new["h2"] if h not in old["h2"]]; rem = [h for h in old["h2"] if h not in new["h2"]]
    if add: out.append("New sections: " + "; ".join(add[:6]))
    if rem: out.append("Removed sections: " + "; ".join(rem[:6]))
    if old["words"] and abs(new["words"] - old["words"]) / old["words"] >= 0.15: out.append(f"Length changed: {old['words']} → {new['words']} words")
    s_add = sorted(set(new["schema"]) - set(old["schema"])); s_rem = sorted(set(old["schema"]) - set(new["schema"]))
    if s_add: out.append("Schema added: " + ", ".join(s_add))
    if s_rem: out.append("Schema removed: " + ", ".join(s_rem))
    if new["has_faq"] and not old["has_faq"]: out.append("FAQ section added")
    if new["tables"] > old["tables"]: out.append("Table added")
    if old["internal_links"] and new["internal_links"] - old["internal_links"] >= 10: out.append(f"Internal links up: {old['internal_links']} → {new['internal_links']}")
    if not out and old["text_hash"] != new["text_hash"]: return []   # tiny text edits are not worth an alert
    return out


def run(site_id):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for w in sb.select("competitor_watch", site_id=f"eq.{site_id}", active="eq.true"):
        try: new = snap(w["competitor_url"])
        except Exception as e:
            print("could not read", w["competitor_url"], str(e)[:100]); sb.update("competitor_watch", {"id": w["id"]}, {"last_error": str(e)[:200], "last_checked": now}); continue
        prev = sb.select("competitor_snapshots", watch_id=f"eq.{w['id']}", order="taken_at.desc", limit="1")
        sb.insert("competitor_snapshots", [{"watch_id": w["id"], "data": new}])
        changes = diff(prev[0]["data"], new) if prev else []
        if changes: sb.insert("competitor_changes", [{"watch_id": w["id"], "site_id": site_id, "summary": changes}])
        sb.update("competitor_watch", {"id": w["id"]}, {"last_checked": now, "last_error": None})
        print(w["competitor_url"], "->", "first snapshot" if not prev else (f"{len(changes)} change(s)" if changes else "no change"))


if __name__ == "__main__":
    from . import run as _r
    _r.each(sys.argv[1], run)
