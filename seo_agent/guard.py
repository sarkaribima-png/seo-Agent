import re
AUTO_KINDS = {"title", "meta_description"}          # applied automatically (within daily limit)
APPROVAL_KINDS = {"delete", "redirect", "sitewide_setting", "content_rewrite", "schema", "noindex"}
def check_seo(title, meta, site):
    errs = []
    if not (30 <= len(title) <= 60): errs.append(f"title length {len(title)} (need 30-60)")
    if not (110 <= len(meta) <= 160): errs.append(f"meta length {len(meta)} (need 110-160)")
    txt = f"{title} {meta}".lower()
    for t in site.get("banned_terms", []) + site.get("competitor_names", []):
        if t and t.lower() in txt: errs.append(f"banned term: {t}")
    if re.search(r"\b(rank|top 1|number one)\b", txt): errs.append("ranking claim")
    return errs
def needs_approval(kind): return kind not in AUTO_KINDS
