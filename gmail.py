"""Read and reply to threads in Karlie's inbox, via the delegated service account. Only ever acts as karlie@workspace6.io."""
import base64, json, os, re, html
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request

ME = "karlie@workspace6.io"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
_creds = None


def _h():
    global _creds
    if _creds is None:
        _creds = service_account.Credentials.from_service_account_info(
            json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
            scopes=["https://www.googleapis.com/auth/gmail.modify"]).with_subject(ME)
    if not _creds.valid:
        _creds.refresh(Request())
    return {"Authorization": f"Bearer {_creds.token}"}


def _body(p):
    mt = p.get("mimeType", "")
    if mt == "text/plain" and p.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", "replace")
    for sub in p.get("parts", []) or []:
        t = _body(sub)
        if t:
            return t
    if mt == "text/html" and p.get("body", {}).get("data"):
        h = base64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", "replace")
        return html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<(br|/p|/div)[^>]*>", "\n", h, flags=re.I)))
    return ""


_QUOTE = re.compile(r"\n(On .{5,200}wrote:|-{2,} ?Original Message|From: .+\nSent: )", re.S)


_LIST = re.compile(r"^\s*(?:[-*•–]|\d+[.)]|[a-z][.)])\s")


def tidy(t):
    """Readable email text: undo plain-text hard wraps (~76 chars), one blank line between paragraphs, no stray spaces."""
    t = t.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ").replace("\u200b", "").replace("\u200c", "").replace("\ufeff", "")
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in t.split("\n")]
    out, last_raw = [], ""
    for l in lines:
        prev = out[-1] if out else ""
        # join when the previous RAW line looks hard-wrapped (55-80 chars) and this one continues the paragraph
        joinable = (prev and l and 55 <= len(last_raw) <= 80 and not last_raw.endswith(":")
                    and not _LIST.match(l) and not _LIST.match(last_raw) and not re.match(r"^(--|—)\s*$", l))
        if joinable:
            out[-1] = prev + " " + l
        else:
            out.append(l)
        last_raw = l
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip(t):
    m = _QUOTE.search(t)
    t = t[:m.start()] if m else t
    return tidy("\n".join(l for l in t.splitlines() if not l.startswith(">")))


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_ON_WROTE = re.compile(r"(?:^|\n)[ \t]*(On\s(?:(?!\n\s*\n).){5,300}?wrote:)[ \t]*\n", re.S)
_FWD = re.compile(r"(?:^|\n)-{3,}\s*Forwarded message\s*-{3,}\s*\n(.*?)\n\s*\n", re.S)


def _unquote(block):
    """One level of '>' quoting off: the quoted email as it was written."""
    out = []
    for l in block.split("\n"):
        if l.startswith(">"):
            out.append(l[1:][1:] if l[1:2] == " " else l[1:])
        elif not l.strip():
            out.append("")
        else:
            break  # anything after the quote (a signature, a later reply) is not part of it
    return "\n".join(out)


def _who_when(header):
    """('Cristina Ursu', 'cristina@funnelytics.io', datetime or None) from an 'On ... wrote:' or forward header."""
    h = re.sub(r"\s+", " ", re.sub(r"(?m)^[ >]+", "", header)).strip()
    em = _EMAIL.search(re.sub(r"\s+", "", h.split("(")[-1]) if "(" in h else h) or _EMAIL.search(h.replace(" ", ""))
    email = em.group(0).lower() if em else ""
    name, when = "", None
    m = re.match(r"On (.+?)\s*<", h)
    if m:
        lead = m.group(1)  # "Wed, Sep 23, 2026 at 4:33 AM, Cristina Ursu" or "Sun, Sep 20, 2026 10:34:32 AM Ryan Doyle"
        cuts = list(re.finditer(r"\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?|\b\d{4}\b", lead))
        at_ = cuts[-1].end() if cuts else 0
        date_txt, name = lead[:at_], lead[at_:].strip(" ,").strip('"')
    else:
        f = re.search(r"From:\s*\"?([^<\"\n]+?)\"?\s*<", h)
        name = f.group(1).strip() if f else ""
        d = re.search(r"Date:\s*(.+?)(?:Subject:|To:|Cc:|$)", h)
        date_txt = d.group(1) if d else ""
    try:
        from dateutil import parser as _dp
        when = _dp.parse(date_txt.replace(" at ", " "), fuzzy=True) if date_txt else None
    except Exception:
        when = None
    return name or email, email, when


