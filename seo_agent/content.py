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
IMG_STYLE = {"medical": "Photorealistic editorial photograph, natural window light, soft neutral and warm palette, shallow depth of field, "
                        "50mm lens, high detail, natural skin texture, calm mood. Adults only, always fully clothed in plain everyday clothes "
                        "(sports top, tank top, loose shirt). No text, no letters, no logos, no watermarks, no nudity, no bare breasts or genitals, "
                        "no scars, marks, bruises or surgical lines, no before-and-after, no surgery, no medical procedures, no syringes. Scene: ",
             "default": "Photorealistic editorial photograph, natural light, soft palette, shallow depth of field, high detail. "
                        "No text, no letters, no logos, no watermarks. Scene: "}
BANNER_NOTE = " Composition: main subject on the right side of the frame; keep the left 45 percent calm, soft and uncluttered for a headline."
CAPTION = os.environ.get("IMAGE_CAPTION", "Illustrative image")
CAPTION_HTML = f"<figcaption>{CAPTION}</figcaption>" if CAPTION else ""
FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/Poppins-Bold.ttf"

SYS = """You write blog posts for a website. The reader is a normal adult who is not an expert. Write like a careful, kind human.
READABILITY: aim for US school grade 5 or lower. Short sentences (average under 12 words). Common words. When a medical or technical
term is needed, say it once and explain it in plain words in the same sentence. Active voice. One idea per sentence.
RHYTHM: mostly short sentences, but mix lengths on purpose. Add a few sentences of 14 to 20 words and a few of 3 to 6 words. Use contractions (it's, don't, you'll) now and then. Use small everyday examples. Do not make every sentence the same shape.
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
<image_prompts>["scene 1: wide calm lifestyle photo related to the topic","scene 2","scene 3"]</image_prompts>
IMAGE SCENES (photo ideas, decided by the topic): scene 1 is the banner. If the topic is about a body area or a cosmetic procedure, scene 1 is a clothed close-up of that area (for example torso in a plain sports top, waist and hips in plain leggings, side profile of the face), calm, no examination, no scars. Otherwise scene 1 is a calm lifestyle photo. Scenes 2 and 3 are ordinary life (a person resting at home, a bright kitchen, a doctor and patient talking at a desk). Never before-and-after, never an operation.
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


HUM = """You are a careful human editor. Rewrite the article so it reads like a real person wrote it, not a machine. Keep every fact,
every heading and the same HTML tags (h2, h3, p, ul, li only). Do not add facts, numbers or claims. Keep the meaning and the length
(within 10 percent). Keep the language simple: short common words, reading grade about 5 to 6.
Make the rhythm uneven the way real writing is: some very short sentences, some longer ones of 15 to 22 words. Start sentences in
different ways. Use contractions now and then. Replace stiff phrases with plain ones. Cut filler and any sentence that only repeats the
point. Remove lists of exactly three that look forced. """ + RULES + """
Reply with the article HTML only inside <article></article>."""


def humanize_pass(art, banned, medical, target):
    import anthropic
    r = anthropic.Anthropic().messages.create(model=MODEL, max_tokens=12000, system=HUM, messages=[{"role": "user", "content": art}])
    new = tag("".join(b.text for b in r.content if getattr(b, "type", "") == "text"), "article")
    if not new: return art, False
    w0, w1 = len(plain(art).split()), len(plain(new).split())
    if grade(new) > TARGET_GRADE + 0.7 or lint(new, banned, medical) or not (0.85 * w0 <= w1 <= 1.15 * w0): return art, False
    return new, True


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
    try:
        new, ok = humanize_pass(best["art"], banned, medical, target)
        if ok: best.update(art=new, grade=grade(new), words=len(plain(new).split()), issues=lint(new, banned, medical))
        print("humanizer pass:", "applied" if ok else "skipped (rewrite broke a rule)")
    except Exception as e:
        print("humanizer pass failed:", str(e)[:150])
    return best


PREFER = ["gpt-image-2", "gpt-image-1.5", "gpt-image-1", "gpt-image-1-mini", "chatgpt-image-latest", "dall-e-3"]


def pick_image_model(key):
    if os.environ.get("OPENAI_IMAGE_MODEL"): return os.environ["OPENAI_IMAGE_MODEL"], ""
    try:
        r = requests.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=30); r.raise_for_status()
        ids = [m["id"] for m in r.json()["data"]]
    except Exception as e:
        return None, f"could not list OpenAI models: {str(e)[:120]}"
    for p in PREFER:
        if p in ids: return p, ""
    cand = sorted(i for i in ids if "image" in i or i.startswith("dall-e"))
    return (cand[0], "") if cand else (None, "no image model on this OpenAI key; models seen: " + ", ".join(sorted(ids)[:12]))


def make_images(prompts, profile):
    key = os.environ.get("OPENAI_API_KEY")
    if not key: return [], "OPENAI_API_KEY missing"
    model, why = pick_image_model(key)
    if not model: return [], why
    print("image model:", model)
    style = IMG_STYLE["medical"] if profile == "medical" else IMG_STYLE["default"]; out, note = [], ""
    for idx, p in enumerate(prompts[:3]):
        prompt = style + str(p)[:600] + (BANNER_NOTE if idx == 0 else ""); last = ""
        quality = os.environ.get("OPENAI_IMAGE_QUALITY_BANNER", "high") if idx == 0 else os.environ.get("OPENAI_IMAGE_QUALITY", "medium")
        for body in ({"model": model, "prompt": prompt, "size": "1536x1024", "n": 1}, {"model": model, "prompt": prompt}):
            try:
                if body.get("size") and model.startswith("gpt-image"): body["quality"] = quality
                r = requests.post("https://api.openai.com/v1/images/generations", headers={"Authorization": f"Bearer {key}"}, json=body, timeout=240)
                if not r.ok: last = f"{model}: {r.status_code} {r.text[:300]}"; continue
                d = r.json()["data"][0]
                raw = base64.b64decode(d["b64_json"]) if d.get("b64_json") else requests.get(d["url"], timeout=60).content
                out.append((raw, str(p))); last = ""; break
            except Exception as e:
                last = f"{model}: {str(e)[:300]}"
        if last and ("moderation" in last.lower() or "safety" in last.lower()):
            try:
                body = {"model": model, "prompt": style + "A calm adult in everyday clothes resting in a bright room at home, face turned slightly away." + (BANNER_NOTE if idx == 0 else "")}
                r = requests.post("https://api.openai.com/v1/images/generations", headers={"Authorization": f"Bearer {key}"}, json=body, timeout=240)
                if r.ok:
                    d = r.json()["data"][0]; raw = base64.b64decode(d["b64_json"]) if d.get("b64_json") else requests.get(d["url"], timeout=60).content
                    out.append((raw, "calm lifestyle photo")); last = ""
            except Exception as e:
                last = f"{model}: {str(e)[:200]}"
        if last: note = "image failed: " + last
    return out, note


def to_webp(raw):
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGB"); b = io.BytesIO(); im.save(b, "WEBP", quality=82)
        return b.getvalue(), "image/webp", "webp", im.size
    except Exception:
        return raw, "image/png", "png", (1536, 1024)


def get_font(size):
    from PIL import ImageFont
    path = "/tmp/Poppins-Bold.ttf"
    try:
        if not os.path.exists(path):
            r = requests.get(FONT_URL, timeout=30); r.raise_for_status(); open(path, "wb").write(r.content)
        return ImageFont.truetype(path, size)
    except Exception:
        for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
            if os.path.exists(p): return ImageFont.truetype(p, size)
        return ImageFont.load_default()


def fetch_logo(site_id):
    try:
        rows = sb.select("brain_items", site_id=f"eq.{site_id}", kind="eq.logo", status="eq.active", select="file_path", order="created_at.desc", limit="1")
        if not rows or not rows[0].get("file_path"): return None
        k = os.environ["SUPABASE_SERVICE_KEY"]
        r = requests.get(os.environ["SUPABASE_URL"].rstrip("/") + "/storage/v1/object/brain/" + rows[0]["file_path"],
                         headers={"apikey": k, "Authorization": f"Bearer {k}"}, timeout=30)
        return r.content if r.ok else None
    except Exception:
        return None


def compose_banner(raw, title, color, logo):
    """Our own banner: generated photo + soft light panel + headline (words before ':' in the brand colour) + logo."""
    from PIL import Image, ImageDraw
    im = Image.open(io.BytesIO(raw)).convert("RGB"); W, H = im.size
    fade = Image.new("L", (W, H), 0); px = fade.load(); edge = int(W * 0.62)
    for x in range(edge):
        a = int(235 * (1 - x / edge) ** 0.9)
        for y in range(H): px[x, y] = a
    im.paste(Image.new("RGB", (W, H), (255, 255, 255)), (0, 0), fade)
    d = ImageDraw.Draw(im); head, _, rest = title.partition(":"); hw = head.split(); words = ([(w, True) for w in hw[:-1]] + [(hw[-1] + ":", True)] if hw and rest else [(w, True) for w in hw]) + [(w, False) for w in rest.split()]
    if not rest: words = [(w, i < 3) for i, w in enumerate(head.split())]
    size = int(H * 0.072); maxw = int(W * 0.5); x0, y0 = int(W * 0.06), int(H * 0.16)
    while True:
        font = get_font(size); lines, cur, curw = [], [], 0
        for w, acc in words:
            ww = d.textlength(w + " ", font=font)
            if cur and curw + ww > maxw: lines.append(cur); cur, curw = [], 0
            cur.append((w, acc)); curw += ww
        lines.append(cur)
        if len(lines) * size * 1.25 < H * 0.62 or size < 26: break
        size -= 4
    y = y0
    for ln in lines:
        x = x0
        for w, acc in ln:
            d.text((x, y), w, font=font, fill=color if acc else "#0f1b33"); x += d.textlength(w + " ", font=font)
        y += int(size * 1.25)
    d.rectangle([x0, y + int(size * 0.2), x0 + int(W * 0.09), y + int(size * 0.2) + 6], fill=color)
    if CAPTION:
        f2 = get_font(max(14, int(H * 0.026))); tw = d.textlength(CAPTION, font=f2); px_, py_ = W - tw - int(W * 0.02), H - int(H * 0.055)
        d.text((px_ + 1, py_ + 1), CAPTION, font=f2, fill=(0, 0, 0)); d.text((px_, py_), CAPTION, font=f2, fill=(255, 255, 255))
    if logo:
        try:
            lg = Image.open(io.BytesIO(logo)).convert("RGBA"); h = int(H * 0.09); lg = lg.resize((int(lg.width * h / lg.height), h))
            im.paste(lg, (x0, H - h - int(H * 0.06)), lg)
        except Exception: pass
    b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()


def upload_media(w, raw, name, alt):
    data, ctype, ext, size = to_webp(raw)
    r = requests.post(w.base + "/wp-json/wp/v2/media", auth=w.auth, timeout=120, data=data,
                      headers={"Content-Disposition": f'attachment; filename="{name}.{ext}"', "Content-Type": ctype})
    r.raise_for_status(); m = r.json()
    requests.post(f"{w.base}/wp-json/wp/v2/media/{m['id']}", auth=w.auth, timeout=60, json={"alt_text": alt, "title": alt[:80]})
    return {"id": m["id"], "url": m["source_url"], "alt": alt, "w": size[0], "h": size[1]}


def place_images(art, imgs):
    """Put image 1 after the intro, the others after the first paragraph of the 3rd and 5th section."""
    if not imgs: return art
    fig = lambda i: (f'<figure class="wp-block-image size-large"><img src="{i["url"]}" alt="{htmlmod.escape(i["alt"], True)}" '
                     f'width="{i.get("w", 1536)}" height="{i.get("h", 1024)}" loading="lazy" />{CAPTION_HTML}</figure>')
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
    title = idea["title"]; logo = fetch_logo(site["id"]); color = site.get("brand_color") or "#2563eb"
    for n, (b, p) in enumerate(raw):
        try:
            if n == 0:
                try: b = compose_banner(b, title, color, logo)
                except Exception as e: note = f"banner text failed: {str(e)[:120]}"
            imgs.append(upload_media(w, b, f"{slug}-{n + 1}", (title if n == 0 else f"{idea['primary_keyword']}: {p}")[:120]))
        except Exception as e: note = f"upload failed: {str(e)[:150]}"
    body = place_images(best["art"], imgs[1:])
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
