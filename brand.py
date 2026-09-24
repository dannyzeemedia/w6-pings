"""Logos and one-line 'what they do' for sponsor brands, read from their own website and cached on the Partners row."""
import datetime as dt
import html as _html
import re
from urllib.parse import urljoin, urlparse
import requests
import airtable as at

FREE = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "me.com", "aol.com", "live.com", "googlemail.com"}
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}


def domain_from(website=None, email=None):
    if website:
        d = urlparse(website if "//" in website else "https://" + website).netloc.lower()
        if d:
            return d.removeprefix("www.")
    if email and "@" in email:
        d = email.split("@")[1].lower()
        if d not in FREE:
            return d
    return None


def favicon(domain):
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=128" if domain else None


def _attr(tag, name):
    m = re.search(name + r"""\s*=\s*["']([^"']+)["']""", tag, re.I)
    return _html.unescape(m.group(1)) if m else None


def lookup(domain):
    """(logo_url, description) from the homepage. Prefers a real logo image, then a big icon; description from meta tags."""
    try:
        r = requests.get(f"https://{domain}", headers=UA, timeout=8, allow_redirects=True)
        r.encoding = r.encoding if r.encoding and r.encoding.lower() not in ("iso-8859-1", "latin-1") else "utf-8"
        page, base = r.text[:400000], r.url
    except Exception:
        return None, None
    logo = None
    if not logo:
        icons = []
        for tag in re.findall(r"<link\b[^>]*>", page, re.I):
            rel = (_attr(tag, "rel") or "").lower()
            href = _attr(tag, "href")
            if href and ("apple-touch-icon" in rel or "icon" in rel):
                size = max([int(x) for x in re.findall(r"(\d+)x\d+", _attr(tag, "sizes") or "")] or [180 if "apple" in rel else 32])
                icons.append((size, urljoin(base, href)))
        icons = [i for i in icons if not re.search(r"pfavico|wixstatic.*favicon|/favicon\.ico$", i[1], re.I) or i[0] >= 64]
        if icons:
            logo = max(icons)[1]
    desc = None
    for pat in (r"""<meta[^>]+property=["']og:description["'][^>]*>""", r"""<meta[^>]+name=["']description["'][^>]*>""",
                r"""<meta[^>]+name=["']twitter:description["'][^>]*>"""):
        m = re.search(pat, page, re.I)
        if m and _attr(m.group(0), "content"):
            desc = re.sub(r"\s+", " ", _attr(m.group(0), "content")).strip()
            break
    if desc and re.search(r"casino|betting|gambl|\bslots?\b|poker|porn|viagra|payday loan|domain (?:is )?for sale|buy this domain|parked", desc, re.I):
        desc = "⚠️ Their website looks like it's been taken over or parked (it's showing unrelated content). The company may have closed or moved; check before emailing them."
    if desc and len(desc) > 220 and not desc.startswith("⚠️"):
        desc = desc[:217].rsplit(" ", 1)[0] + "…"
    return logo, desc


def enrich_partner(pid):
    """Look the brand up once (or monthly) and cache it on the Partners row."""
    f = at.get("partners", pid)["fields"]
    checked = f.get("🤖 Brand Checked")
    if checked and (dt.date.today() - dt.date.fromisoformat(checked)).days < 30:
        return
    dom = domain_from(f.get("Website"))
    if not dom:
        emails = re.findall(r"[\w.+-]+@([\w-]+\.[\w.-]+)", f.get("Contact") or "")
        dom = next((e.lower() for e in emails if e.lower() not in FREE), None)
    fields = {"🤖 Brand Checked": dt.date.today().isoformat()}
    if dom:
        logo, desc = lookup(dom)
        if logo:
            fields["🤖 Logo URL"] = logo
        if desc:
            fields["🤖 What They Do"] = desc
        if not f.get("Website"):
            fields["Website"] = f"https://{dom}"
    at.update("partners", pid, fields)


def for_partners(pids):
    """{pid: {logo, desc, website, domain}} for display; falls back to the site's favicon until the lookup has run."""
    pids = [p for p in set(pids) if p]
    if not pids:
        return {}
    formula = "OR(" + ",".join(f"RECORD_ID()='{i}'" for i in pids[:90]) + ")"
    out = {}
    for r in at.list_records("partners", formula, fields=["Name", "Website", "🤖 Logo URL", "🤖 What They Do"]):
        f = r["fields"]
        dom = domain_from(f.get("Website"))
        out[r["id"]] = {"name": f.get("Name"), "logo": f.get("🤖 Logo URL") or favicon(dom), "desc": f.get("🤖 What They Do"),
                        "website": f.get("Website") or (f"https://{dom}" if dom else None), "domain": dom}
    return out
