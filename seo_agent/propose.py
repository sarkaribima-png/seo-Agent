import os, json, anthropic
SYS = """You are an SEO editor for a medical/cosmetic surgery clinic website. Write ONE title and ONE meta description
for the page. Rules: title 30-60 chars; meta 110-160 chars; use only facts present in the input; NEVER claim rankings,
guarantees, percentages, 'best', FDA/NABH approvals, or competitor names. Use top_queries: the title must contain the
highest-impression query naturally, and the meta must add a concrete reason to click. Mention only things shown in h1/h2:
if the headings do not show reviews, prices or surgeon details, do not mention them. If the current title already does
this well, return it unchanged.If the current title contains a city name (Gurgaon, Delhi), keep that city in the new title. Do not drop local intent. Keep brand suffix ' | Kalosa' only if the title stays under 60.
Reply with JSON only: {"title":"","meta":"","why":""}"""
def propose(page, live, queries, feedback=""):
    cl = anthropic.Anthropic()
    msg = {"url": page, "current_title": live["title"], "current_meta": live["meta"], "h1": live["h1"],
           "h2": live.get("h2", []), "top_queries": queries, "previous_errors": feedback}
    r = cl.messages.create(model=os.environ.get("SEO_MODEL", "claude-haiku-5-5"), max_tokens=2000, system=SYS,
                           messages=[{"role": "user", "content": json.dumps(msg)}])
    t = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
    return json.loads(t[t.index("{"): t.rindex("}") + 1])
