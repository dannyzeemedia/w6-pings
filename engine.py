"""The outbound engine's hands: inbox sync, sending, prospect selection, context for the brain, and applying its decisions.

No AI in here. The thinking (classifying replies, writing drafts, the AI-tell check, learning) is done by a Claude routine
that calls /api/engine/context, thinks, then posts its decisions to /api/engine/apply.
"""
import base64, datetime as dt, html, json, random, re
from functools import cached_property
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from zoneinfo import ZoneInfo
import os
import requests
import airtable as at
import apollo
import gmail

ME = gmail.ME
JESSE = "usrhUhvPEfKVofPL8"  # Karlie's Airtable login (Jesse Zee)
LIVE_DEAL = {"Discussion", "To Be Invoiced", "Invoiced", "Overdue", "WG: Feedback Plz"}
WON = {"Paid", "Invoiced", "To Be Invoiced", "Overdue"}
FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "me.com", "aol.com", "live.com", "googlemail.com"}
INTERNAL = ("workspace6.io", "zee.media")
BOUNCE_FROM = re.compile(r"mailer-daemon|postmaster|mail delivery", re.I)
AUTO_SUBJ = re.compile(r"out of (the )?office|automatic reply|auto.?reply|autoreply|away from|on leave|vacation|abwesend|^auto:", re.I)
LEFT = re.compile(r"no longer (with|at|work|employed|part of)|(has|have) left (the company|[A-Z])|last day (at|with)|is no longer|not with .{0,30} anymore|this (mailbox|inbox) is (no longer|not) (monitored|active)", re.I)
UNSUB = re.compile(r"^\s*(unsubscribe|remove me|stop|take me off)", re.I)
DEFAULT_BEST = (9, 11)  # local hours that worked best across her history, used until we know a person's habits


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def pts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def domain_of(addr):
    a = parseaddr(addr)[1].lower()
    return a.split("@")[1] if "@" in a else ""


def site_domain(url):
    m = re.search(r"(?:https?://)?(?:www\.)?([^/\s]+)", url or "")
    return m.group(1).lower() if m else ""


# ---------------------------------------------------------------- lookups
class World:
    """One snapshot of the tables the engine reasons over. Tables load lazily, so an idle tick stays fast."""

    def __init__(self):
        self.settings_rec = at.settings()
        self.s = self.settings_rec["fields"]

    @cached_property
    def partners(self):
        return {r["id"]: r for r in at.list_records("partners", fields=[
            "Name", "Website", "Contact", "Stage", "Type", "Notes", "Sales", "Competitors"])}

    @cached_property
    def sales(self):
        return {r["id"]: r for r in at.list_records("sales", fields=[
            "Opportunity", "Status", "Value", "Partner", "Created", "Modified", "Ping", "Ping Modified", "Via"])}

    @cached_property
    def contacts(self):
        return {r["id"]: r for r in at.list_records("contacts")}

    @cached_property
    def by_email(self):
        return {r["fields"].get("Email", "").lower(): r for r in self.contacts.values() if r["fields"].get("Email")}

    @cached_property
    def handsoff(self):
        today = dt.date.today().isoformat()
        return [h for h in at.list_records("handsoff", "{Active}")
                if not h["fields"].get("Until") or h["fields"]["Until"] >= today]

    @cached_property
    def dom2partner(self):
        out = {}
        for pid, p in self.partners.items():
            d = site_domain(p["fields"].get("Website"))
            if d:
                out.setdefault(d, pid)
        for c in self.contacts.values():
            e = c["fields"].get("Email", "").lower()
            d = e.split("@")[1] if "@" in e else ""
            if d and d not in FREE_MAIL and c["fields"].get("Partner"):
                out.setdefault(d, c["fields"]["Partner"][0])
        return out

    def partner_for(self, addr):
        c = self.by_email.get(parseaddr(addr)[1].lower())
        if c and c["fields"].get("Partner"):
            return c["fields"]["Partner"][0]
        return self.dom2partner.get(domain_of(addr))

    def partner_domains(self, pid):
        out = {d for d, p in self.dom2partner.items() if p == pid}
        d = site_domain(self.partners.get(pid, {}).get("fields", {}).get("Website"))
        if d:
            out.add(d)
        return sorted(out - FREE_MAIL)

    def partner_sales(self, pid):
        return [self.sales[s] for s in self.partners.get(pid, {}).get("fields", {}).get("Sales", []) if s in self.sales]

    def blocked(self, pid):
        """Why the system must not email this company right now, or None."""
        if not pid:
            return None
        names = {self.partners[pid]["fields"].get("Name", "").strip().lower()} if pid in self.partners else set()
        for h in self.handsoff:
            f = h["fields"]
            if pid in f.get("Partner", []) or f.get("Company", "").strip().lower() in names:
                return f"Hands off: {f.get('Reason', 'Karlie is handling them')}"
        for s in self.partner_sales(pid):
            if s["fields"].get("Status") in LIVE_DEAL:
                return f"Live deal in Sales ({s['fields']['Status']})"
        return None

    def tz_for(self, contact):
        z = (contact or {}).get("fields", {}).get("Time Zone") or self.s.get("Send Timezone") or "America/New_York"
        try:
            return ZoneInfo(z)
        except Exception:
            return ZoneInfo("America/New_York")