def earlier_emails(raw):
    """Emails quoted or forwarded inside a message that Karlie never received herself (Sam copies her in
    halfway through a conversation, or forwards one). Oldest first. Each: from, email, when, body."""
    out = []
    text = raw.replace("\r\n", "\n")
    for _ in range(12):  # each round peels one quoted or forwarded email, going back in time
        q, f = _ON_WROTE.search(text), _FWD.search(text)
        if not q and not f:
            break
        if f and (not q or f.start() < q.start()):
            header, rest = f.group(1), text[f.end():]
        else:
            header, rest = q.group(1), _unquote(text[q.end():])
        name, email, when = _who_when(header)
        m = _QUOTE.search("\n" + rest)
        nxt = _FWD.search(rest)
        cut = min([x for x in (m.start() - 1 if m else None, nxt.start() if nxt else None) if x is not None and x >= 0], default=len(rest))
        body = tidy("\n".join(l for l in rest[:cut].splitlines() if not l.startswith(">")))
        if body.strip():
            out.append({"from": name, "email": email, "when": when, "body": body})
        text = rest
    return list(reversed(out))


_tcache = {}


_trefresh = set()


def thread(tid, max_age=90):
    """Messages oldest-first: from, is_me, ts, body (quoted history stripped), message-id header. Cached briefly."""
    import time as _time
    hit = _tcache.get(tid)
    if hit and _time.time() - hit[0] < max_age:
        return hit[1]
    if hit and _time.time() - hit[0] < 900 and tid not in _trefresh:  # answer now, refresh underneath
        import threading
        _trefresh.add(tid)
        def bg():
            try:
                _tcache[tid] = (_time.time(), _thread(tid))
            except Exception:
                pass
            finally:
                _trefresh.discard(tid)
        threading.Thread(target=bg, daemon=True).start()
        return hit[1]
    out = _thread(tid)
    _tcache[tid] = (_time.time(), out)
    return out


def _thread(tid):
    r = requests.get(f"{API}/threads/{tid}", headers=_h(), params={"format": "full"}, timeout=30)
    r.raise_for_status()
    out = []
    for m in r.json()["messages"]:
        hd = {x["name"].lower(): x["value"] for x in m["payload"].get("headers", [])}
        out.append({"from": hd.get("from", ""), "to": hd.get("to", ""), "cc": hd.get("cc", ""),
                    "subject": hd.get("subject", ""), "msgid": hd.get("message-id", ""),
                    "refs": hd.get("references", ""), "ts": int(m["internalDate"]),
                    "is_me": ME in hd.get("from", "").lower(), "body": _strip(_body(m["payload"]))})
    if out:  # the first email Karlie got may carry a conversation she wasn't on (copied in, or forwarded)
        try:
            out[0]["earlier"] = earlier_emails(_body(r.json()["messages"][0]["payload"]))
        except Exception:
            out[0]["earlier"] = []
    return out


def with_earlier(msgs, last=None):
    """The thread as a reader needs it: emails from before Karlie was copied in first (marked), then hers.
    last=N keeps the newest N real emails; the earlier ones only show when the first email is still in view."""
    shown = msgs[-last:] if last else msgs
    pre = [dict(e, extracted=True, is_me=False) for e in (msgs[0].get("earlier") or [])] if msgs and shown and shown[0] is msgs[0] else []
    return pre + list(shown)


def reply(tid, text, body_html=None):
    """Reply in-thread to whoever last wrote to Karlie. Returns (message id, recipient)."""
    msgs = thread(tid)
    last_in = next((m for m in reversed(msgs) if not m["is_me"]), msgs[-1])
    # reply all, the way Gmail does: everyone on the last email except Karlie. When a teammate copied her in
    # (Sam looping her into a sponsor's thread), the sponsor is who she's answering, so they go in To.
    everyone, seen = [], set()
    for name, addr in getaddresses([last_in["from"], last_in["to"], last_in["cc"]]):
        a_ = addr.lower()
        if a_ and a_ != ME and a_ not in seen:
            seen.add(a_)
            everyone.append(a_)
    if last_in["is_me"]:
        everyone = [a_.lower() for _, a_ in getaddresses([last_in["to"]]) if a_] or everyone
    ours = [a_ for a_ in everyone if a_.endswith("@workspace6.io")]
    theirs = [a_ for a_ in everyone if a_ not in ours]
    sender = parseaddr(last_in["from"])[1].lower()
    if sender.endswith("@workspace6.io") and theirs:
        to_list, cc_list = theirs, ours
    else:
        to_list, cc_list = [sender] if sender and sender != ME else everyone[:1], [a_ for a_ in everyone if a_ != sender]
    to = ", ".join(to_list)
    subj = last_in["subject"] or msgs[0]["subject"]
    msg = EmailMessage()
    msg["From"] = f"Karlie <{ME}>"
    msg["To"] = to
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    if last_in["msgid"]:
        msg["In-Reply-To"] = last_in["msgid"]
        msg["References"] = (last_in["refs"] + " " + last_in["msgid"]).strip()
    msg.set_content(text)
    import richtext
    msg.add_alternative(richtext.email_html(body_html) if (body_html or "").strip() else _to_html(text), subtype="html")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    _tcache.pop(tid, None)
    r = requests.post(f"{API}/messages/send", headers=_h(), json={"raw": raw, "threadId": tid}, timeout=30)
    r.raise_for_status()
    return r.json()["id"], to


