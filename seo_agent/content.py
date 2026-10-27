"""Phase 2: write drafts for approved ideas, make images, create WordPress drafts, publish approved posts.
Run: python -m seo_agent.content write <site_id>   |   python -m seo_agent.content publish <site_id>"""
import os, re, io, sys, json, base64, html as htmlmod, datetime as dt
import requests
from . import sb
from .humanize import RULES, lint, plain

TARGET_GRADE = float(os.environ.get("READABILITY_TARGET", "5.5"))
MAX_DRAFTS = int(os.environ.get("MAX_DRAFTS_PER_RUN", "2"))
MODEL = os.environ.get("CONTENT_MODEL", "claude-sonnet-5-5")
IMG_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1")
IMG_STYLE = {"medical": "Flat minimal vector illustration, soft calm colours, simple shapes, no text, no logos, no realistic human bodies or faces, "
                        "no before-and-after, no surgery scenes, no blood. Concept: ",
             "default": "Clean modern flat illustration, simple shapes, no text, no logos, no real people. Concept: "}

SYS = """You write blog posts for a website. The reader is a normal adult who is not an expert. Write like a careful, kind human.
READABILITY: aim for US school grade 5 or lower. Short sentences (average under 12 words). Common words. When a medical or technical
term is needed, say it once and explain it in plain words in the same sentence. Active voice. One idea per sentence.
STRUCTURE: the first two sentences answer the main question of the post. Then 4 to 7 sections, each with an h2 heading (h3 allowed).
Use the primary keyword in the first 100 words and in one h2. Use secondary keywords where they fit naturally. No stuffing.
End with a short FAQ section (h2 'Frequently asked questions' with h3 questions and short answers) and then a short, calm line about
what to do next (for health topics: talk to a qualified doctor who can examine you). No summary closer.
FACTS: use only the facts supplied in 'brain' or very general, well known knowledge stated carefully. Never invent numbers, prices,
percentages, timelines, studies, awards, rankings or patient stories. For cost topics explain what changes the price, not a price.
Never promise results. Say once, plainly, that results and risks differ from person to person and a doctor must assess each case.
Never name competitors. Mention the clinic or doctor at most once and only with facts from 'brain'.
The post is ORIGINAL. You only know the topic and keywords, never copy anyone's wording.
FORMAT: HTML only inside <article>: h2, h3, p, ul, li. No h1, no bold, no tables, no inline styles, no emojis.
""" + RULES + """
OUTPUT, exactly these tags and nothing else:
<meta_title>30-60 characters, contains the primary keyword</meta_title>
<meta_description>110-155 characters, plain, gives a reason to click, no claims</meta_description>
<slug>short-lowercase-slug</slug>
<image_prompts>["concept 1","concept 2","concept 3"]</image_prompts>
<article>...</article>"""


def tag(t, name):
    m = re.search(rf"<{name}>(.*?)</{name}>", t, re.S | re.I); return m.group(1).strip() if m else ""


def syllables(w):
    w = re.sub(r"[^a-z]", "", w.lower())
    if not w: return 0
    if len(w) <= 3: return 1
    w = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", w); w = re.sub(r"^y", "", w)
    return max(1, len(re.findall(r"[aeiouy]{1,2}", w)))


def grade(html):
    """Flesch-Kincaid grade level (no extra libraries). Headings and list items count as sentences."""
    text = re.sub(r"</(h2|h3|p|li)>", ".\n", html or "", flags=re.I)
    t = plain(text)
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", t)
    sents = [x for x in re.split(r"[.!?]+\s", t + " ") if re.search(r"[A-Za-z]", x)]
    if not words or not sents: return 99.0
    syl = sum(syllables(w) for w in words)
    return round(0.39 * len(words) / len(sents) + 11.8 * syl / len(words) - 15.59, 1)


def slugify(s): return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")[:70]


def brain_context(site_id, profile):
    items = sb.select("brain_items", site_id=f"eq.{site_id}", status="eq.active", select="kind,title,body", order="created_at.desc", limit="80")
    rules = [r for r in sb.select("seo_rules", status="eq.active", select="title,rule,applies_to") if r["applies_to"] in ("all", profile)]
    parts, n = [], 0
    for i in items:
        if i["kind"] not in ("fact", "reference", "voice", "faq", "content"): continue
        s = f"[{i['kind']}] {i['title']}: {(i.get('body') or '')[:900]}"
        if n + len(s) > 8000: break
        parts.append(s); n += len(s)
    return {"facts_and_references": parts, "seo_rules": [f"{r['title']}: {r['rule']}" for r in rules][:20]}