# ---------------------------------------------------------------- log helpers
def log(event, direction, by, email="", partner=None, sale=None, contact=None, draft=None, summary="", snippet="",
        msg_id="", thread_id="", at_time=None, handled=None, reviewed=None):
    f = {"Event": event, "Direction": direction, "By": by, "Email": email or None, "Summary": summary[:250],
         "Snippet": snippet[:1500], "Gmail Message ID": msg_id, "Gmail Thread ID": thread_id,
         "At": iso(at_time or now())}
    for k, v in (("Partner", partner), ("Sale", sale), ("Contact", contact), ("Draft", draft)):
        if v:
            f[k] = [v] if isinstance(v, str) else v
    if handled is not None:
        f["Handled"] = handled
    if reviewed is not None:
        f["Reviewed"] = reviewed
    return at.create("log", {k: v for k, v in f.items() if v not in (None, "")})


def known_message_ids():
    """Gmail ids of messages already in the ping log (so we never log the same email twice)."""
    since = (now() - dt.timedelta(days=45)).strftime("%Y-%m-%d")
    return {r["fields"].get("Gmail Message ID") for r in
            at.list_records("log", f"IS_AFTER({{At}}, '{since}')", fields=["Gmail Message ID"])}


# ---------------------------------------------------------------- inbox sync
def sync_inbox(w):
    """Read everything new in Karlie's mailbox since last time and turn it into ping-log events."""
    hist = w.s.get("Gmail History ID")
    if not hist:
        at.update("settings", w.settings_rec["id"], {"Gmail History ID": gmail.profile()["historyId"]})
        return {"started": True}
    new_ids, latest = gmail.history_since(hist)
    seen = known_message_ids()
    out = {"bounced": 0, "auto": 0, "replied": 0, "karlie": 0, "left": 0, "ignored": 0}
    for mid in new_ids:
        if mid in seen:
            continue
        try:
            m = gmail.message(mid)
        except Exception:
            continue
        if "SPAM" in m["labels"] or "DRAFT" in m["labels"]:
            continue
        kind = _handle_message(w, m)
        out[kind] = out.get(kind, 0) + 1
    at.update("settings", w.settings_rec["id"], {"Gmail History ID": latest})
    return out


def _thread_is_ours(tid):
    return bool(at.list_records("drafts", f"{{Gmail Thread ID}}='{tid}'", fields=["Status"], max_records=1))


def _handle_message(w, m):
    frm = m["from"]
    if m["is_me"]:
        # Karlie wrote this herself (not through the system): count it, and it tells us she's active with them.
        if not any(w.partner_for(a) for _, a in getaddresses([m["to"], m["cc"]])):
            return "ignored"
        to = getaddresses([m["to"]])[0][1] if m["to"] else ""
        pid = w.partner_for(to)
        c = w.by_email.get(to.lower())
        log("Sent", "Out", "Karlie", to, partner=pid, contact=c and c["id"], summary="Karlie emailed them directly",
            snippet=m["body"], msg_id=m["id"], thread_id=m["thread_id"], at_time=m["dt"], handled=True, reviewed=False)
        return "karlie"

    if BOUNCE_FROM.search(frm):
        failed = m["headers"].get("x-failed-recipients", "").lower().split(",")
        failed = [f.strip() for f in failed if f.strip()] or [e for e in w.by_email if e and e in m["body"].lower()]
        for e in failed[:3]:
            c = w.by_email.get(e)
            pid = c and (c["fields"].get("Partner") or [None])[0] or w.partner_for(e)
            if c:
                at.update("contacts", c["id"], {"Status": "Bounced", "Dead Reason": m["body"][:500]})
            log("Bounced", "In", "Them", e, partner=pid, contact=c and c["id"], summary="Email bounced. Address marked dead.",
                snippet=m["body"][:600], msg_id=m["id"], thread_id=m["thread_id"], at_time=m["dt"], handled=True, reviewed=False)
            _cancel_pending(w, pid, e, "The address bounced.")
        return "bounced"

    sender = parseaddr(frm)[1].lower()
    pid = w.partner_for(sender)
    ours = _thread_is_ours(m["thread_id"])
    if not pid and not ours:
        return "ignored"
    c = w.by_email.get(sender)
    auto = (m["headers"].get("auto-submitted", "no").lower() not in ("", "no") or "x-autoreply" in m["headers"]
            or m["headers"].get("precedence", "").lower() in ("auto_reply", "bulk", "junk") or AUTO_SUBJ.search(m["subject"]))
    if LEFT.search(m["body"][:1500]):
        target = c
        if not target:  # "Sam no longer works here" from someone else: the person we emailed is the one who left
            last = at.list_records("drafts", f"AND({{Gmail Thread ID}}='{m['thread_id']}', {{Status}}='Sent')",
                                   fields=["To Email"], max_records=1)
            if last:
                target = w.by_email.get((last[0]["fields"].get("To Email") or "").lower())
        if target and target["fields"].get("Status") == "Active":
            at.update("contacts", target["id"], {"Status": "Left Company", "Dead Reason": m["body"][:500]})
        log("Left Company", "In", "Them", (target or {}).get("fields", {}).get("Email", sender), partner=pid,
            contact=target and target["id"], summary="They've left the company. Address marked dead.",
            snippet=m["body"][:1500], msg_id=m["id"], thread_id=m["thread_id"], at_time=m["dt"], handled=True, reviewed=False)
        _cancel_pending(w, pid, sender, "The contact has left the company.")
        return "left"
    if auto:
        log("Auto-Reply", "In", "Them", sender, partner=pid, contact=c and c["id"], summary="Automatic reply",
            snippet=m["body"][:600], msg_id=m["id"], thread_id=m["thread_id"], at_time=m["dt"], handled=True, reviewed=True)
        return "auto"

    # A real person wrote back. Phase 1: it goes straight to Karlie, and every queued email to that company stops.
    if not c:
        c = at.create("contacts", {"Email": sender, "Name": frm.split("<")[0].strip(' "') or None, "Status": "Active",
                                   "Source": "Email History", **({"Partner": [pid]} if pid else {})})
    _record_reply_time(w, c, m["dt"])
    log("Replied", "In", "Them", sender, partner=pid, contact=c["id"], summary="Replied", snippet=m["body"][:1500],
        msg_id=m["id"], thread_id=m["thread_id"], at_time=m["dt"], handled=True, reviewed=False)
    if UNSUB.search(m["body"]):
        at.update("contacts", c["id"], {"Status": "Unsubscribed", "Dead Reason": m["body"][:300]})
        _cancel_pending(w, pid, sender, "They asked not to be emailed.")
        return "replied"
    log("Handed To Karlie", "In", "Them", sender, partner=pid, contact=c["id"],
        summary="New reply. Summary coming shortly.", snippet=m["body"][:1500], msg_id=m["id"] + ":handoff",
        thread_id=m["thread_id"], at_time=m["dt"], handled=False, reviewed=False)
    _cancel_pending(w, pid, sender, "They replied, so the plan changed. It's with Karlie now.")
    return "replied"


