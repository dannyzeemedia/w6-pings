"""The "Publish to W6" connector: a small MCP server Claire (and the team) add to Claude, so a page made in Claude goes
live at partners.workspace6.io/<slug>. Each person has their own private connector link (the token in the URL), which
says who published what: you can update or take down your own pages, never someone else's."""
import base64
import json
import os
import re
import requests
import airtable as at
import partner_pages

PROTOCOL = "2025-06-18"
RESERVED = {"admin", "api", "login", "static", "assets", "robots.txt", "favicon.ico", "healthz", "mcp", "www", "index"}
MAX_BYTES = 5 * 1024 * 1024
BASE_URL = f"https://{partner_pages.HOST}"

TOOLS = [
    {"name": "publish_page",
     "description": ("Publish a web page (a complete, self-contained HTML document, e.g. an artifact you made) at "
                     f"{BASE_URL}/<slug>. Publishing to a slug you already own replaces that page. Returns the live link. "
                     "Pages are public to anyone with the link and hidden from search engines. Pages that rely on Claude-only "
                     "features (window.claude, storage) won't work outside Claude."),
     "inputSchema": {"type": "object", "properties": {
         "slug": {"type": "string", "description": "The address after partners.workspace6.io/: lowercase letters, numbers and hyphens, e.g. 'levanta' or 'q4-media-kit'."},
         "html": {"type": "string", "description": "The full HTML document, starting with <!doctype html>. Inline CSS/JS; images as data URIs or https links."},
         "title": {"type": "string", "description": "A short name for the page, for the team's list."}},
         "required": ["slug", "html"]}},
    {"name": "list_pages",
     "description": "List the pages published to partners.workspace6.io, with their links and who published each.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "unpublish_page",
     "description": "Take one of your pages down. Its link then shows 'Nothing here yet'. You can publish to the slug again later.",
     "inputSchema": {"type": "object", "properties": {"slug": {"type": "string"}}, "required": ["slug"]}},
]


def users():
    try:
        return json.loads(os.environ.get("PAGES_TOKENS") or "{}")  # {"<token>": "claire", ...}
    except ValueError:
        return {}


def _rows(formula=None):
    params = {"pageSize": 100, **({"filterByFormula": formula} if formula else {})}
    return at._fetch("GET", f"{at.API}/{at.TABLES['pages']}", None, params=params)["records"]


def _clean_slug(s):
    s = (s or "").strip().strip("/").lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s).strip("-")
    return s[:80]


def publish(who, args):
    slug, html, title = _clean_slug(args.get("slug")), args.get("html") or "", (args.get("title") or "").strip()
    if not slug or slug in RESERVED:
        return f"'{args.get('slug')}' can't be used as a page address. Pick another (lowercase letters, numbers, hyphens)."
    data = html.encode("utf-8")
    if not data.strip():
        return "There's no HTML to publish."
    if len(data) > MAX_BYTES:
        return f"That page is {len(data) // 1048576} MB; the limit is 5 MB. Link big images instead of embedding them."
    if "<html" not in html.lower() and "<body" not in html.lower():
        html = f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title or slug}</title></head><body>{html}</body></html>'
        data = html.encode("utf-8")
    hit = _rows(f"LOWER({{Slug}})='{slug}'")
    if hit and (hit[0]["fields"].get("Published By") or who) != who:
        return f"{BASE_URL}/{slug} belongs to {hit[0]['fields'].get('Published By')}. Pick a different address."
    if hit:
        rid = hit[0]["id"]
        at.update("pages", rid, {"Page": [], "Live": True, **({"Title": title} if title else {})})
    else:
        rid = at.create("pages", {"Slug": slug, "Title": title or slug, "Published By": who, "Live": True})["id"]
    r = requests.post(f"https://content.airtable.com/v0/{at.BASE}/{rid}/Page/uploadAttachment", headers=at._h(), timeout=120,
                      json={"contentType": "text/html", "filename": f"{slug}.html", "file": base64.b64encode(data).decode()})
    r.raise_for_status()
    partner_pages._cache.pop(slug, None)
    return f"Live at {BASE_URL}/{slug}" + (" (updated)" if hit else "")


def list_pages(who, args):
    rows = [r for r in _rows() if r["fields"].get("Live")]
    if not rows:
        return "No pages are published yet."
    return "\n".join(f"- {BASE_URL}/{r['fields'].get('Slug')} · {r['fields'].get('Title') or ''} · by {r['fields'].get('Published By') or '?'}"
                     for r in sorted(rows, key=lambda r: r["fields"].get("Slug") or ""))


def unpublish(who, args):
    slug = _clean_slug(args.get("slug"))
    hit = _rows(f"LOWER({{Slug}})='{slug}'")
    if not hit or not hit[0]["fields"].get("Live"):
        return f"There's no live page at {BASE_URL}/{slug}."
    if (hit[0]["fields"].get("Published By") or who) != who:
        return f"That page belongs to {hit[0]['fields'].get('Published By')}; only they can take it down."
    at.update("pages", hit[0]["id"], {"Live": False})
    partner_pages._cache.pop(slug, None)
    return f"Taken down. {BASE_URL}/{slug} now shows 'Nothing here yet'."


CALLS = {"publish_page": publish, "list_pages": list_pages, "unpublish_page": unpublish}


def handle(token, msg):
    """One JSON-RPC message (MCP over streamable HTTP, plain JSON replies). Returns the reply, or None for a notification."""
    who = users().get(token)
    if not who:
        return {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32001, "message": "This connector link isn't valid."}}
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # notifications (initialized, cancelled): nothing to say back
    ok = lambda result: {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "initialize":
        return ok({"protocolVersion": (msg.get("params") or {}).get("protocolVersion") or PROTOCOL,
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "Publish to W6", "version": "1.0"},
                   "instructions": f"Publishes pages to {BASE_URL}/<slug>. Use publish_page with the complete HTML of the page."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        p = msg.get("params") or {}
        fn = CALLS.get(p.get("name"))
        if not fn:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool {p.get('name')}"}}
        try:
            text, err = fn(who, p.get("arguments") or {}), False
        except Exception as ex:
            text, err = f"That didn't work: {ex}", True
        return ok({"content": [{"type": "text", "text": text}], "isError": err})
    if method in ("resources/list", "prompts/list"):
        return ok({"resources": []} if method == "resources/list" else {"prompts": []})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
