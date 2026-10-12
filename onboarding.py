"""Sponsor onboarding: once a sponsor has bought placements, Karlie sends them a private link that asks only for
what we need for the things they bought (Marquee / Shout-Out / Takeover / Group Buy). Answers and files land on a
row in 🤖 Sponsor Onboarding; when they're done, Clara tells #w6-team and tags Karlie."""
import base64
import datetime as dt
import json
import os
import re
import secrets

import requests

import airtable as at
import bookings as bk

KARLIE_SLACK_ID, W6_TEAM_CHANNEL = "U09JHPUV6N8", "C09J30RP80K"
PUBLIC_BASE = os.environ.get("PUBLIC_BASE_URL", "https://pings.workspace6.io")  # Pings itself (Karlie's links in Slack)
# Where sponsors fill it in: the Lovable sponsor site. Swap to https://sponsors.workspace6.io once its DNS is connected,
# and to https://workspace6.io once the main site moves to Lovable.
SPONSOR_SITE = os.environ.get("ONBOARD_SITE", "https://workspace6-sponsors.lovable.app")
UPLOAD_MAX = 5 * 1024 * 1024  # Airtable's per-file upload limit

KINDS = ["marquee", "shoutout", "takeover", "groupbuy"]  # the placements this form knows how to ask about
LABEL = {"marquee": "Marquee", "shoutout": "Shout-Out", "takeover": "Takeover", "groupbuy": "Group Buy"}

# Each question: key, kind of input, label, help, required. "when" = only required/shown when another answer matches.
CONTACT = [
    {"key": "name", "type": "text", "label": "Your name", "req": True},
    {"key": "email", "type": "email", "label": "Your email", "req": True},
    {"key": "proofs", "type": "text", "label": "Who should we send proofs to?",
     "help": "Name and email. Leave it blank if it's you."},
]

QUESTIONS = {
    "marquee": [
        {"key": "logo", "type": "file", "label": "Your logo, dark-background edition", "req": True, "accept": "image/*,.svg",
         "help": "It sits in the top and bottom “Powered by” slots, on our dark green. An SVG is ideal. If not, a "
                 "transparent PNG that's legible on dark. A white or light version usually does it."},
        {"key": "hero", "type": "file", "label": "Your mid-section hero image", "req": True, "accept": "image/*",
         "help": "Landscape or square, anywhere from 4:3 to 1:1. Not portrait. Around 650px wide and a modest file size, "
                 "so it loads before the reader moves on. Most people read this on a phone, so check it there."},
        {"key": "blurb", "type": "textarea", "label": "Your mid-section blurb, about 150 words", "req": True,
         "help": "Type it here, or paste a link to a doc if you'd like formatting. Send it as close to publishable as you "
                 "can: spelling, grammar, links. We'll very likely copy and paste it."},
        {"key": "url_top", "type": "url", "label": "Link for the top and bottom “Powered by” slots", "req": True,
         "help": "The final URL, UTMs included."},
        {"key": "url_mid", "type": "url", "label": "Link for the mid-section", "req": True,
         "help": "Also final, also with UTMs. It may well be the same as the one above. That's fine."},
    ],
    "shoutout": [
        {"key": "copy", "type": "textarea", "label": "Your Shout-Out, one to three sentences", "req": True,
         "help": "Roughly 40 to 60 words. Lead with the problem you solve, not the company history. There are examples above."},
        {"key": "link_words", "type": "text", "label": "Which words should be the link?",
         "help": "Usually your brand name, or a short call to action like “Get a free audit”."},
        {"key": "url", "type": "url", "label": "The link, with UTMs", "req": True},
        {"key": "offer", "type": "text", "label": "A member offer or code, if there is one"},
    ],
    "takeover": [
        {"key": "writer", "type": "choice", "label": "Who's writing it?", "req": True,
         "options": ["We'll write it (recommended)", "We'll send our own copy"],
         "help": "We know what this audience reads to the end. Give us the points and we'll do the rest."},
        {"key": "points", "type": "textarea", "label": "The points you want us to hit", "req": True,
         "when": ("writer", "We'll write it (recommended)")},
        {"key": "stats", "type": "textarea", "label": "Stats, results or customer wins we can use",
         "when": ("writer", "We'll write it (recommended)")},
        {"key": "avoid", "type": "textarea", "label": "Anything we shouldn't say",
         "when": ("writer", "We'll write it (recommended)")},
        {"key": "own_copy", "type": "textarea", "label": "Your copy, or a link to the doc", "req": True,
         "when": ("writer", "We'll send our own copy")},
        {"key": "cta", "type": "text", "label": "The offer or call to action", "req": True},
        {"key": "logo", "type": "file", "label": "Your logo, dark-background edition", "req": True, "accept": "image/*,.svg"},
        {"key": "images", "type": "file", "label": "Images or screenshots to use", "multiple": True, "accept": "image/*"},
        {"key": "urls", "type": "textarea", "label": "Final link or links, with UTMs", "req": True},
        {"key": "tone", "type": "textarea", "label": "Brand guidelines or tone notes"},
    ],
    "groupbuy": [
        {"key": "offer", "type": "textarea", "label": "What W6 members get", "req": True,
         "help": "Plainly. e.g. “We'll waive the implementation fee for one workflow.”"},
        {"key": "qualify", "type": "text", "label": "Who qualifies", "help": "Leave it blank if everyone does."},
        {"key": "deadline", "type": "date", "label": "When the offer closes", "req": True},
        {"key": "claim", "type": "text", "label": "How members claim it", "req": True,
         "help": "A link, or a named person's email. A real person gets more replies than a form."},
        {"key": "examples", "type": "textarea", "label": "What it's good for",
         "help": "A few concrete examples. e.g. inventory reporting, returns, PO reconciliation."},
        {"key": "must", "type": "textarea", "label": "Anything we must say, or mustn't"},
    ],
}


