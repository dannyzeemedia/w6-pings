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


# Strong signs a sponsor's domain is no longer theirs. Deliberately narrow: this retires companies automatically.
HIJACK = re.compile(r"\bcasino\b|\bbetting\b|\bgambl|\bpoker\b|slot\s*gacor|\btogel\b|\bjudi\b|\bporn|viagra|payday loan|"
                    r"domain (?:name )?(?:is )?for sale|buy this domain|this domain (?:may be|is) for sale|parked (?:free|domain)|domain parking|"
                    r"hugedomains|dan\.com|sedo\.com|afternic", re.I)


def is_gone(domain):
    """True only when the domain no longer exists at all (NXDOMAIN), not for a slow or erroring site."""
    import socket
    try:
        socket.getaddrinfo(domain, 443)
        return False
    except socket.gaierror as ex:
        return ex.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -5))
    except Exception:
        return False


def _root(d):
    """Registered domain, roughly: shop.centro.com -> centro.com, foo.co.uk -> foo.co.uk."""
    parts = (d or "").lower().removeprefix("www.").split(".")
    return ".".join(parts[-3:] if len(parts) > 2 and len(parts[-2]) <= 3 and len(parts[-1]) == 2 else parts[-2:])


def lookup(domain):
    """(logo_url, description, dead_reason, moved_to) from the homepage. dead_reason is set when the site is hijacked,
    parked or gone; moved_to when it now lands on a different company's domain (a possible acquisition or rebrand)."""
    try:
        r = requests.get(f"https://{domain}", headers=UA, timeout=8, allow_redirects=True)
        r.encoding = r.encoding if r.encoding and r.encoding.lower() not in ("iso-8859-1", "latin-1") else "utf-8"
        page, base = r.text[:400000], r.url
    except Exception:
        if is_gone(domain) and is_gone("www." + domain):
            return None, "⚠️ Their website no longer exists. The company has likely closed.", f"{domain} no longer exists (no DNS record)", None
        return None, None, None, None
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
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    probe = " ".join(filter(None, [desc, title and _html.unescape(title.group(1)), urlparse(base).netloc]))
    dead = None
    if HIJACK.search(probe):
        dead = f"{domain} now shows unrelated content ({HIJACK.search(probe).group(0)})"
        desc = "⚠️ Their website has been taken over or parked (it's showing unrelated content). Retired from outreach."
        logo = None
    if desc and len(desc) > 220 and not desc.startswith("⚠️"):
        desc = desc[:217].rsplit(" ", 1)[0] + "…"
    landed = urlparse(base).netloc.lower()
    moved = landed.removeprefix("www.") if landed and _root(landed) != _root(domain) and not dead else None
    return logo, desc, dead, moved


def retire_partner(pid, reason):
    """Stop all outreach to a company whose website is gone or hijacked. Keeps their history; reversible via Stage."""
    f = at.get("partners", pid)["fields"]
    if f.get("Stage") == "Out of Service":
        return False
    note = f"🤖 {dt.date.today().isoformat()}: retired from outreach, {reason}."
    at.update("partners", pid, {"Stage": "Out of Service", "Notes": ((f.get("Notes") or "").rstrip() + "\n\n" + note).strip()})
    for c in at.list_records("contacts", f"FIND('{pid}', ARRAYJOIN({{Partner}}))", fields=["Status"]):
        if c["fields"].get("Status") in (None, "Active"):
            at.update("contacts", c["id"], {"Status": "Do Not Contact", "Dead Reason": note})
    for d in at.list_records("drafts", f"AND(OR({{Status}}='Pending Approval', {{Status}}='Approved'), FIND('{pid}', ARRAYJOIN({{Partner}})))", fields=["Status"]):
        at.update("drafts", d["id"], {"Status": "Cancelled", "Replan Note": "Their website is gone or hijacked, so the company was retired from outreach."})
    return True


