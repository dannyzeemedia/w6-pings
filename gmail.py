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


def _strip(t):
    m = _QUOTE.search(t)
    t = t[:m.start()] if m else t
    return "\n".join(l for l in t.splitlines() if not l.startswith(">")).strip()


def thread(tid):
    """Messages oldest-first: from, is_me, ts, body (quoted history stripped), message-id header."""
    r = requests.get(f"{API}/threads/{tid}", headers=_h(), params={"format": "full"}, timeout=30)
    r.raise_for_status()
    out = []
    for m in r.json()["messages"]:
        hd = {x["name"].lower(): x["value"] for x in m["payload"].get("headers", [])}
        out.append({"from": hd.get("from", ""), "to": hd.get("to", ""), "cc": hd.get("cc", ""),
                    "subject": hd.get("subject", ""), "msgid": hd.get("message-id", ""),
                    "refs": hd.get("references", ""), "ts": int(m["internalDate"]),
                    "is_me": ME in hd.get("from", "").lower(), "body": _strip(_body(m["payload"]))})
    return out


def reply(tid, text):
    """Reply in-thread to whoever last wrote to Karlie. Returns (message id, recipient)."""
    msgs = thread(tid)
    last_in = next((m for m in reversed(msgs) if not m["is_me"]), msgs[-1])
    to = parseaddr(last_in["from"])[1] if not last_in["is_me"] else getaddresses([last_in["to"]])[0][1]
    subj = last_in["subject"] or msgs[0]["subject"]
    msg = EmailMessage()
    msg["From"] = f"Karlie <{ME}>"
    msg["To"] = to
    msg["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    if last_in["msgid"]:
        msg["In-Reply-To"] = last_in["msgid"]
        msg["References"] = (last_in["refs"] + " " + last_in["msgid"]).strip()
    msg.set_content(text)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    r = requests.post(f"{API}/messages/send", headers=_h(), json={"raw": raw, "threadId": tid}, timeout=30)
    r.raise_for_status()
    return r.json()["id"], to


def open_link(msgid):
    """Gmail web link that opens this exact message in Karlie's inbox."""
    q = requests.utils.quote(f"rfc822msgid:{msgid.strip('<>')}")
    return f"https://mail.google.com/mail/u/{ME}/#search/{q}"