# ---------- links ----------
def upcoming_for(partner_id):
    """This sponsor's placements from a week ago onwards, that this form knows how to ask about."""
    today = dt.date.today()
    rows = bk.load(today - dt.timedelta(days=7), today + dt.timedelta(days=400))
    return [b for b in rows if b.get("partner") == partner_id and b["kind"] in KINDS and b["state"] in ("paid", "pencilled", "swap")]


def contacts_for(partner_id):
    """The sponsor's live contacts, the one we talk to most first (most pings back and forth)."""
    p = at.get("partners", partner_id)["fields"]
    out = []
    for cid in p.get("🤖 Outbound Contacts") or []:
        f = at.get("contacts", cid)["fields"]
        if f.get("Status") == "Dead" or not f.get("Email"):
            continue
        out.append({"name": f.get("Name") or "", "email": f["Email"], "pings": len(f.get("🤖 Outbound Ping Log") or [])})
    return sorted(out, key=lambda c: (-c["pings"], not c["name"]))


def make_order(bookings):
    items = []
    for kind in KINDS:
        mine = sorted((b for b in bookings if b["kind"] == kind), key=lambda b: b["date"])
        for i, b in enumerate(mine):
            items.append({"key": f"{kind}-{i}", "kind": kind, "n": i, "of": len(mine),
                          "date": b["date"].isoformat(), "booking": b["id"]})
    return {"items": items}


def create_link(partner_id, sponsor, bookings, contact_name="", contact_email="", created_by=""):
    token = secrets.token_urlsafe(12)
    order = make_order(bookings)
    fields = {
        "Name": f"{sponsor} · {summary(order)}",
        "Token": token,
        "Partner": [partner_id],
        "Bookings": [b["id"] for b in bookings],
        "Order": json.dumps(order),
        "Contact Name": contact_name or None,
        "Contact Email": contact_email or None,
        "Status": "Sent",
        "Created By": created_by,
    }
    return at.create("onboard", {k: v for k, v in fields.items() if v not in (None, "")})


def url_for_token(token):
    return f"{SPONSOR_SITE}/onboarding/{token}"