def ask(user, prev=""):
    import anthropic
    r = anthropic.Anthropic().messages.create(model=MODEL, max_tokens=12000, system=SYS, messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")


def draft_article(site, idea, brain):
    banned = site.get("banned_terms") or []; medical = site.get("profile") == "medical"
    target = int(idea.get("word_count") or 1200)
    base = {"site": {"niche": site.get("niche"), "profile": site.get("profile"), "brand_voice": site.get("brand_voice"), "banned_terms": banned},
            "brain": brain, "post": {"title_idea": idea["title"], "primary_keyword": idea["primary_keyword"],
            "secondary_keywords": idea.get("secondary_keywords"), "post_type": idea.get("post_type"), "search_intent": idea.get("intent"),
            "target_words": target}}
    best, feedback, prev = None, "", ""
    for attempt in range(3):
        user = json.dumps(base) if not prev else json.dumps({**base, "task": "REVISE the article below. Keep the facts and headings. Fix every problem listed.",
                                                           "problems": feedback, "previous_article": prev})
        t = ask(user); art = tag(t, "article")
        if not art: feedback = "You must return the <article> tag with HTML."; continue
        g, words, issues = grade(art), len(plain(art).split()), lint(art, banned, medical)
        bad = []
        if g > TARGET_GRADE: bad.append(f"reading grade is {g}, must be {TARGET_GRADE} or lower: split long sentences (under 11 words on average), use simpler words")
        if words < target * 0.85: bad.append(f"too short: {words} words, need about {target}")
        if words > target * 1.2: bad.append(f"too long: {words} words, need about {target}")
        bad += issues
        score = max(0, g - TARGET_GRADE) * 2 + len(issues) * 3 + (5 if words < target * 0.85 or words > target * 1.2 else 0)
        if best is None or score < best["score"]:
            best = {"score": score, "t": t, "art": art, "grade": g, "words": words, "issues": issues}
        if not bad: break
        feedback, prev = "; ".join(bad), art
    if not best: raise RuntimeError("Claude returned no article")
    return best


def make_images(prompts, profile):
    key = os.environ.get("OPENAI_API_KEY")
    if not key: return [], "OPENAI_API_KEY missing"
    style = IMG_STYLE["medical"] if profile == "medical" else IMG_STYLE["default"]; out, note = [], ""
    for p in prompts[:3]:
        try:
            body = {"model": IMG_MODEL, "prompt": style + str(p)[:600], "size": "1536x1024", "n": 1}
            if IMG_MODEL.startswith("gpt-image"): body["quality"] = os.environ.get("OPENAI_IMAGE_QUALITY", "medium")
            r = requests.post("https://api.openai.com/v1/images/generations", headers={"Authorization": f"Bearer {key}"}, json=body, timeout=240)
            if not r.ok: raise RuntimeError(f"{r.status_code} {r.text[:400]}")
            d = r.json()["data"][0]
            raw = base64.b64decode(d["b64_json"]) if d.get("b64_json") else requests.get(d["url"], timeout=60).content
            out.append((raw, str(p)))
        except Exception as e:
            note = f"image failed: {str(e)[:420]}"
    return out, note


def to_webp(raw):
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGB"); b = io.BytesIO(); im.save(b, "WEBP", quality=82); return b.getvalue(), "image/webp", "webp"
    except Exception:
        return raw, "image/png", "png"


def upload_media(w, raw, name, alt):
    data, ctype, ext = to_webp(raw)
    r = requests.post(w.base + "/wp-json/wp/v2/media", auth=w.auth, timeout=120, data=data,
                      headers={"Content-Disposition": f'attachment; filename="{name}.{ext}"', "Content-Type": ctype})
    r.raise_for_status(); m = r.json()
    requests.post(f"{w.base}/wp-json/wp/v2/media/{m['id']}", auth=w.auth, timeout=60, json={"alt_text": alt, "title": alt[:80]})
    return {"id": m["id"], "url": m["source_url"], "alt": alt}


def place_images(art, imgs):
    """Put image 1 after the intro, the others after the first paragraph of the 3rd and 5th section."""
    if not imgs: return art
    fig = lambda i: (f'<figure class="wp-block-image size-large"><img src="{i["url"]}" alt="{htmlmod.escape(i["alt"], True)}" '
                     f'width="1536" height="1024" loading="lazy" /></figure>')
    parts = re.split(r"(?i)(?=<h2[ >])", art); spots = {(0 if parts[0].strip() else 1): 0, 3: 1, 5: 2}; out = []
    for n, part in enumerate(parts):
        k = spots.get(n)
        if k is not None and k < len(imgs):
            m = re.search(r"</p>", part, re.I)
            if m: part = part[:m.end()] + fig(imgs[k]) + part[m.end():]
        out.append(part)
    return "".join(out)


def write_one(site, idea, w):
    brain = brain_context(site["id"], site.get("profile"))
    best = draft_article(site, idea, brain); t = best["t"]
    try: prompts = json.loads(tag(t, "image_prompts") or "[]")
    except ValueError: prompts = []
    if not prompts: prompts = [idea["primary_keyword"] + " concept", idea["title"] + " concept"]
    raw, note = make_images(prompts, site.get("profile")); slug = slugify(tag(t, "slug") or idea["primary_keyword"]); imgs = []
    for n, (b, p) in enumerate(raw):
        try: imgs.append(upload_media(w, b, f"{slug}-{n + 1}", f"{idea['primary_keyword']}: {p}"[:120]))
        except Exception as e: note = f"upload failed: {str(e)[:150]}"
    body = place_images(best["art"], imgs); title = idea["title"]
    mt, md = tag(t, "meta_title")[:70] or title, tag(t, "meta_description")[:170]
    r = requests.post(w.base + "/wp-json/wp/v2/posts", auth=w.auth, timeout=90,
                      json={"title": title, "content": body, "status": "draft", "slug": slug, "excerpt": md,
                            **({"featured_media": imgs[0]["id"]} if imgs else {})})
    r.raise_for_status(); wp_id = r.json()["id"]
    try: w.set_seo(wp_id, mt, md)
    except Exception as e: note = (note + " | " if note else "") + f"seo meta failed: {str(e)[:120]}"
    row = {"site_id": site["id"], "idea_id": idea["id"], "title": title, "slug": slug, "html": body, "meta_title": mt, "meta_description": md,
           "target_words": idea.get("word_count"), "words": best["words"], "grade": best["grade"], "issues": best["issues"] + ([note] if note else []),
           "images": imgs, "wp_post_id": wp_id, "status": "draft"}
    sb.insert("content_posts", [row]); return row


def write(site_id):
    from . import run, wp
    cfg = run.sites()[site_id]; site = run.ensure_site(cfg); w = wp.WP(cfg)
    if site["mode"] == "paused": return print("site paused")
    ideas = sb.select("content_ideas", site_id=f"eq.{site_id}", status="eq.approved", attempts="lt.2", order="decided_at.asc", limit=str(MAX_DRAFTS))
    if not ideas: return print("no approved ideas waiting")
    for idea in ideas:
        sb.update("content_ideas", {"id": idea["id"]}, {"status": "drafting", "attempts": (idea.get("attempts") or 0) + 1})
        try:
            row = write_one(site, idea, w)
            sb.update("content_ideas", {"id": idea["id"]}, {"status": "drafted", "error": None})
            print("draft ready:", row["title"], "| grade", row["grade"], "| words", row["words"], "| issues", row["issues"])
        except Exception as e:
            sb.update("content_ideas", {"id": idea["id"]}, {"status": "approved", "error": str(e)[:300]}); print("FAILED", idea["title"], e)


def publish(site_id):
    from . import run, wp
    cfg = run.sites()[site_id]; site = run.ensure_site(cfg); w = wp.WP(cfg)
    if site["mode"] == "paused": return print("site paused")
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
    done = len(sb.select("content_posts", site_id=f"eq.{site_id}", status="eq.published", published_at=f"gte.{since}", select="id"))
    room = int(site.get("posts_per_day") or 1) - done
    if room <= 0: return print("daily post limit reached")
    for p in sb.select("content_posts", site_id=f"eq.{site_id}", status="eq.approved", order="approved_at.asc", limit=str(room)):
        if site.get("reviewer_required") and not p.get("reviewed_at"): print("skipped, not reviewed yet:", p["title"]); continue
        probs = lint(p["html"], site.get("banned_terms") or [], site.get("profile") == "medical")
        if probs: sb.update("content_posts", {"id": p["id"]}, {"status": "draft", "issues": probs}); print("sent back:", p["title"], probs); continue
        try:
            r = requests.post(f"{w.base}/wp-json/wp/v2/posts/{p['wp_post_id']}", auth=w.auth, json={"status": "publish"}, timeout=60); r.raise_for_status()
            sb.update("content_posts", {"id": p["id"]}, {"status": "published", "published_at": dt.datetime.now(dt.timezone.utc).isoformat(), "publish_url": r.json().get("link")})
            if p.get("idea_id"): sb.update("content_ideas", {"id": p["idea_id"]}, {"status": "published"})
            print("published:", p["title"], r.json().get("link"))
        except Exception as e:
            sb.update("content_posts", {"id": p["id"]}, {"error": str(e)[:300]}); print("PUBLISH FAILED", p["title"], e)


if __name__ == "__main__":
    {"write": write, "publish": publish}[sys.argv[1]](sys.argv[2])
