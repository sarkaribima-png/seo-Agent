"""Keeps a site's daily-post pipeline full without manual clicks. Run: python -m seo_agent.autopilot <site_id|all>
  1. if the idea queue is short, ask for new ideas (competitor data if imported, else niche + keywords + Search Console)
  2. approve the next ideas so the writer always has work
  3. approve clean drafts (no issues) on sites with no medical reviewer rule. Medical sites are never auto-approved."""
import sys, datetime as dt
from . import sb

QUEUE = 3   # ideas kept approved and waiting


def run(site_id):
    from . import ideas
    site = (sb.select("sites", id=f"eq.{site_id}") or [None])[0]
    if not site or not site.get("autopilot"): return print(site_id, "autopilot off")
    if site.get("mode") == "paused": return print(site_id, "site paused")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    ids = lambda st: sb.select("content_ideas", site_id=f"eq.{site_id}", status=f"eq.{st}", order="volume.desc.nullslast,id.asc", limit="50")
    waiting, sugg = ids("approved"), ids("suggested")
    if len(waiting) + len(sugg) < QUEUE + 2:
        ideas.run(site_id); sugg = ids("suggested")
    for i in sugg[:max(0, QUEUE - len(waiting))]:
        sb.update("content_ideas", {"id": i["id"]}, {"status": "approved", "decided_at": now}); print("idea approved:", i["title"])
    if site.get("reviewer_required") or site.get("profile") == "medical":
        return print(site_id, "medical site: drafts wait for the reviewer")
    for p in sb.select("content_posts", site_id=f"eq.{site_id}", status="eq.draft", limit="20"):
        if p.get("issues"): print("left for manual check:", p["title"], p["issues"]); continue
        sb.update("content_posts", {"id": p["id"]}, {"status": "approved", "approved_at": now}); print("post approved:", p["title"])


if __name__ == "__main__":
    from . import run as _r
    _r.each(sys.argv[1], run, kinds=("wordpress", "static", "html"))