def mark_done(tid):
    """Mark the thread read and archive it (take it out of the inbox). New replies from them bring it back as usual."""
    r = requests.post(f"{API}/threads/{tid}/modify", headers=_h(), json={"removeLabelIds": ["UNREAD", "INBOX"]}, timeout=30)
    return r.ok


def open_link(msgid):
    """Gmail web link that opens this exact message in Karlie's inbox."""
    q = requests.utils.quote(f"rfc822msgid:{msgid.strip('<>')}")
    return f"https://mail.google.com/mail/u/{ME}/#search/{q}"


# ---------------------------------------------------------------- engine helpers
import datetime as _dt

_sig = {"html": None}


def profile():
    r = requests.get(f"{API}/profile", headers=_h(), timeout=30)
    r.raise_for_status()
    return r.json()


def history_since(history_id):
    """Ids of messages added since `history_id`, and the new latest history id."""
    ids, page, latest = [], None, history_id
    while True:
        params = {"startHistoryId": history_id, "historyTypes": "messageAdded", "maxResults": 500}
        if page:
            params["pageToken"] = page
        r = requests.get(f"{API}/history", headers=_h(), params=params, timeout=30)
        if r.status_code == 404:  # history id too old: restart from now
            return [], profile()["historyId"]
        r.raise_for_status()
        j = r.json()
        latest = j.get("historyId", latest)
        for h in j.get("history", []):
            for m in h.get("messagesAdded", []):
                ids.append(m["message"]["id"])
        page = j.get("nextPageToken")
        if not page:
            return list(dict.fromkeys(ids)), latest


def search(q, n=20):
    r = requests.get(f"{API}/messages", headers=_h(), params={"q": q, "maxResults": n}, timeout=30)
    r.raise_for_status()
    return [m["id"] for m in r.json().get("messages", [])]


def message(mid, meta_only=False):
    params = {"format": "metadata" if meta_only else "full"}
    r = requests.get(f"{API}/messages/{mid}", headers=_h(), params=params, timeout=30)
    r.raise_for_status()
    m = r.json()
    hd = {x["name"].lower(): x["value"] for x in m["payload"].get("headers", [])}
    return {"id": m["id"], "thread_id": m["threadId"], "labels": m.get("labelIds", []), "headers": hd,
            "from": hd.get("from", ""), "to": hd.get("to", ""), "cc": hd.get("cc", ""), "subject": hd.get("subject", ""),
            "msgid": hd.get("message-id", ""), "is_me": ME in hd.get("from", "").lower(),
            "dt": _dt.datetime.fromtimestamp(int(m["internalDate"]) / 1000, _dt.timezone.utc),
            "body": "" if meta_only else _strip(_body(m["payload"])), "raw": "" if meta_only else _body(m["payload"])}


def signature_html():
    if _sig["html"] is None:
        r = requests.get(f"{API}/settings/sendAs/{ME}", headers=_h(), timeout=30)
        _sig["html"] = r.json().get("signature", "") if r.ok else ""
    return _sig["html"]


_URL = re.compile(r"(https?://[^\s<>()]+)")


def _to_html(text):
    out = []
    for line in text.split("\n"):
        esc = html.escape(line)
        esc = _URL.sub(lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', esc)
        out.append(f"<div>{esc}</div>" if line.strip() else "<div><br></div>")
    return '<div dir="ltr">' + "".join(out) + "</div>"


def send(to, subject, body, thread_id=None, signature=True, to_name=None, body_html=None):
    """Send as Karlie, looking exactly like Gmail: plain + HTML parts, her real signature on first emails.
    With thread_id it replies in that thread. Returns (message id, thread id)."""
    msg = EmailMessage()
    msg["From"] = f"Karlie <{ME}>"
    msg["To"] = f"{to_name} <{to}>" if to_name and "," not in to_name else to
    if thread_id:
        msgs = thread(thread_id)
        last = msgs[-1]
        base = msgs[0]["subject"]
        msg["Subject"] = base if base.lower().startswith("re:") else f"Re: {base}"
        if last["msgid"]:
            msg["In-Reply-To"] = last["msgid"]
            msg["References"] = (last["refs"] + " " + last["msgid"]).strip()
    else:
        msg["Subject"] = subject
    sig = signature_html() if signature else ""
    msg.set_content(body + ("\n\n-- \nKarlie Zee | Workspace6\n👉 Read this week's Workspace6 DTC News https://news.workspace6.io/" if sig else ""))
    import richtext
    rich = richtext.email_html(body_html) if (body_html or "").strip() else _to_html(body)
    msg.add_alternative(rich + (f'<br><div class="gmail_signature">{sig}</div>' if sig else ""), subtype="html")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
        _tcache.pop(thread_id, None)
    r = requests.post(f"{API}/messages/send", headers=_h(), json=payload, timeout=30)
    r.raise_for_status()
    j = r.json()
    return j["id"], j["threadId"]