def by_token(token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,40}", token or ""):
        return None
    rows = at.list_records("onboard", f"{{Token}}='{token}'", max_records=1)
    return rows[0] if rows else None


def fresh(rid):
    at._forget(at.TABLES["onboard"])
    return at.get("onboard", rid)


def order_of(rec):
    try:
        return json.loads(rec["fields"].get("Order") or "{}")
    except ValueError:
        return {"items": []}


def answers_of(rec):
    try:
        return json.loads(rec["fields"].get("Answers") or "{}")
    except ValueError:
        return {}


def summary(order):
    counts = {}
    for it in order.get("items", []):
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
    return ", ".join(f"{n}× {LABEL[k]}" for k, n in counts.items()) or "nothing yet"


# ---------- saving ----------
def save_answers(rec, incoming):
    """Merge what the browser sent. Only known keys are kept, so nothing odd can be written."""
    order = order_of(rec)
    cur = answers_of(rec)
    out = {"contact": dict(cur.get("contact") or {}), "items": dict(cur.get("items") or {})}
    for q in CONTACT:
        v = (incoming.get("contact") or {}).get(q["key"])
        if v is not None:
            out["contact"][q["key"]] = str(v)[:2000]
    for it in order.get("items", []):
        got = (incoming.get("items") or {}).get(it["key"])
        if not isinstance(got, dict):
            continue
        mine = dict(out["items"].get(it["key"]) or {})
        for q in QUESTIONS[it["kind"]]:
            if q["type"] != "file" and q["key"] in got:
                mine[q["key"]] = str(got[q["key"]])[:20000]
        for flag in ("same_as_prev", "use_for_all"):
            if flag in got:
                mine[flag] = bool(got[flag])
        out["items"][it["key"]] = mine
    fields = {"Answers": json.dumps(out)}
    if rec["fields"].get("Status") == "Sent":
        fields["Status"] = "Started"
        fields["Started At"] = dt.datetime.now(dt.timezone.utc).isoformat()
    at.update("onboard", rec["id"], fields)
    return out


def files_of(rec):
    """{"marquee-0/logo": [{id, filename, url, thumb}], ...} from the Files field (filenames are prefixed with the key)."""
    out = {}
    for a in rec["fields"].get("Files") or []:
        fn = a.get("filename") or ""
        if "__" not in fn:
            continue
        key, name = fn.split("__", 1)
        out.setdefault(key.replace("~", "/"), []).append({
            "id": a.get("id"), "filename": name, "url": a.get("url"),
            "thumb": ((a.get("thumbnails") or {}).get("large") or {}).get("url")})
    return out


def upload(rec, key, filename, mimetype, data):
    if len(data) > UPLOAD_MAX:
        return f"{filename} is over 5 MB. A smaller version, please."
    safe = re.sub(r"[^\w.\- ]+", "_", filename or "file")[:120]
    r = requests.post(f"https://content.airtable.com/v0/{at.BASE}/{rec['id']}/Files/uploadAttachment",
                      headers=at._h(), timeout=120,
                      json={"contentType": mimetype or "application/octet-stream",
                            "filename": f"{key.replace('/', '~')}__{safe}",
                            "file": base64.b64encode(data).decode()})
    at._forget(at.TABLES["onboard"])
    if not r.ok:
        return f"{filename} didn't upload ({r.status_code}). Try again?"
    return None


def remove_file(rec, file_id):
    keep = [{"id": a["id"]} for a in rec["fields"].get("Files") or [] if a.get("id") != file_id]
    at.update("onboard", rec["id"], {"Files": keep})


# ---------- checking + finishing ----------
def _shown(q, ans):
    w = q.get("when")
    return not w or (ans.get(w[0]) or "") == w[1]


def _covered(order, answers, it):
    """A placement can borrow another one's answers: a later Marquee 'same as the previous one', or Shout-Outs
    'use this one for all'."""
    items = answers.get("items") or {}
    if it["kind"] == "marquee" and it["n"] > 0 and (items.get(it["key"]) or {}).get("same_as_prev"):
        return True
    if it["kind"] == "shoutout" and it["n"] > 0 and (items.get(f"shoutout-0") or {}).get("use_for_all"):
        return True
    return False


