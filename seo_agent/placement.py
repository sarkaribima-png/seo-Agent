"""Where a new section goes on a page. One place that decides what a valid position looks like (used by run.py and static.py).
A position is one of the strings 'before_faq' (default), 'after_faq', 'end', or an object:
    {"after_heading": "Recovery after surgery", "occurrence": 1}
which puts the section after that heading's whole block (before the next heading of the same or a higher level)."""
import re, html

SIMPLE = ("before_faq", "after_faq", "end")
MAX_HEADING = 200


def norm_heading(s):
    """Lower case, tags and entities removed, spaces collapsed. Used to compare a heading typed by a person with the page's heading."""
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or "")).replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def normalize(pos):
    """Return a clean position (string or dict). Anything unknown becomes 'before_faq'. A bad heading position raises ValueError."""
    if pos is None or pos == "": return "before_faq"
    if isinstance(pos, str): return pos if pos in SIMPLE else "before_faq"
    if isinstance(pos, dict):
        h = pos.get("after_heading")
        if not isinstance(h, str) or not norm_heading(h): raise ValueError("after_heading needs the heading text")
        if len(h) > MAX_HEADING: raise ValueError("heading text is too long")
        try: occ = int(pos.get("occurrence") or 1)
        except (TypeError, ValueError): raise ValueError("occurrence must be a number")
        if not 1 <= occ <= 20: raise ValueError("occurrence must be between 1 and 20")
        out = {"after_heading": re.sub(r"\s+", " ", h).strip()}
        if occ != 1: out["occurrence"] = occ
        return out
    raise ValueError("position must be a string or an object")


def pick_heading(heads, text, occurrence=1):
    """heads: list of (start, end, level, plain_text). Exact match (normalised) first. If there is none, a heading that contains the
    text is accepted only when exactly one heading does. Returns the heading tuple or None."""
    want = norm_heading(text)
    exact = [x for x in heads if norm_heading(x[3]) == want]
    if exact: return exact[occurrence - 1] if occurrence <= len(exact) else None
    part = [x for x in heads if want in norm_heading(x[3])]
    return part[0] if len(part) == 1 and occurrence == 1 else None
