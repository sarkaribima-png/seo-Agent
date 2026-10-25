import os, json, anthropic
SYS = """You are an SEO editor for a medical/cosmetic surgery clinic website. Write ONE title and ONE meta description
for the page. Rules: title 30-60 chars; meta 110-160 chars; include the main query naturally; use only facts present in
the input; NEVER claim rankings, guarantees, percentages, 'best', FDA/NABH approvals, or competitor names. Keep brand suffix
' | Kalosa' only if the title stays under 60. Reply with JSON only: {"title":"","meta":"","why":""}"""
def propose(page, live, queries, feedback=""):
    cl = anthropic.Anthropic()
    msg = {"url": page, "current_title": live["title"], "current_meta": live["meta"], "h1": live["h1"],
           "top_queries": queries, "previous_errors": feedback}
    r = cl.messages.create(model=os.environ.get("SEO_MODEL", "claude-haiku-5-5"), max_tokens=2000, system=SYS,
                           messages=[{"role": "user", "content": json.dumps(msg)}])
    t = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
    return json.loads(t[t.index("{"): t.rindex("}") + 1])