def missing(rec):
    order, answers, files = order_of(rec), answers_of(rec), files_of(fresh(rec["id"]))
    out = []
    for q in CONTACT:
        if q.get("req") and not (answers.get("contact") or {}).get(q["key"], "").strip():
            out.append({"item": "contact", "q": q["key"], "label": q["label"]})
    for it in order.get("items", []):
        if _covered(order, answers, it):
            continue
        ans = (answers.get("items") or {}).get(it["key"]) or {}
        for q in QUESTIONS[it["kind"]]:
            if not q.get("req") or not _shown(q, ans):
                continue
            ok = bool(files.get(f"{it['key']}/{q['key']}")) if q["type"] == "file" else bool((ans.get(q["key"]) or "").strip())
            if not ok:
                out.append({"item": it["key"], "q": q["key"], "label": f"{item_title(it)}: {q['label']}"})
    return out


def item_title(it):
    t = LABEL[it["kind"]]
    if it.get("of", 1) > 1:
        t += f" {it['n'] + 1} of {it['of']}"
    try:
        t += " · " + dt.date.fromisoformat(it["date"]).strftime("%a %-d %b")
    except (ValueError, KeyError):
        pass
    return t


def readable(rec):
    order, answers, files = order_of(rec), answers_of(rec), files_of(rec)
    c = answers.get("contact") or {}
    lines = [f"Contact: {c.get('name', '')} <{c.get('email', '')}>"]
    if c.get("proofs"):
        lines.append(f"Proofs to: {c['proofs']}")
    for it in order.get("items", []):
        lines += ["", f"== {item_title(it)} =="]
        if _covered(order, answers, it):
            lines.append("Same as the previous one." if it["kind"] == "marquee" else "Same as Shout-Out 1.")
            continue
        ans = (answers.get("items") or {}).get(it["key"]) or {}
        for q in QUESTIONS[it["kind"]]:
            if not _shown(q, ans):
                continue
            if q["type"] == "file":
                fs = files.get(f"{it['key']}/{q['key']}") or []
                val = ", ".join(f["filename"] for f in fs) or "-"
            else:
                val = (ans.get(q["key"]) or "-").strip()
            lines.append(f"{q['label']}: {val}")
    return "\n".join(lines)


def complete(rec, sponsor):
    at.update("onboard", rec["id"], {"Status": "Complete", "Answers Readable": readable(fresh(rec["id"])),
                                     "Completed At": dt.datetime.now(dt.timezone.utc).isoformat()})
    try:
        slack_complete(fresh(rec["id"]), sponsor)
    except Exception:
        pass


def slack_complete(rec, sponsor):
    tok = os.environ.get("SLACK_BOT_TOKEN")
    if not tok:
        return
    esc = lambda v: str(v or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    order, c = order_of(rec), answers_of(rec).get("contact") or {}
    email = (c.get("email") or rec["fields"].get("Contact Email") or "").strip().lower()
    test = email.endswith("@zee.media")
    dates = "; ".join(item_title(it) for it in order.get("items", []))
    head = (f"\U0001f9ea Test onboarding from the team: *{esc(sponsor)}*" if test
            else f"✅ *{esc(sponsor)}* finished their onboarding <@{KARLIE_SLACK_ID}>")
    text = "\n".join([
        head,
        f"*{esc(summary(order))}*",
        esc(dates),
        f"From {esc(c.get('name'))} · {esc(email)}",
        f"Everything they sent, files included: {PUBLIC_BASE}/onboarding/{rec['id']}",
    ])
    requests.post("https://slack.com/api/chat.postMessage", headers={"Authorization": f"Bearer {tok}"}, timeout=15,
                  json={"channel": W6_TEAM_CHANNEL, "text": text, "unfurl_links": False})


def all_links():
    return at.list_records("onboard", sort=[("Name", "asc")])
