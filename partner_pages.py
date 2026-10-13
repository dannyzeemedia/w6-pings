"""Partner pages: pages the W6 team publishes (Claude artifacts) at partners.workspace6.io/<slug>.

Served by Pings, but walled off from it: on that address NOTHING else in Pings answers (no login, no pages, no API).
Each page is a row in 🤖 Partner Pages: Slug, the HTML file in Page, and Live."""
import re
import time
import requests
import airtable as at

HOST = "partners.workspace6.io"
_cache = {}  # slug -> (fetched at, html or None)
TTL = 60

NOTHING = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Workspace6</title><meta name="robots" content="noindex">
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f7f6;color:#202020;font:16px/1.5 Inter,system-ui,sans-serif}
main{text-align:center;padding:24px}h1{font:400 32px Georgia,serif;color:#265c51;margin:0 0 8px}a{color:#265c51}</style></head>
<body><main><h1>Nothing here yet</h1><p>There's no page at this address. <a href="https://workspace6.io">Go to Workspace6</a></p></main></body></html>"""


def is_partner_host(host):
    return (host or "").split(":")[0].lower() == HOST


def page(slug):
    """The live page's HTML for this slug, or None."""
    slug = (slug or "").strip("/").lower()
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,80}$", slug):
        return None
    hit = _cache.get(slug)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    rows = at._fetch("GET", f"{at.API}/{at.TABLES['pages']}", None,
                     params={"filterByFormula": f"AND(LOWER({{Slug}})='{slug}', {{Live}})", "maxRecords": 1})["records"]
    html = None
    files = rows[0]["fields"].get("Page") if rows else None
    if files:
        r = requests.get(files[0]["url"], timeout=30)
        if r.ok:
            html = r.content.decode("utf-8", "replace")
    _cache[slug] = (time.time(), html)
    return html


def respond(path):
    """The whole partners.workspace6.io site: a page, or the plain 'nothing here' page."""
    if path in ("/healthz",):
        return "ok", 200
    if path == "/robots.txt":
        return "User-agent: *\nDisallow: /\n", 200, {"Content-Type": "text/plain"}
    try:
        html = page(path)
    except Exception:
        html = None
    if html is None:
        return NOTHING, 404, {"Content-Type": "text/html; charset=utf-8"}
    return html, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache"}