def logo_is_light(url):
    """True when a logo is white or very light on a transparent background (made for dark browser tabs), so it
    would vanish on a white chip. False when it's fine, None when we can't tell (SVG, unreachable)."""
    if not url or url.startswith("data:image/svg") or url.lower().split("?")[0].endswith(".svg"):
        return None
    try:
        from PIL import Image
        import io
        r = requests.get(url, headers=UA, timeout=8)
        im = Image.open(io.BytesIO(r.content)).convert("RGBA")
        im.thumbnail((64, 64))
        px = [p for p in im.getdata() if p[3] > 128]
        if len(px) < 20:
            return None
        lum = sum((0.2126 * p[0] + 0.7152 * p[1] + 0.0722 * p[2]) / 255 for p in px) / len(px)
        see_through = sum(1 for p in im.getdata() if p[3] <= 128) / (im.size[0] * im.size[1])
        return lum > 0.82 and see_through > 0.15
    except Exception:
        return None


def logo_tile_color(url):
    """'#rrggbb' when a logo is a solid square tile (its corners are opaque and the same colour), so the chip can
    wear that colour and the icon reads as a rounded app icon. None for logos on a transparent background."""
    if not url or url.startswith("data:image/svg") or url.lower().split("?")[0].endswith(".svg"):
        return None
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(requests.get(url, headers=UA, timeout=8).content)).convert("RGBA")
        im.thumbnail((96, 96))
        w, h = im.size
        k = max(1, min(w, h) // 12)
        pts = [(x, y) for cx, cy in ((k, k), (w - 1 - k, k), (k, h - 1 - k), (w - 1 - k, h - 1 - k))
               for x in (cx - 1, cx, cx + 1) for y in (cy - 1, cy, cy + 1) if 0 <= x < w and 0 <= y < h]
        px = [im.getpixel(p) for p in pts]
        if any(p[3] < 240 for p in px):
            return None
        r, g, b = (sum(p[i] for p in px) // len(px) for i in range(3))
        if max(max(abs(p[i] - c) for i, c in enumerate((r, g, b))) for p in px) > 28:
            return None  # corners disagree: a photo or a busy image, not a flat tile
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return None


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
    dead = None
    if dom:
        logo, desc, dead, moved = lookup(dom)
        if moved and not f.get("🤖 Fate Checked"):
            fields["🤖 What Happened"] = f"Website now goes to {moved}. Not looked into yet (the overnight run searches what happened)."
        if logo:
            fields["🤖 Logo URL"] = logo
            fields["🤖 Logo On Dark"] = bool(logo_is_light(logo))
            fields["🤖 Logo Bg"] = logo_tile_color(logo) or ""
        if desc:
            fields["🤖 What They Do"] = desc
        if not f.get("Website"):
            fields["Website"] = f"https://{dom}"
    at.update("partners", pid, fields)
    if dead:
        retire_partner(pid, dead)
    return dead


def for_partners(pids):
    """{pid: {logo, desc, website, domain}} for display; falls back to the site's favicon until the lookup has run."""
    pids = [p for p in set(pids) if p]
    if not pids:
        return {}
    formula = "OR(" + ",".join(f"RECORD_ID()='{i}'" for i in pids[:90]) + ")"
    out = {}
    for r in at.list_records("partners", formula, fields=["Name", "Website", "🤖 Logo URL", "🤖 What They Do", "🤖 Logo On Dark", "🤖 Logo Bg"]):
        f = r["fields"]
        dom = domain_from(f.get("Website"))
        out[r["id"]] = {"name": f.get("Name"), "logo": f.get("🤖 Logo URL") or favicon(dom), "desc": f.get("🤖 What They Do"),
                        "dark": bool(f.get("🤖 Logo On Dark") and f.get("🤖 Logo URL")),
                        "bg": f.get("🤖 Logo Bg") if f.get("🤖 Logo URL") else None,
                        "website": f.get("Website") or (f"https://{dom}" if dom else None), "domain": dom}
    return out


def site_name(domain):
    """The company's own name from its homepage (og:site_name, else the title before any separator), or None."""
    try:
        r = requests.get(f"https://{domain}", headers=UA, timeout=8)
        page = r.text[:200000]
    except Exception:
        return None
    m = re.search(r"""<meta[^>]+property=["']og:site_name["'][^>]*>""", page, re.I)
    name = _attr(m.group(0), "content") if m else None
    if not name:
        t = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
        name = _html.unescape(t.group(1)).strip() if t else None
        if name:
            name = re.split(r"\s+[|\-–—:·]\s+", name)[0].strip()
    if not name or len(name) > 40 or HIJACK.search(name):
        return None
    return name