def _record_reply_time(w, contact, when):
    f = contact["fields"]
    tz = w.tz_for(contact)
    local = when.astimezone(tz)
    seen = [l for l in (f.get("Reply Times Seen") or "").splitlines() if l.strip()]
    seen.append(local.strftime("%a %H:%M"))
    seen = seen[-20:]
    mins = sorted(int(x.split()[1][:2]) * 60 + int(x.split()[1][3:]) for x in seen)
    best = None
    if len(mins) >= 2:
        lo, hi = mins[len(mins) // 5], mins[-1 - len(mins) // 5]  # trim outliers once there's enough data
        lo, hi = max(lo - 15, 0), min(hi + 15, 24 * 60 - 1)
        best = f"{lo // 60:02d}:{lo % 60:02d}-{hi // 60:02d}:{hi % 60:02d}"
    at.update("contacts", contact["id"], {"Reply Times Seen": "\n".join(seen), **({"Best Time To Reach": best} if best else {})})


def _cancel_pending(w, pid, email, why):
    conds = [f"{{To Email}}='{email}'"]
    if pid:
        conds.append(f"FIND('{pid}', ARRAYJOIN({{Partner}}))")
    for d in at.list_records("drafts", f"AND(OR({{Status}}='Pending Approval', {{Status}}='Approved'), OR({','.join(conds)}))",
                             fields=["Status", "Kind"]):
        if d["fields"].get("Kind") == "Reply":
            continue
        at.update("drafts", d["id"], {"Status": "Cancelled", "Replan Note": why, "Decided At": iso(now())})


# ---------------------------------------------------------------- sending
def _window(w, contact):
    """(start_min, end_min) local minutes when we're allowed to send to this person, narrowed to their habits."""
    s = w.s
    lo, hi = (s.get("Send Window Start (Hour)") or 8) * 60, (s.get("Send Window End (Hour)") or 17) * 60
    if s.get("Optimise Send Timing"):
        best = (contact or {}).get("fields", {}).get("Best Time To Reach")
        if best and re.match(r"\d\d:\d\d-\d\d:\d\d", best):
            a, b = best.split("-")
            bl, bh = int(a[:2]) * 60 + int(a[3:]), int(b[:2]) * 60 + int(b[3:])
            if max(lo, bl) < min(hi, bh):
                return max(lo, bl), min(hi, bh)
        bl, bh = DEFAULT_BEST[0] * 60, DEFAULT_BEST[1] * 60
        if max(lo, bl) < min(hi, bh):
            return max(lo, bl), min(hi, bh)
    return lo, hi


def sendable_now(w, contact):
    tz = w.tz_for(contact)
    t = now().astimezone(tz)
    if t.strftime("%a") not in (w.s.get("Send Days") or []):
        return False
    lo, hi = _window(w, contact)
    return lo <= t.hour * 60 + t.minute < hi


def sent_today(w):
    tz = ZoneInfo(w.s.get("Send Timezone") or "America/New_York")
    since = (now() - dt.timedelta(days=2)).strftime("%Y-%m-%d")
    rows = at.list_records("log", f"AND({{Event}}='Sent', {{By}}='System', IS_AFTER({{At}}, '{since}'))", fields=["At"])
    today = now().astimezone(tz).date()
    return sum(1 for r in rows if pts(r["fields"].get("At")) and pts(r["fields"]["At"]).astimezone(tz).date() == today)


def auto_ok(mode, kind):
    if mode == "Fully Auto":
        return True
    if mode == "Auto Cold, Approve Replies":
        return kind in ("Cold", "Follow-up", "Gone-Contact Rescue")
    return False


def daily_cap(s):
    """The goal is what Karlie aims for; she can overachieve up to twice that. Hard ceiling protects the inbox."""
    return min(60, 2 * (s.get("Daily Ping Limit") or 0))


def send_due(w, max_sends=1):
    """Send at most `max_sends` emails this tick (ticks run every ~10 min, which spaces sends out like a person)."""
    if w.s.get("Paused"):
        return {"paused": True}
    limit = daily_cap(w.s)
    done = sent_today(w)
    if done >= limit:
        return {"limit_reached": done}
    mode = w.s.get("Approval Mode") or "Approve Every Draft"
    cands = at.list_records("drafts", "OR({Status}='Approved', {Status}='Pending Approval')", sort=[("Scheduled For", "asc")])
    cands = [d for d in cands if d["fields"].get("Status") == "Approved" or auto_ok(mode, d["fields"].get("Kind"))]
    if not cands:
        return {"sent": [], "nothing_approved": True, "sent_today": done, "limit": limit}
    sent, skipped = [], []
    for d in cands:
        f = d["fields"]
        if f.get("Status") == "Pending Approval" and not auto_ok(mode, f.get("Kind")):
            continue
        if f.get("Scheduled For") and pts(f["Scheduled For"]) > now():
            continue
        if f.get("Remix Request"):
            continue  # being rewritten on Karlie's instruction
        pid = (f.get("Partner") or [None])[0]
        contact = w.contacts.get((f.get("Contact") or [None])[0]) or w.by_email.get((f.get("To Email") or "").lower())
        why = w.blocked(pid)
        if not why and contact and contact["fields"].get("Status") not in (None, "Active"):
            why = f"Contact is {contact['fields']['Status'].lower()}"
        if why:
            at.update("drafts", d["id"], {"Status": "Cancelled", "Replan Note": why, "Decided At": iso(now())})
            skipped.append(why)
            continue
        if not sendable_now(w, contact):
            continue
        try:
            mid, tid = _send(w, d, contact)
        except Exception as ex:
            at.update("drafts", d["id"], {"Status": "Failed", "Replan Note": f"Send failed: {ex}"[:500]})
            skipped.append(f"failed: {ex}")
            continue
        sent.append(f.get("To Email"))
        if len(sent) >= max_sends or done + len(sent) >= limit:
            break
    return {"sent": sent, "skipped": skipped, "sent_today": done + len(sent), "limit": limit}


def _send(w, d, contact):
    f = d["fields"]
    to = f["To Email"]
    tid = f.get("Gmail Thread ID")
    first = not tid
    mid, tid = gmail.send(to, f.get("Subject") or "", f.get("Body") or "", thread_id=tid, signature=first,
                          to_name=f.get("To Name"))
    t = now()
    at.update("drafts", d["id"], {"Status": "Sent", "Sent At": iso(t), "Gmail Thread ID": tid,
                                  **({} if f.get("Decided At") else {"Decided At": iso(t)})})
    pid = (f.get("Partner") or [None])[0]
    sale = _touch_sale(w, pid, (f.get("Sale") or [None])[0], t)
    log("Sent", "Out", "System", to, partner=pid, sale=sale, contact=contact and contact["id"], draft=d["id"],
        summary=f"{f.get('Kind', 'Email')}: {f.get('Subject', '')}", snippet=f.get("Body", ""), msg_id=mid,
        thread_id=tid, at_time=t, handled=True, reviewed=True)
    if contact:
        cf = contact["fields"]
        at.update("contacts", contact["id"], {"Last Pinged": iso(t), "Pings Sent": (cf.get("Pings Sent") or 0) + 1})
    if pid and w.partners.get(pid, {}).get("fields", {}).get("Stage") in (None, "New"):
        at.update("partners", pid, {"Stage": "Contacted"})
    return mid, tid


def _touch_sale(w, pid, sale_id, t):
    """Keep Karlie's Sales table in step: bump Ping on the open outreach row, or open one."""
    if not pid:
        return None
    if not sale_id:
        open_rows = [s for s in w.partner_sales(pid) if s["fields"].get("Status") in (None, "Outreach", "Went Cold", "Maybe Later")]
        open_rows.sort(key=lambda s: s["fields"].get("Created") or "", reverse=True)
        sale_id = open_rows[0]["id"] if open_rows else None
    if sale_id:
        cur = w.sales.get(sale_id, {}).get("fields", {})
        n = min(int(cur.get("Ping") or 0) + 1, 4)
        at.update("sales", sale_id, {"Ping": str(n), "Ping Modified": iso(t), "Via": "Email", "Status": "Outreach"})
        return sale_id
    name = w.partners[pid]["fields"].get("Name", "") if pid in w.partners else ""
    r = at.create("sales", {"Opportunity": f"{name} // Pings", "Partner": [pid], "Status": "Outreach", "Ping": "1",
                            "Ping Modified": iso(t), "Via": "Email", "Assignee": {"id": JESSE}})
    return r["id"]


def next_send_time(w, contact, not_before=None):
    """Earliest moment the sender would actually send to this person: their local window, allowed days,
    the daily limit, and emails already approved ahead of it. Returns a UTC datetime (or None if no window)."""
    tz = w.tz_for(contact)
    lo, hi = _window(w, contact)
    days = w.s.get("Send Days") or []
    limit = daily_cap(w.s)
    if not days or not limit or hi <= lo:
        return None
    ahead = len([d for d in at.list_records("drafts", "AND({Status}='Approved', {Remix Request}='')", fields=["Subject"])])
    t = max(now(), not_before or now()) + dt.timedelta(minutes=10)  # next tick
    done_today = sent_today(w)
    for _ in range(24 * 6 * 14):  # walk forward in 10-minute ticks, up to two weeks
        local = t.astimezone(tz)
        mins = local.hour * 60 + local.minute
        if local.strftime("%a") in days and lo <= mins < hi:
            if done_today < limit:
                if ahead <= 0:
                    return t
                ahead -= 1
                done_today += 1
        nxt = t + dt.timedelta(minutes=10)
        if nxt.astimezone(tz).date() != local.date():
            done_today = 0
        t = nxt
    return None


# ---------------------------------------------------------------- on-the-fly rewrites
def remix_queue():
    """Drafts Karlie asked to rewrite, with what the rewriter needs."""
    w = World()
    out = []
    for d in at.list_records("drafts", "AND({Remix Request}!='', OR({Status}='Pending Approval', {Status}='Approved'))"):
        f = d["fields"]
        out.append({"draft_id": d["id"], "instruction": f["Remix Request"], "kind": f.get("Kind"), "to_name": f.get("To Name"),
                    "subject": f.get("Subject"), "body": f.get("Body"), "brief": f.get("Brief"), "why": f.get("Why This Email"),
                    "thread_id": f.get("Gmail Thread ID")})
    if not out:
        return {"items": [], "more_requested": int(w.s.get("More Drafts Requested") or 0)}
    lessons = [l["fields"].get("Lesson") for l in at.list_records("lessons", "{Active}", fields=["Lesson"])]
    return {"items": out, "voice": w.s.get("Karlie's Voice"), "lessons": lessons,
            "more_requested": int(w.s.get("More Drafts Requested") or 0)}


def remix_apply(item):
    d = at.get("drafts", item["draft_id"])["fields"]
    hist = (d.get("Remix History") or "")
    hist = (f"[{dt.date.today().isoformat()}] Asked: {d.get('Remix Request', '')}\nBefore:\n{d.get('Body', '')}\n\n" + hist)[:20000]
    f = {"Body": item["body"], "Remix Request": "", "Remix History": hist, "Edited By Karlie": True,
         "AI-Tell Check": item.get("ai_tell_check", "")[:3000]}
    if item.get("subject") is not None and d.get("Kind") != "Follow-up":
        f["Subject"] = item["subject"]
    at.update("drafts", item["draft_id"], f)
    return {"ok": True}


# ---------------------------------------------------------------- choosing who to ping
def _last_touch(w, pid):
    """Most recent email in or out with anyone at the company, from the log and her mailbox."""
    latest = None
    for d in w.partner_domains(pid)[:3]:
        try:
            ids = gmail.search(f"from:{d} OR to:{d}", 1)
        except Exception:
            ids = []
        if ids:
            m = gmail.message(ids[0], meta_only=True)
            latest = max(latest or m["dt"], m["dt"])
    return latest


def pick_prospects(w, n):
    """Rank companies to pitch cold. Deterministic; the brain can still skip any it doesn't like."""
    if n <= 0:
        return []
    repitch = dt.timedelta(days=w.s.get("Days Before Re-pitching A Company") or 45)
    queued = {p for d in at.list_records("drafts", "OR({Status}='Pending Approval', {Status}='Approved')", fields=["Partner"])
              for p in d["fields"].get("Partner", [])}
    recent = {}
    since = (now() - repitch).strftime("%Y-%m-%d")
    for r in at.list_records("log", f"AND(OR({{Event}}='Sent', {{Event}}='Replied'), IS_AFTER({{At}}, '{since}'))", fields=["Partner"]):
        for p in r["fields"].get("Partner", []):
            recent[p] = True
    scored = []
    for d in at.list_records("drafts", f"IS_AFTER(CREATED_TIME(), '{since}')", fields=["Partner"]):
        for p in d["fields"].get("Partner", []):
            recent[p] = True  # drafted (or skipped by the brain) recently
    for pid, p in w.partners.items():
        if pid in queued or pid in recent or w.blocked(pid):
            continue
        people = [c for c in w.contacts.values() if pid in c["fields"].get("Partner", []) and c["fields"].get("Status") == "Active"]
        if not people:
            continue
        f = p["fields"]
        sales = w.partner_sales(pid)
        st = [s["fields"].get("Status") for s in sales]
        score = 0.0
        score += 3 if any(x in WON for x in st) else 0          # past sponsors reply twice as often
        score += 1 if any(x in ("Maybe Later", "Went Cold") for x in st) else 0
        score -= 2 if "Declined" in st and len(st) == 1 else 0
        score += 1 if f.get("Type") == "Software" else 0
        score += 0.5 if f.get("Stage") == "Engaged" else 0
        score += random.random() * 0.8                          # keep the mix fresh
        scored.append((score, pid, people))
    scored.sort(reverse=True)
    out = []
    for score, pid, people in scored:
        last = _last_touch(w, pid)
        if last and now() - last < repitch:
            continue
        people.sort(key=lambda c: (c["fields"].get("Source") != "Partners Table", c["fields"].get("Pings Sent") or 0))
        out.append({"partner": pid, "contact": people[0]["id"], "score": round(score, 2),
                    "last_touch": last and iso(last)})
        if len(out) >= n:
            break
    return out


def due_followups(w):
    s = w.s
    gaps = [s.get("Follow-up 1 After (Days)"), s.get("Follow-up 2 After (Days)"), s.get("Follow-up 3 After (Days)")]
    maxn = s.get("Max Follow-ups") or 0
    out = []
    sent = at.list_records("drafts", "AND({Status}='Sent', {Kind}!='Reply', IS_AFTER({Sent At}, DATEADD(TODAY(), -60, 'days')))",
                           sort=[("Sent At", "desc")])
    by_thread = {}
    for d in sent:
        by_thread.setdefault(d["fields"].get("Gmail Thread ID"), []).append(d)
    queued_threads = {d["fields"].get("Gmail Thread ID") for d in
                      at.list_records("drafts", "OR({Status}='Pending Approval', {Status}='Approved')", fields=["Gmail Thread ID"])}
    for tid, ds in by_thread.items():
        if not tid or tid in queued_threads:
            continue
        ds.sort(key=lambda d: d["fields"]["Sent At"])
        n_prior_followups = sum(1 for d in ds if d["fields"].get("Kind") == "Follow-up")
        if n_prior_followups >= maxn:
            continue
        gap = gaps[n_prior_followups] if n_prior_followups < 3 else None
        if not gap:
            continue
        last = ds[-1]["fields"]
        if now() - pts(last["Sent At"]) < dt.timedelta(days=gap):
            continue
        if at.list_records("log", f"AND({{Gmail Thread ID}}='{tid}', OR({{Event}}='Replied', {{Event}}='Left Company', {{Event}}='Bounced'))",
                           fields=["Event"], max_records=1):
            continue
        pid = (last.get("Partner") or [None])[0]
        if w.blocked(pid):
            continue
        c = w.contacts.get((last.get("Contact") or [None])[0]) or w.by_email.get((last.get("To Email") or "").lower())
        if c and c["fields"].get("Status") != "Active":
            continue
        out.append({"thread": tid, "partner": pid, "contact": c and c["id"], "followup_number": n_prior_followups + 1,
                    "previous_drafts": [d["id"] for d in ds]})
    return out


def rescue_targets(w):
    if not w.s.get("Gone-Contact Rescue"):
        return []
    tries = w.s.get("Rescue Tries Per Company") or 0
    out, seen = [], set()
    for c in w.contacts.values():
        f = c["fields"]
        if f.get("Status") not in ("Bounced", "Left Company") or not f.get("Partner"):
            continue
        pid = f["Partner"][0]
        if pid in seen or w.blocked(pid):
            continue
        seen.add(pid)
        used = sum((x["fields"].get("Rescue Tries") or 0) for x in w.contacts.values() if pid in x["fields"].get("Partner", []))
        if used >= tries:
            continue
        # only rescue companies someone actually tried recently (don't mass-email every old bounce)
        if not f.get("Last Pinged") or now() - pts(f["Last Pinged"]) > dt.timedelta(days=30):
            continue
        out.append({"partner": pid, "gone_contact": c["id"], "tries_used": used})
    return out


# ---------------------------------------------------------------- context for the brain
def company_history(w, pid, max_threads=8):
    """Everything Karlie would know about a company: her email threads with anyone there + the Airtable rows."""
    p = w.partners.get(pid, {}).get("fields", {})
    threads, seen = [], set()
    for d in w.partner_domains(pid)[:3]:
        try:
            ids = gmail.search(f"from:{d} OR to:{d}", 40)
        except Exception:
            ids = []
        for mid in ids:
            m = gmail.message(mid, meta_only=True)
            if m["thread_id"] in seen:
                continue
            seen.add(m["thread_id"])
            if len(seen) > max_threads:
                break
            msgs = gmail.thread(m["thread_id"])
            threads.append({"thread_id": m["thread_id"], "subject": msgs[0]["subject"], "messages": [
                {"from": "Karlie" if x["is_me"] else x["from"], "date": dt.datetime.fromtimestamp(x["ts"] / 1000, dt.timezone.utc).strftime("%Y-%m-%d"),
                 "text": x["body"][:1800]} for x in msgs[-6:]], "earlier_messages": max(0, len(msgs) - 6)})
    sales = [{"opportunity": s["fields"].get("Opportunity"), "status": s["fields"].get("Status"), "value": s["fields"].get("Value"),
              "created": s["fields"].get("Created"), "pings": s["fields"].get("Ping")} for s in w.partner_sales(pid)]
    pings = [{"at": r["fields"].get("At"), "event": r["fields"].get("Event"), "by": r["fields"].get("By"), "summary": r["fields"].get("Summary")}
             for r in at.list_records("log", f"FIND('{pid}', ARRAYJOIN({{Partner}}))", sort=[("At", "desc")], max_records=15)]
    people = [{"id": c["id"], "email": c["fields"].get("Email"), "name": c["fields"].get("Name"), "title": c["fields"].get("Title"),
               "status": c["fields"].get("Status"), "time_zone": c["fields"].get("Time Zone")}
              for c in w.contacts.values() if pid in c["fields"].get("Partner", [])]
    return {"partner_id": pid, "name": p.get("Name"), "website": p.get("Website"), "type": p.get("Type"), "stage": p.get("Stage"),
            "notes": (p.get("Notes") or "")[:2000], "sales": sales, "people": people, "ping_log": pings, "email_threads": threads}


def context(max_new=None, more=0):
    """Everything the brain needs for one run, as JSON."""
    w = World()
    s = w.s
    lessons = [{"id": l["id"], "lesson": l["fields"].get("Lesson"), "applies_to": l["fields"].get("Applies To"),
                "about": l["fields"].get("About"), "source": l["fields"].get("Source")}
               for l in at.list_records("lessons", "{Active}")]
    # replies and events waiting to be read
    events = []
    for r in at.list_records("log", "AND(NOT({Reviewed}), OR({Event}='Replied', {Event}='Left Company', {Event}='Bounced', AND({Event}='Sent', {By}='Karlie')))",
                             sort=[("At", "asc")], max_records=25):
        f = r["fields"]
        tid = f.get("Gmail Thread ID")
        thread = []
        if tid:
            try:
                thread = [{"from": "Karlie" if x["is_me"] else x["from"], "text": x["body"][:2000]} for x in gmail.thread(tid)[-6:]]
            except Exception:
                pass
        handoff = at.list_records("log", f"AND({{Gmail Thread ID}}='{tid}', {{Event}}='Handed To Karlie', NOT({{Handled}}))", fields=["Summary"], max_records=1) if tid else []
        events.append({"log_id": r["id"], "event": f.get("Event"), "by": f.get("By"), "email": f.get("Email"),
                       "partner_id": (f.get("Partner") or [None])[0], "contact_id": (f.get("Contact") or [None])[0],
                       "thread_id": tid, "at": f.get("At"), "thread": thread, "handoff_log_id": handoff[0]["id"] if handoff else None})
    # Karlie's decisions to learn from
    decided = []
    for d in at.list_records("drafts", "AND(NOT({Learned From}), OR({Status}='Sent', {Status}='Rejected', {Status}='Approved'), OR({Edited By Karlie}, {Karlie Feedback}!='', {Status}='Rejected', {Written By Karlie}))",
                             max_records=30):
        f = d["fields"]
        decided.append({"draft_id": d["id"], "kind": f.get("Kind"), "status": f.get("Status"), "written_by_karlie": bool(f.get("Written By Karlie")),
                        "ai_subject": f.get("AI Original Subject"), "ai_body": f.get("AI Original Body"),
                        "final_subject": f.get("Subject"), "final_body": f.get("Body"), "her_note": f.get("Karlie Feedback")})
    # capacity
    queued = len(at.list_records("drafts", "AND(OR({Status}='Pending Approval', {Status}='Approved'), OR({Scheduled For}='', IS_BEFORE({Scheduled For}, DATEADD(NOW(), 2, 'days'))))", fields=["Status"]))
    goal = s.get("Daily Ping Limit") or 0
    room = max(0, 2 * goal - queued)  # keep twice the day's goal waiting, so she can overachieve
    if more:  # "Create N more" button: write N regardless of what's already waiting
        room = more
        at.update("settings", w.settings_rec["id"], {"More Drafts Requested": 0, "More Drafts Started": iso(now())})
    followups = due_followups(w)[:room]
    room -= len(followups)
    rescues = rescue_targets(w)[:max(0, min(room, 3))]
    room -= len(rescues)
    cold = pick_prospects(w, room if max_new is None else min(room, max_new))
    work = []
    for x in followups:
        prev = [at.get("drafts", i)["fields"] for i in x["previous_drafts"]]
        work.append({"kind": "Follow-up", **x, "company": company_history(w, x["partner"]) if x["partner"] else None,
                     "previous_emails": [{"subject": p.get("Subject"), "body": p.get("Body"), "sent_at": p.get("Sent At")} for p in prev]})
    for x in rescues:
        known = {c["fields"].get("Email", "").lower() for c in w.contacts.values()}
        others = [{"id": c["id"], "email": c["fields"].get("Email"), "name": c["fields"].get("Name"), "title": c["fields"].get("Title")}
                  for c in w.contacts.values() if x["partner"] in c["fields"].get("Partner", []) and c["fields"].get("Status") == "Active"]
        found = []
        if not others and os.environ.get("APOLLO_API_KEY"):
            for d in w.partner_domains(x["partner"])[:1]:
                try:
                    found = apollo.people(d, exclude=known)
                except Exception:
                    found = []
        gone = w.contacts[x["gone_contact"]]["fields"]
        work.append({"kind": "Gone-Contact Rescue", **x, "gone_name": gone.get("Name"), "gone_email": gone.get("Email"),
                     "other_known_people": others, "apollo_candidates": found, "company": company_history(w, x["partner"])})
    for x in cold:
        work.append({"kind": "Cold", **x, "company": company_history(w, x["partner"])})
    weekly = not s.get("Voice Updated At") or now() - pts(s["Voice Updated At"]) > dt.timedelta(days=7)
    return {"now": iso(now()), "settings": {k: v for k, v in s.items() if k not in ("Karlie's Voice", "Gmail History ID")},
            "voice": s.get("Karlie's Voice"), "lessons": lessons, "events_to_review": events, "decisions_to_learn": decided,
            "work": work, "voice_rewrite_due": weekly,
            "results": results_summary() if weekly else None,
            "pending_voice_notes": [{"id": l["id"], "note": l["fields"].get("Lesson")} for l in
                                    at.list_records("lessons", "AND({Active}, NOT({Folded Into Voice}))")]}


def results_summary(days=56):
    """Per-draft outcomes for the weekly what-works review."""
    since = (now() - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    for d in at.list_records("drafts", f"AND({{Status}}='Sent', {{Kind}}!='Reply', IS_AFTER({{Sent At}}, '{since}'))"):
        f = d["fields"]
        tid = f.get("Gmail Thread ID")
        ev = [r["fields"].get("Event") for r in at.list_records("log", f"{{Gmail Thread ID}}='{tid}'", fields=["Event"])] if tid else []
        out.append({"kind": f.get("Kind"), "subject": f.get("Subject"), "words": len((f.get("Body") or "").split()),
                    "sent_at": f.get("Sent At"), "edited_by_karlie": bool(f.get("Edited By Karlie")),
                    "replied": "Replied" in ev, "bounced": "Bounced" in ev})
    return out


# ---------------------------------------------------------------- applying the brain's decisions
def _txt(v, bullets=True):
    """The brain sometimes sends a list where we want text; accept both."""
    if isinstance(v, list):
        return "\n".join((("- " if bullets and not str(x).lstrip().startswith("-") else "") + str(x)) for x in v)
    return "" if v is None else str(v)


def apply(payload):
    w = World()
    t = iso(now())
    done = {"drafts": 0, "events": 0, "lessons": 0, "planned": 0, "contacts": 0}
    for e in payload.get("events", []):
        f = {"Reviewed": True}
        if e.get("summary"):
            f["Summary"] = e["summary"][:250]
        at.update("log", e["log_id"], f)
        if e.get("handoff_log_id") and e.get("summary"):
            at.update("log", e["handoff_log_id"], {"Summary": e["summary"][:250], "Reviewed": True})
        if e.get("contact_time_zone") and e.get("contact_id"):
            at.update("contacts", e["contact_id"], {"Time Zone": e["contact_time_zone"]})
        done["events"] += 1
    for c in payload.get("new_contacts", []):
        if c.get("email") and c["email"].lower() not in w.by_email:
            at.create("contacts", {"Email": c["email"].lower(), "Name": c.get("name"), "Title": c.get("title"), "Status": "Active",
                                   "Source": c.get("source") or "Referral", "Referred By": c.get("referred_by"),
                                   **({"Partner": [c["partner_id"]]} if c.get("partner_id") else {}),
                                   **({"Time Zone": c["time_zone"]} if c.get("time_zone") else {})})
            done["contacts"] += 1
    for d in payload.get("drafts", []):
        contact = w.contacts.get(d.get("contact_id")) or w.by_email.get((d.get("to_email") or "").lower())
        if not contact and d.get("to_email"):
            contact = at.create("contacts", {"Email": d["to_email"].lower(), "Name": d.get("to_name"), "Status": "Active",
                                             "Source": d.get("contact_source") or "Apollo",
                                             **({"Partner": [d["partner_id"]]} if d.get("partner_id") else {})})
        if not contact:
            continue
        cf = contact["fields"]
        f = {"Subject": d.get("subject") or "", "AI Original Subject": d.get("subject") or "", "Body": _txt(d["body"], False),
             "AI Original Body": _txt(d["body"], False),
             "Status": "Pending Approval", "Kind": d["kind"], "To Email": cf["Email"], "To Name": d.get("to_name") or cf.get("Name"),
             "Why This Email": _txt(d.get("why"), False)[:3000], "AI-Tell Check": _txt(d.get("ai_tell_check"), False)[:3000],
             "Brief": _txt(d.get("brief"))[:6000], "Contact": [contact["id"]]}
        if d.get("partner_id"):
            f["Partner"] = [d["partner_id"]]
        if d.get("thread_id"):
            f["Gmail Thread ID"] = d["thread_id"]
        if d.get("scheduled_for"):
            f["Scheduled For"] = d["scheduled_for"]
            done["planned"] += 1
        if d.get("they_asked_for"):
            f["They Asked For"] = d["they_asked_for"][:1000]
        at.create("drafts", f)
        if d["kind"] == "Gone-Contact Rescue":
            at.update("contacts", contact["id"], {"Rescue Tries": (cf.get("Rescue Tries") or 0) + 1})
        done["drafts"] += 1
    for l in payload.get("lessons", []):
        at.create("lessons", {"Lesson": l["lesson"][:250], "Evidence": l.get("evidence", "")[:3000], "Active": True,
                              "Source": l.get("source") or "Karlie's Edit", "Applies To": l.get("applies_to") or "All Emails",
                              "About": l.get("about") or "Voice", "Learned At": t, "Added By": "system",
                              **({"Draft": [l["draft_id"]]} if l.get("draft_id") else {})})
        done["lessons"] += 1
    for sk in payload.get("skipped", []):
        if sk.get("partner_id"):
            at.create("drafts", {"Subject": "(skipped)", "Status": "Cancelled", "Kind": sk.get("kind") or "Cold",
                                 "Partner": [sk["partner_id"]], "Replan Note": ("Skipped by the brain: " + sk.get("why", ""))[:1000],
                                 "Decided At": t})
    for did in payload.get("learned_from", []):
        at.update("drafts", did, {"Learned From": True})
    for lid in payload.get("lessons_off", []):
        at.update("lessons", lid, {"Active": False})
    if payload.get("voice"):
        at.update("settings", w.settings_rec["id"], {"Karlie's Voice": payload["voice"], "Voice Updated At": t})
        for lid in payload.get("folded_notes", []):
            at.update("lessons", lid, {"Folded Into Voice": True})
    at.update("settings", w.settings_rec["id"], {"Last Brain Run": t})
    return done


def tick():
    w = World()
    out = {"inbox": sync_inbox(w)}
    w = World()  # fresh snapshot: the sync may have changed contacts or hands-off state
    out["send"] = send_due(w)
    at.update("settings", w.settings_rec["id"], {"Last Tick": iso(now())})
    return out
