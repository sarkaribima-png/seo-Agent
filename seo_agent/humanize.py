"""Humanizer rules (ported from the humanizer skill) + a checker that finds AI-writing tells in a draft."""
import re

RULES = """WRITING STYLE (humanizer rules, follow strictly):
- State each point directly. Never use 'not X but Y', 'not just/only/merely X, but Y', 'it's not X, it's Y', or 'This does not mean X. It means Y.'
- No one-line closers that repeat the point ("That is the real win.", "That matters."). End a section on its last concrete fact.
- No deep-sounding sayings ("at its core", "the real question is", "what really matters").
- No staged openers ("Let's dive in", "Here's what you need to know", "Honestly?", "Look,").
- Do not argue with an imaginary reader ("You might think", "Don't get me wrong", "A tempting approach").
- Do not force lists of three. Use the number of items the facts have.
- Do not start several sentences in a row with the same word.
- No em dashes or en dashes. Use a period, comma, colon or brackets.
- Avoid these words: additionally, crucial, delve, pivotal, testament, tapestry, landscape (abstract), showcase, underscore, vibrant, robust, meticulous, intricate, enhance, foster, garner, seamless, realm, journey (figurative), game-changer.
- No inflated significance, no sales language (stunning, renowned, world-class, state-of-the-art), no vague authority ("experts say").
- Use plain verbs: is, are, has. Not 'serves as', 'stands as', 'boasts', 'features'.
- No bold text, no emojis, no chatbot talk ("I hope this helps", "Great question").
- Vary sentence length. Real writing mixes short and long sentences. Keep a calm, human, helpful tone, like a doctor's helper explaining to a worried friend."""

STOCK = ["additionally", "crucial", "delve", "pivotal", "testament", "tapestry", "showcase", "showcasing", "underscore", "underscores",
         "vibrant", "robust", "meticulous", "meticulously", "intricate", "intricacies", "enhance", "enhances", "foster", "fosters",
         "garner", "seamless", "seamlessly", "realm", "game-changer", "game changer", "stunning", "renowned", "world-class",
         "state-of-the-art", "breathtaking", "nestled", "in the heart of", "at its core", "dive into", "let's dive", "let's explore",
         "here's what you need to know", "i hope this helps", "great question", "in conclusion", "in today's world", "ever-evolving",
         "the future looks bright", "exciting times", "evolving landscape"]
PATTERNS = [(r"\bnot (just|only|merely)\b[^.]{0,80}\bbut\b", "uses 'not just X but Y'"),
            (r"\b(it'?s|it is|this is|that is) not\b[^.]{0,60}[,;] ?(it'?s|it is|but)\b", "uses 'it is not X, it is Y'"),
            (r"\bthis does not mean\b[^.]{0,80}\. it means\b", "uses 'This does not mean X. It means Y'"),
            (r"\bthat is the real\b|\bread that again\b|\blet that sink in\b", "one-line dramatic closer"),
            (r"\bhonestly\?|\blet'?s be honest\b|\breal talk\b", "staged candor")]
RISKY = ["guarantee", "guaranteed", "100%", "painless", "scarless", "risk-free", "no risk", "miracle", "permanent cure", "best surgeon",
         "best doctor", "#1", "number one", "before and after", "before & after"]


def plain(html):
    import html as h
    return h.unescape(re.sub(r"<[^>]+>", " ", html or "")).replace("\u00a0", " ")


def lint(html, banned=(), medical=True):
    """Return a list of plain-language problems found in an HTML article."""
    t = plain(html); low = t.lower(); out = []
    if re.search(r"[\u2014\u2013]|\s--\s", t): out.append("contains em or en dashes; use a period, comma or colon")
    for w in STOCK:
        if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low): out.append(f"uses AI-sounding phrase '{w}'")
    for rx, msg in PATTERNS:
        if re.search(rx, low): out.append(msg)
    if len(re.findall(r"<(strong|b)[ >]", html or "", re.I)) > 0: out.append("contains bold text; remove it")
    if re.search(r"<h1", html or "", re.I): out.append("contains an h1; use h2 and h3 only")
    for b in list(banned) + (RISKY if medical else []):
        if b and re.search(r"(?<![a-z0-9])" + re.escape(b.lower()) + r"(?![a-z0-9])", low): out.append(f"contains banned or risky term '{b}'")
    # three sentences in a row starting with the same word
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", t) if s.strip()]
    for i in range(len(sents) - 2):
        a = [s.split()[0].lower() for s in sents[i:i + 3] if s.split()]
        if len(a) == 3 and a[0] == a[1] == a[2] and a[0] not in ("the",):
            out.append(f"three sentences in a row start with '{a[0]}'"); break
    return list(dict.fromkeys(out))[:12]
