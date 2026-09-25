"""Karlie's outbound dashboard: daily ping count, approvals, replies to handle, rules, voice, results.
Airtable (W6 Media base) is the database; this app is only a friendlier screen over it."""
import os, json, re, datetime as dt
import requests
from functools import wraps
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, redirect as _redirect, url_for, session, flash
from werkzeug.security import check_password_hash
import airtable as at
import gmail
import engine
import bookings as bk
import brand
from ops_log import OpsLog

app = Flask(__name__)


def redirect(location, code=303):
    return _redirect(location, code)

app.secret_key = os.environ["SECRET_KEY"]
app.permanent_session_lifetime = dt.timedelta(days=90)
USERS = json.loads(os.environ.get("DASH_USERS", "{}"))  # {"karlie": "<werkzeug hash>", ...}
BRIS = ZoneInfo("Australia/Brisbane")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ZONES = ["America/New_York", "America/Los_Angeles", "Europe/London", "Australia/Brisbane"]
ZONE_LABEL = {"America/New_York": "US East", "America/Los_Angeles": "US West",
              "Europe/London": "UK", "Australia/Brisbane": "Brisbane"}
MODES = {
    "Approve Every Draft": "I approve every email before it goes",
    "Auto Cold, Approve Replies": "Send new pitches on its own, ask me before replying to anyone",
    "Fully Auto": "Send everything on its own",
}
APPROVE_AHEAD_DAYS = 2  # scheduled pings show up for approval this many days before they're due


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("user"):
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


@app.template_filter("when")
def when(s):
    t = parse_ts(s) if isinstance(s, str) else s
    if not t:
        return ""
    t = t.astimezone(BRIS)
    today = dt.datetime.now(BRIS).date()
    if t.date() == today:
        return "today " + t.strftime("%-I:%M%p").lower()
    if t.date() == today - dt.timedelta(days=1):
        return "yesterday"
    if t.date() == today + dt.timedelta(days=1):
        return "tomorrow"
    return t.strftime("%a %-d %b" if abs((t.date() - today).days) < 180 else "%-d %b %Y")


@app.template_filter("mswhen")
def mswhen(ms):
    return when(dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc))


@app.template_filter("datevalue")
def datevalue(s):
    t = parse_ts(s)
    return t.astimezone(BRIS).strftime("%Y-%m-%d") if t else ""


@app.context_processor
def inject():
    n = None
    if session.get("user") and request.endpoint not in ("login", "healthz", "static"):
        try:
            n = len(pending_drafts(fields=["Subject"]))
        except Exception:
            n = None
    mp = False
    if session.get("user") and request.endpoint in ("today", "approve"):
        try:
            mp = more_pending(at.settings()["fields"])
        except Exception:
            pass
    ny = None
    if session.get("user") and request.endpoint not in ("login", "healthz", "static"):
        try:
            ny = len(at.list_records("log", "AND({Event}='Handed To Karlie', NOT({Handled}))", fields=["Event"]))
        except Exception:
            ny = None
    pa, goal, nudge = None, 0, False
    if session.get("user") and request.endpoint not in ("login", "healthz", "static"):
        try:
            sf = at.settings()["fields"]
            pa, goal = actions_today(), int(sf.get("Daily Ping Limit") or 0)
            nudge = session.get("user") == "karlie" and bool(sf.get("Autopilot Nudge Sent")) and not sf.get("Autopilot Nudge Seen")
        except Exception:
            pass
    return {"user": session.get("user"), "zone_label": ZONE_LABEL, "n_to_approve": n, "more_pending": mp, "n_yours": ny,
            "version": VERSION, "pings_today": pa, "goal": goal, "autopilot_nudge": nudge}


def actions_today():
    """Pings Karlie actioned today (her day in Brisbane): drafts she approved + replies she sent herself."""
    start = dt.datetime.now(BRIS).replace(hour=0, minute=0, second=0, microsecond=0)
    since = iso(start.replace(minute=0))
    approved = at.list_records("drafts", f"AND(IS_AFTER({{Decided At}}, '{since}'), OR({{Status}}='Approved', {{Status}}='Sent'), NOT({{Written By Karlie}}), {{Kind}}!='Reply')", fields=["Decided At"])
    replies = at.list_records("log", f"AND({{Event}}='Sent', {{By}}='Karlie', IS_AFTER({{At}}, '{since}'))", fields=["At"])
    return len(approved) + len(replies)


def _norm(t):
    """Compare text the way a person would: ignore line endings, trailing spaces and extra blank lines."""
    t = (t or "").replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(l.rstrip() for l in t.split("\n"))).strip()


def celebrate(title, msg="", emoji="🎉", big=False):
    flash(json.dumps({"title": title, "msg": msg, "emoji": emoji, "big": big}).replace("</", "<\\/"), "celebrate")


def once(key, ids):
    """Return the ids not yet celebrated in this browser, and remember them (bounded, fits in the cookie)."""
    seen = session.get(key, [])
    new = [i for i in ids if i not in seen]
    if new:
        session[key] = (seen + new)[-60:]
    return new


def brands_for(records, email_field):
    """{record id: brand dict} for drafts/log rows: their Partner's logo + blurb, else the email domain's icon."""
    pids = [p for r in records for p in r["fields"].get("Partner", [])[:1]]
    by_p = brand.for_partners(pids)
    out = {}
    for r in records:
        f = r["fields"]
        pid = (f.get("Partner") or [None])[0]
        b = by_p.get(pid)
        if not b:
            dom = brand.domain_from(email=f.get(email_field))
            b = {"name": None, "logo": brand.favicon(dom), "desc": None, "website": f"https://{dom}" if dom else None, "domain": dom}
        out[r["id"]] = b
    return out


def pending_drafts(fields=None):
    horizon = iso((now_utc() + dt.timedelta(days=APPROVE_AHEAD_DAYS)).replace(minute=0, second=0, microsecond=0))
    return at.list_records(
        "drafts",
        f"AND({{Status}}='Pending Approval', OR({{Scheduled For}}='', IS_BEFORE({{Scheduled For}}, '{horizon}')))",
        sort=[("Scheduled For", "asc")], fields=fields)


# ---------- auth ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("user", "").strip().lower()
        if u in USERS and check_password_hash(USERS[u], request.form.get("password", "")):
            session.permanent = True
            session["user"] = u
            return redirect(request.args.get("next") or url_for("today"))
        flash("That name and password don't match.")
        return render_template("login.html"), 422  # Turbo only re-renders a failed form post on a 4xx
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- today ----------
@app.route("/")
@login_required
def today():
    s = at.settings()
    f = s["fields"]
    tz = ZoneInfo(f.get("Send Timezone") or "America/New_York")
    sent_today = actions_today()
    yours = at.list_records("log", "AND({Event}='Handed To Karlie', NOT({Handled}))", fields=["Summary"])
    horizon = iso((now_utc() + dt.timedelta(days=APPROVE_AHEAD_DAYS)).replace(minute=0, second=0, microsecond=0))
    upcoming = at.list_records(
        "drafts", f"AND({{Status}}='Pending Approval', IS_AFTER({{Scheduled For}}, '{horizon}'))",
        sort=[("Scheduled For", "asc")], max_records=30)
    names = at.partner_names([p for d in upcoming for p in d["fields"].get("Partner", [])])
    today_d = dt.date.today().isoformat()
    handsoff = [h for h in at.list_records("handsoff", "{Active}", sort=[("Added At", "desc")])
                if not h["fields"].get("Until") or h["fields"]["Until"] >= today_d]
    if session.get("user") == "karlie" or request.args.get("party"):
        _today_parties(f, sent_today, yours, tz)
    return render_template("today.html", s=f, sent_today=sent_today, n_yours=len(yours),
                           upcoming=upcoming, names=names, brands=brands_for(upcoming, "To Email"), handsoff=handsoff, partners=at.all_partners())


def _today_parties(f, sent_today, yours, tz):
    new_replies = once("seen_handoffs", [e["id"] for e in yours])
    if new_replies:
        celebrate("Someone wrote back!" if len(new_replies) == 1 else f"{len(new_replies)} people wrote back!",
                  "They're waiting for you in Your turn.", "💌", big=True)
    limit = f.get("Daily Ping Limit") or 0
    if limit and sent_today >= limit and once("seen_limit", [dt.datetime.now(tz).date().isoformat()]):
        celebrate("Daily pings done!", f"All {limit} out the door. Go have a coffee.", "🏆", big=True)
    since = (now_utc() - dt.timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        wins = at.list_records("sales", f"AND(OR({{Status}}='Paid',{{Status}}='To Be Invoiced',{{Status}}='Invoiced'), IS_AFTER({{Modified}}, '{since}'))",
                               fields=["Opportunity", "Value"])
    except Exception:
        wins = []
    for w in wins:
        if once("seen_wins", [w["id"]]):
            v = w["fields"].get("Value")
            celebrate("Deal closed!", (w["fields"].get("Opportunity") or "A new sponsor") + (f" · ${v:,.0f}" if v else ""), "💸", big=True)


@app.post("/settings/quick")
@login_required
def quick_settings():
    s = at.settings()
    fields = {}
    if "limit" in request.form:
        try:
            fields["Daily Ping Limit"] = max(0, min(200, int(request.form["limit"] or 0)))
        except ValueError:
            pass
    if "paused" in request.form:
        fields["Paused"] = request.form["paused"] == "1"
    if fields:
        at.update("settings", s["id"], fields)
    return redirect(url_for("today"))


@app.post("/planned/<rid>")
@login_required
def planned(rid):
    d = at.get("drafts", rid)["fields"]
    if d.get("Status") != "Pending Approval":
        return redirect(url_for("today"))
    if request.form.get("action") == "cancel":
        at.update("drafts", rid, {"Status": "Cancelled", "Decided At": iso(now_utc()),
                                  "Replan Note": f"Cancelled by {session['user']} from the dashboard."})
        flash("Cancelled. It won't go out.")
    elif request.form.get("date"):
        day = dt.date.fromisoformat(request.form["date"])
        tz = ZoneInfo(at.settings()["fields"].get("Send Timezone") or "America/New_York")
        at.update("drafts", rid, {"Scheduled For": iso(dt.datetime.combine(day, dt.time(10), tz))})
        flash("Moved to " + day.strftime("%a %-d %b") + ".")
    return redirect(url_for("today"))


# ---------- approvals ----------
@app.route("/approve")
@login_required
def approve():
    drafts = pending_drafts()
    queued = at.list_records("drafts", "{Status}='Approved'", sort=[("Expected Send", "asc")])
    asks = [l for l in (at.settings()["fields"].get("Ping Requests") or "").splitlines() if l.strip()]
    names = at.partner_names([p for d in drafts + queued for p in d["fields"].get("Partner", [])])
    sale_ids = [sid for d in drafts if d["fields"].get("Kind") == "Deal Nudge" for sid in d["fields"].get("Sale", [])]
    deals = {r["id"]: r["fields"] for r in at.list_records("sales", "OR(" + ",".join(f"RECORD_ID()='{i}'" for i in sale_ids) + ")",
                                                          fields=["Opportunity", "Status", "Created", "Value"])} if sale_ids else {}
    for v in deals.values():
        v["Opportunity"] = re.sub(r"^[?\s]*//\s*", "", v.get("Opportunity") or "")
        if v.get("Created"):
            v["Created"] = dt.date.fromisoformat(v["Created"][:10]).strftime("%-d %b %Y")
    return render_template("approve.html", drafts=drafts, names=names, queued=queued, asks=asks, short_eta=lambda s: _short_eta(parse_ts(s)),
                           brands=brands_for(drafts + queued, "To Email"), deals=deals)


@app.post("/approve/<rid>/hold")
@login_required
def approve_hold(rid):
    """'This deal is still moving': bin the nudge and leave the company alone for a while."""
    d = at.get("drafts", rid)["fields"]
    days = at.settings()["fields"].get("Stalled Deal After (Days)") or 21
    until = (dt.date.today() + dt.timedelta(days=days)).isoformat()
    pid = (d.get("Partner") or [None])[0]
    at.create("handsoff", {"Company": d.get("To Name") or d.get("To Email"), **({"Partner": [pid]} if pid else {}), "Active": True,
                           "Reason": "Deal In Progress", "Until": until, "Added By": session["user"], "Added At": iso(now_utc()),
                           "Note": "Karlie said the deal is still moving (from a stalled-deal nudge)."})
    at.update("drafts", rid, {"Status": "Cancelled", "Decided At": iso(now_utc()), "Learned From": True,
                              "Replan Note": f"Karlie: deal still in progress. Leaving them alone until {until}."})
    celebrate("Got it", f"Leaving them alone for {days} days. It'll check in again after that if the deal still hasn't closed.", "🤝")
    return redirect(url_for("approve"))


@app.post("/approve/<rid>")
@login_required
def decide(rid):
    d = at.get("drafts", rid)["fields"]
    if d.get("Status") != "Pending Approval":
        flash("That email was already dealt with.")
        return redirect(url_for("approve"))
    action = request.form.get("action", "save")
    subject, body = request.form.get("subject", "").strip(), request.form.get("body", "").strip()
    fields = {"Subject": subject, "Body": body, "Karlie Feedback": request.form.get("feedback", "").strip()}
    fields["Edited By Karlie"] = (_norm(body) != _norm(d.get("AI Original Body"))
                                  or _norm(subject) != _norm(d.get("AI Original Subject")))
    if action in ("send", "reject"):
        fields["Decided At"] = iso(now_utc())
    if action == "send":
        fields["Status"] = "Approved"
        eta, eta_t = _eta(d)
        if eta_t:
            fields["Expected Send"] = iso(eta_t)
        left = len(pending_drafts(fields=["Subject"])) - 1
        if request.headers.get("X-Fetch") == "1":
            at.update("drafts", rid, fields)
            _check_autopilot()
            return {"ok": True, "eta": eta, "left": max(left, 0), "subject": subject,
                    "eta_short": _short_eta(eta_t), "pings_today": actions_today(),
                    "goal": int(at.settings()["fields"].get("Daily Ping Limit") or 0)}
        if left <= 0:
            celebrate("All clear!", eta + " Every draft's dealt with.", "✨", big=True)
        else:
            celebrate("Off it goes!", eta + f" {left} more to look at.", "🚀")
    elif action == "reject":
        fields["Status"] = "Rejected"
        flash("Binned. It'll learn from why.")
    else:
        flash("Edits saved.")
    at.update("drafts", rid, fields)
    return redirect(url_for("approve"))


def _eta(d):
    """(plain-English 'when will this go' in Brisbane time and theirs, UTC datetime or None)."""
    try:
        w = engine.World()  # settings only; tables load lazily and we don't touch them here
        cid = (d.get("Contact") or [None])[0]
        contact = at.get("contacts", cid) if cid else None
        t = engine.next_send_time(w, contact)
        if not t:
            return "It'll go out once there's a send day and time set on the Rules page.", None
        tz = w.tz_for(contact)
        fmt = lambda x, f: x.strftime(f).replace("AM", "am").replace("PM", "pm")
        who = (d.get("To Name") or "them").split()[0]
        place = tz.key.split("/")[-1].replace("_", " ")
        line = (f"Goes out around {fmt(t.astimezone(BRIS), '%a %-d %b, %-I:%M%p')} your time "
                f"({fmt(t.astimezone(tz), '%a %-I:%M%p')} for {who} in {place}).")
        if w.s.get("Paused"):
            line = "Sending is paused, so it's waiting. Once you press Start sending: " + line[0].lower() + line[1:]
        return line, t
    except Exception:
        return "It'll go out in the next send window.", None


def _eta_line(d):
    return _eta(d)[0]


def _short_eta(t):
    if not t:
        return "waiting for a send window"
    return "goes out ~" + t.astimezone(BRIS).strftime("%a %-I:%M%p").replace("AM", "am").replace("PM", "pm")


@app.post("/approve/<rid>/undo")
@login_required
def approve_undo(rid):
    d = at.get("drafts", rid)["fields"]
    if d.get("Status") == "Approved":
        at.update("drafts", rid, {"Status": "Pending Approval", "Decided At": None, "Expected Send": None})
        flash("Pulled back. It's in To approve again.")
    else:
        flash("Too late, that one has already gone.")
    return redirect(url_for("approve"))


@app.post("/approve/<rid>/remix")
@login_required
def remix(rid):
    d = at.get("drafts", rid)["fields"]
    instr = request.form.get("instruction", "").strip()
    if not instr or d.get("Status") not in ("Pending Approval", "Approved"):
        return redirect(url_for("approve"))
    at.update("drafts", rid, {"Subject": request.form.get("subject", d.get("Subject", "")).strip(),
                              "Body": request.form.get("body", d.get("Body", "")).strip(), "Remix Request": instr})
    flash("Rewriting it now. The new version usually shows up here in about a minute.")
    return redirect(url_for("approve") + f"#d-{rid}")


def more_pending(f):
    started, last = parse_ts(f.get("More Drafts Started")), parse_ts(f.get("Last Brain Run"))
    recent = started and now_utc() - started < dt.timedelta(minutes=20)
    return bool(f.get("More Drafts Requested")) or bool(recent and (not last or last < started))


@app.post("/sync")
@login_required
def sync_now():
    """Pull in everything new from her inbox right now, then have the laptop learn from it within a minute or two."""
    try:
        w = engine.World()
        got = engine.sync_inbox(w)
    except Exception as ex:
        flash(f"Couldn't reach Gmail just now ({ex}). Try again in a minute.")
        return redirect(request.referrer or url_for("today"))
    s = at.settings()
    at.update("settings", s["id"], {"Sync Requested": True})
    at._cache.clear()
    parts = []
    for k, label in (("replied", "new repl{}"), ("karlie", "email{} you sent yourself"), ("bounced", "bounce{}"), ("left", "person{} who left")):
        n = got.get(k, 0) if isinstance(got, dict) else 0
        if n:
            parts.append(f"{n} " + label.format("ies" if (k == "replied" and n > 1) else ("y" if k == "replied" else ("s" if n > 1 else ""))).replace("persons", "people"))
    msg = ("Found " + ", ".join(parts) + ".") if parts else "All up to date, nothing new in your inbox."
    flash(msg + " It's learning from your latest edits and replies now; summaries and suggested replies update in a minute or two.")
    return redirect(request.referrer or url_for("today"))


@app.post("/ask")
@login_required
def ask_ping():
    text = " ".join(request.form.get("ask", "").split())
    if text:
        s = at.settings()
        cur = (s["fields"].get("Ping Requests") or "").strip()
        line = f"[{dt.date.today().isoformat()} {session['user']}] {text}"
        at.update("settings", s["id"], {"Ping Requests": (cur + "\n" + line).strip()})
        flash("Got it. That ping will be written in the next couple of minutes and land here for you to approve.")
    return redirect(url_for("approve"))


@app.post("/more")
@login_required
def more_drafts():
    s = at.settings()
    if not more_pending(s["fields"]):
        at.update("settings", s["id"], {"More Drafts Requested": 5})
    flash("On it. Writing 5 more now. They'll pop into To approve in a few minutes.")
    return redirect(request.referrer or url_for("approve"))


@app.get("/approve/status")
@login_required
def approve_status():
    return {"remixing": [d["id"] for d in at.list_records("drafts", "AND({Remix Request}!='', {Status}='Pending Approval')", fields=["Subject"])],
            "more_pending": more_pending(at.settings()["fields"]),
            "asks": bool((at.settings()["fields"].get("Ping Requests") or "").strip()),
            "remixing_replies": [x["id"] for x in at.list_records("log", "AND({Remix Request}!='', NOT({Handled}))", fields=["Event"])],
            "pending": len(pending_drafts(fields=["Subject"]))}


@app.get("/api/engine/remix")
def engine_remix_list():
    if not engine_auth():
        return {"error": "unauthorised"}, 401
    return engine.remix_queue()


@app.get("/api/engine/summaries")
def engine_summaries():
    if not engine_auth():
        return {"error": "unauthorised"}, 401
    return engine.summaries_queue()


@app.post("/api/engine/remix")
def engine_remix_apply():
    if not engine_auth():
        return {"error": "unauthorised"}, 401
    return engine.remix_apply(request.get_json(force=True))


# ---------- replies handed to her ----------
@app.route("/yours")
@login_required
def yours():
    ev = at.list_records("log", "AND({Event}='Handed To Karlie', NOT({Handled}))", sort=[("At", "desc")])
    names = at.partner_names([p for e in ev for p in e["fields"].get("Partner", [])])
    threads = {}
    for e in ev:
        tid = e["fields"].get("Gmail Thread ID")
        if tid and tid not in threads:
            try:
                msgs = gmail.thread(tid)
                threads[tid] = {"msgs": msgs[-4:], "earlier": max(0, len(msgs) - 4),
                                "link": gmail.open_link(msgs[-1]["msgid"]) if msgs[-1]["msgid"] else None}
            except Exception:
                threads[tid] = None
    return render_template("yours.html", events=ev, names=names, threads=threads, brands=brands_for(ev, "Email"))


@app.post("/yours/<rid>/done")
@login_required
def yours_done(rid):
    e = at.get("log", rid)["fields"]
    at.update("log", rid, {"Handled": True})
    if request.form.get("outcome") == "mine":
        names = at.partner_names(e.get("Partner", []))
        company = ", ".join(names.values()) or e.get("Email", "")
        at.create("handsoff", {"Company": company, "Partner": e.get("Partner", []), "Active": True,
                               "Reason": "Karlie Took Over", "Added By": session["user"], "Added At": iso(now_utc()),
                               "Note": "Taken over from Your turn."})
        flash(f"{company} is all yours. The system won't email them until you hand them back.")
    else:
        flash("Handed back. The system will carry on with them.")
    return redirect(url_for("yours"))


# ---------- hands off ----------
@app.post("/handsoff")
@login_required
def handsoff_add():
    name = request.form.get("company", "").strip()
    if not name:
        return redirect(url_for("today") + "#handsoff")
    match = [pid for pid, n in at.all_partners() if n.lower() == name.lower()]
    until = request.form.get("until") or None
    at.create("handsoff", {"Company": name, "Partner": match[:1], "Active": True,
                           "Reason": request.form.get("reason") or "Talking On WhatsApp",
                           "Until": until, "Note": request.form.get("note", "").strip(),
                           "Added By": session["user"], "Added At": iso(now_utc())})
    flash(f"Got it. Hands off {name}" + (f" until {dt.date.fromisoformat(until).strftime('%-d %b')}." if until else " until you release them."))
    return redirect(url_for("today") + "#handsoff")


@app.post("/handsoff/<rid>/release")
@login_required
def handsoff_release(rid):
    at.update("handsoff", rid, {"Active": False, "Note": (at.get("handsoff", rid)["fields"].get("Note", "") +
                                f"\nReleased by {session['user']} {dt.date.today().isoformat()}").strip()})
    flash("Released. The system can email them again.")
    return redirect(url_for("today") + "#handsoff")


AUTOPILOT_STREAK = 50


def approval_streak():
    """How many of her most recent approvals in a row went out exactly as written (no edits, no note, no bins)."""
    n = 0
    for d in at.list_records("drafts", "AND({Decided At}!='', {Kind}!='Reply', NOT({Written By Karlie}), OR({Status}='Approved', {Status}='Sent', {Status}='Rejected'))",
                             sort=[("Decided At", "desc")], fields=["Status", "Edited By Karlie", "Karlie Feedback"], max_records=AUTOPILOT_STREAK + 5):
        f = d["fields"]
        if f.get("Status") == "Rejected" or f.get("Edited By Karlie") or (f.get("Karlie Feedback") or "").strip():
            break
        n += 1
    return n


def _check_autopilot():
    """50 approvals in a row with zero edits = phase 1 is done. Tell Danny on Slack and Karlie on her page (once)."""
    try:
        s = at.settings()
        if s["fields"].get("Autopilot Nudge Sent") or s["fields"].get("Approval Mode") != "Approve Every Draft":
            return
        if approval_streak() < AUTOPILOT_STREAK:
            return
        at.update("settings", s["id"], {"Autopilot Nudge Sent": True})
        tok = os.environ.get("SLACK_BOT_TOKEN")
        if tok:
            requests.post("https://slack.com/api/chat.postMessage", headers={"Authorization": f"Bearer {tok}"}, timeout=15, json={
                "channel": os.environ.get("DANNY_SLACK_ID", "UJF18F3SA"),
                "text": (f"🎉 Karlie has approved {AUTOPILOT_STREAK} pings in a row without changing a word. Phase 1 looks done: "
                         "the drafts are good enough to send without her approval. Switch W6 Pings to "
                         "\"Send new pitches on its own, ask me before replying to anyone\" on the Rules page "
                         "(https://pings.workspace6.io/rules) when you two are ready. Takes 1 minute.")})
    except Exception:
        pass


@app.post("/autopilot/seen")
@login_required
def autopilot_seen():
    s = at.settings()
    at.update("settings", s["id"], {"Autopilot Nudge Seen": True})
    return {"ok": True}


@app.post("/yours/<rid>/save")
@login_required
def yours_save(rid):
    at.update("log", rid, {"Suggested Reply": request.form.get("body", "").strip()})
    flash("Edits saved.")
    return redirect(url_for("yours") + f"#d-{rid}")


@app.post("/yours/<rid>/remix")
@login_required
def yours_remix(rid):
    instr = request.form.get("instruction", "").strip()
    if instr:
        at.update("log", rid, {"Suggested Reply": request.form.get("body", "").strip(), "Remix Request": instr})
        flash("Rewriting it now. The new version usually shows up here in about a minute.")
    return redirect(url_for("yours") + f"#d-{rid}")


@app.post("/yours/<rid>/reply")
@login_required
def yours_reply(rid):
    e = at.get("log", rid)["fields"]
    text = request.form.get("body", "").strip()
    tid = e.get("Gmail Thread ID")
    if not text or not tid:
        flash("Nothing to send.")
        return redirect(url_for("yours"))
    try:
        mid, to = gmail.reply(tid, text)
    except Exception as ex:
        flash(f"Couldn't send that one: {ex}. Nothing went out; your text is below.")
        session["unsent_" + rid] = text
        return redirect(url_for("yours"))
    t = iso(now_utc())
    link = {"Partner": e.get("Partner", []), "Sale": e.get("Sale", [])}
    if e.get("Contact"):
        link["Contact"] = e["Contact"]
    sug = (e.get("Suggested Reply") or "").strip()
    draft = at.create("drafts", {"Subject": "Reply from Karlie", "Status": "Sent", "Kind": "Reply",
                                 "To Email": to, "Body": text, "Written By Karlie": not sug,
                                 **({"AI Original Body": sug, "Edited By Karlie": _norm(text) != _norm(sug)} if sug else {}),
                                 **({"Karlie Feedback": request.form.get("feedback", "").strip()} if request.form.get("feedback", "").strip() else {}),
                                 "Gmail Thread ID": tid, "Sent At": t, "Decided At": t, **link})
    at.create("log", {"Summary": "Karlie replied from the dashboard", "Event": "Sent", "Direction": "Out",
                      "Email": to, "At": t, "Snippet": text[:500], "Gmail Message ID": mid,
                      "Gmail Thread ID": tid, "Draft": [draft["id"]], "Handled": True, **link})
    at.update("log", rid, {"Handled": True})
    try:
        gmail.mark_done(tid)  # read + archived in her inbox, since she's dealt with it
    except Exception:
        pass
    celebrate("Sent!", f"On its way to {to}. Marked read and archived in your inbox, and it'll learn from how you wrote it.", "💌")
    return redirect(url_for("yours"))


# ---------- rules ----------
NUM_FIELDS = ["Daily Ping Limit", "Send Window Start (Hour)", "Send Window End (Hour)", "Max Follow-ups",
              "Follow-up 1 After (Days)", "Follow-up 2 After (Days)", "Follow-up 3 After (Days)",
              "Days Before Re-pitching A Company", "Rescue Tries Per Company", "Rest After No Reply (Days)",
              "Stalled Deal After (Days)", "Stalled Deal Nudges Per Day"]
CHECKS = ["Follow-ups Must Add Something New", "Gone-Contact Rescue", "Optimise Send Timing",
          "Honour Requested Timing"]


@app.route("/rules", methods=["GET", "POST"])
@login_required
def rules():
    s = at.settings()
    if request.method == "POST":
        fm = request.form
        fields = {}
        for k in NUM_FIELDS:
            v = fm.get(k, "").strip()
            fields[k] = int(v) if v.isdigit() else None
        for k in CHECKS:
            fields[k] = bool(fm.get(k))
        fields["Send Days"] = [d for d in DAYS if fm.get("day_" + d)]
        fields["Send Timezone"] = fm.get("Send Timezone") if fm.get("Send Timezone") in ZONES else None
        fields["Approval Mode"] = fm.get("Approval Mode") if fm.get("Approval Mode") in MODES else "Approve Every Draft"
        fields["Notes"] = fm.get("Notes", "")
        at.update("settings", s["id"], fields)
        flash("Rules saved.")
        return redirect(url_for("rules"))
    lessons = at.list_records("lessons", sort=[("Learned At", "desc")], max_records=60)
    return render_template("rules.html", s=s["fields"], days=DAYS, zones=ZONES, modes=MODES, lessons=lessons)


@app.post("/offers")
@login_required
def offers_save():
    text = request.form.get("offers", "").strip()
    if text:
        s = at.settings()
        at.update("settings", s["id"], {"Offer Rules": text})
        flash("Saved. Every new draft, rewrite and suggested reply follows these prices and rules from now on.")
    return redirect(url_for("rules") + "#offers")


@app.post("/voice/note")
@login_required
def voice_note():
    note = request.form.get("note", "").strip()
    if note:
        u = session["user"]
        at.create("lessons", {"Lesson": note[:250], "Evidence": note if len(note) > 250 else "",
                              "Active": True, "About": "Voice", "Applies To": "All Emails",
                              "Source": "Karlie's Note" if u == "karlie" else "Rule From Danny",
                              "Added By": u, "Learned At": iso(now_utc())})
        flash("Got it. Every new draft follows this from now on, and it'll be written into the voice guide on the next update.")
    return redirect(url_for("rules") + "#voice")


@app.post("/lessons/<rid>/fix")
@login_required
def lesson_fix(rid):
    new = request.form.get("lesson", "").strip()
    old = at.get("lessons", rid)["fields"]
    if new and new != old.get("Lesson"):
        stamp = f"Corrected by {session['user']} on {dt.date.today().isoformat()}. Was: {old.get('Lesson', '')}"
        at.update("lessons", rid, {"Lesson": new[:250], "Active": True, "Folded Into Voice": False,
                                   "Evidence": (stamp + "\n" + (old.get("Evidence") or "")).strip()})
        flash("Corrected. Every new draft follows the new wording from now on.")
    return redirect(url_for("rules") + "#learned")


@app.post("/lessons/<rid>")
@login_required
def lesson_toggle(rid):
    at.update("lessons", rid, {"Active": request.form.get("active") == "1"})
    return redirect(url_for("rules") + "#learned")


# ---------- bookings ----------
@app.route("/bookings")
@login_required
def bookings_page():
    today = dt.date.today()
    view = request.args.get("view", "calendar")
    try:
        month = dt.date.fromisoformat((request.args.get("month") or today.strftime("%Y-%m")) + "-01")
    except ValueError:
        month = today.replace(day=1)
    nxt = (month + dt.timedelta(days=32)).replace(day=1)
    prev = (month - dt.timedelta(days=1)).replace(day=1)
    grid_start = month - dt.timedelta(days=month.weekday())
    grid_end = (nxt - dt.timedelta(days=1))
    grid_end = grid_end + dt.timedelta(days=6 - grid_end.weekday())
    list_end = today + dt.timedelta(days=90)
    rows = bk.load(min(grid_start, today), max(grid_end, list_end))
    by_day = {}
    for b in rows:
        by_day.setdefault(b["date"], []).append(b)

    weeks, d = [], grid_start
    while d <= grid_end:
        days = [d + dt.timedelta(days=i) for i in range(7)]
        # Welcome Flow runs every day, so draw each sponsor's run as one band across the week instead of 7 chips
        bands, seen = [], {}
        for i, day in enumerate(days):
            for b in by_day.get(day, []):
                if b["kind"] == "welcome" or b["state"] == "blockout":
                    k = (b["sponsor"], b["state"], b["kind"])
                    if k in seen and seen[k]["end"] == i - 1:
                        seen[k]["end"] = i
                    else:
                        seen[k] = {"sponsor": b["sponsor"], "state": b["state"], "kind": b["kind"], "start": i, "end": i, "id": b["id"]}
                        bands.append(seen[k])
        cells = [{"date": day, "in_month": day.month == month.month, "today": day == today, "past": day < today,
                  "entries": [b for b in by_day.get(day, []) if b["kind"] != "welcome" and b["state"] != "blockout"],
                  } for day in days]
        weeks.append({"cells": cells, "bands": bands})
        d += dt.timedelta(days=7)
    upcoming = [b for b in rows if today <= b["date"] <= list_end and b["state"] != "blockout"]
    blockouts = []
    for b in sorted([b for b in rows if b["state"] == "blockout" and b["date"] >= today], key=lambda x: (x["kind"], x["date"])):
        if blockouts and blockouts[-1]["kind"] == b["kind"] and (b["date"] - blockouts[-1]["end"]).days == 1:
            blockouts[-1]["end"] = b["date"]; blockouts[-1]["ids"].append(b["id"])
        else:
            blockouts.append({"kind": b["kind"], "start": b["date"], "end": b["date"], "ids": [b["id"]]})
    groups = {}
    for b in upcoming:
        wk = b["date"] - dt.timedelta(days=b["date"].weekday())
        groups.setdefault(wk, []).append(b)
    bbrands = brand.for_partners([b["partner"] for b in upcoming])
    return render_template("bookings.html", view=view, month=month, prev=prev, nxt=nxt, weeks=weeks, groups=sorted(groups.items()), bbrands=bbrands,
                           types=bk.TYPES, partners=at.all_partners(), today=today, blockouts=blockouts,
                           n_pencilled=sum(1 for b in upcoming if b["state"] == "pencilled"), n_paid=sum(1 for b in upcoming if b["state"] == "paid"),
                           counts={k: sum(1 for b in upcoming if b["kind"] == k) for k in bk.TYPES})


def _ensure_partner(name):
    """Partner id for this sponsor name, creating a Partners row if it's a new sponsor."""
    name = (name or "").strip()
    if not name:
        return None, False
    pid = next((p for p, n in at.all_partners() if n.lower() == name.lower()), None)
    if pid:
        return pid, False
    rec = at.create("partners", {"Name": name, "Stage": "Engaged"}, typecast=True)
    at._names_cache["at"] = 0  # refresh the picker
    return rec["id"], True


@app.post("/bookings/understand")
@login_required
def bookings_understand():
    return bk.parse_request(request.form.get("text", ""), at.all_partners())


@app.post("/bookings/confirm")
@login_required
def bookings_confirm():
    fm = request.form
    if fm.get("state") == "blockout":
        kind = fm.get("kind") if fm.get("kind") in bk.TYPES or fm.get("kind") == "all" else None
        ds = [d for d in fm.get("dates", "").split(",") if re.match(r"\d{4}-\d{2}-\d{2}$", d)]
        if not kind or len(ds) < 2:
            flash("Something went wrong reading that blockout. Nothing was added.")
            return redirect(url_for("bookings_page"))
        days = bk.add_blockout(kind, dt.date.fromisoformat(ds[0]), dt.date.fromisoformat(ds[-1]))
        flash(f"Blocked out {'everything' if kind == 'all' else bk.TYPES[kind]['label']} for {len(days)} day{'s' if len(days) > 1 else ''}. Nothing will be booked or pitched for those dates.")
        return redirect(url_for("bookings_page", month=ds[0][:7]))
    kind = fm.get("kind") if fm.get("kind") in bk.TYPES else None
    dates = [d for d in fm.get("dates", "").split(",") if re.match(r"\d{4}-\d{2}-\d{2}$", d)]
    if not kind or not dates:
        flash("Something went wrong reading that booking. Nothing was added.")
        return redirect(url_for("bookings_page"))
    pid, created = _ensure_partner(fm.get("sponsor"))
    bk.add_dates(fm.get("sponsor").strip(), pid, kind, fm.get("state", "pencilled"), dates)
    note = " Added them to your Partners list too." if created else ""
    celebrate("Booked!" if fm.get("state") == "paid" else "Pencilled in!", f"{fm.get('sponsor')} · {bk.TYPES[kind]['label']} · {len(dates)} date{'s' if len(dates) > 1 else ''}.{note}",
              "💸" if fm.get("state") == "paid" else "✏️", big=fm.get("state") == "paid")
    return redirect(url_for("bookings_page", month=dates[0][:7]))


@app.post("/bookings/add")
@login_required
def bookings_add():
    fm = request.form
    name = fm.get("sponsor", "").strip()
    kind = fm.get("kind") if fm.get("kind") in bk.TYPES else "marquee"
    try:
        start = dt.date.fromisoformat(fm.get("start"))
        count = int(fm.get("count") or 1)
    except (TypeError, ValueError):
        flash("Pick a start date first.")
        return redirect(url_for("bookings_page"))
    if fm.get("state") == "blockout":
        kind = fm.get("kind") if fm.get("kind") in bk.TYPES or fm.get("kind") == "all" else "marquee"
        end = start + dt.timedelta(days=max(1, min(count, 366)) - 1)
        days = bk.add_blockout(kind, start, end)
        flash(f"Blocked out {'everything' if kind == 'all' else bk.TYPES[kind]['label']} from {start.strftime('%-d %b')} to {end.strftime('%-d %b')}.")
        return redirect(url_for("bookings_page", month=start.strftime("%Y-%m")))
    if not name:
        flash("Add the sponsor's name (or choose ⛔ Block out for dates with no sponsor).")
        return redirect(url_for("bookings_page"))
    clash = bk.blocked_dates(kind, bk.dates_for(start, max(1, min(count, 120)), fm.get("cadence", "once")))
    if clash:
        flash(f"{bk.TYPES[kind]['label']} is blocked out on " + ", ".join(d.strftime('%a %-d %b') for d in clash[:5]) + ". Nothing was booked.")
        return redirect(url_for("bookings_page", month=start.strftime("%Y-%m")))
    pid, created = _ensure_partner(name)
    ds = bk.add(name or "Sponsor", pid, kind, fm.get("state", "pencilled"), start, count, fm.get("cadence", "once"))
    celebrate("Booked!", f"{name} · {bk.TYPES[kind]['label']} · {len(ds)} date{'s' if len(ds) > 1 else ''} from {ds[0].strftime('%a %-d %b')}", "📅",
              big=fm.get("state") == "paid")
    return redirect(url_for("bookings_page", month=ds[0].strftime("%Y-%m"), view=fm.get("view", "calendar")))


@app.post("/bookings/unblock")
@login_required
def bookings_unblock():
    ids = [i for i in request.form.get("ids", "").split(",") if i.startswith("rec")]
    for i in ids:
        at.delete("promo", i)
    flash("Blockout removed. Those dates are open again.")
    return redirect(request.referrer or url_for("bookings_page"))


@app.get("/bookings/<rid>/group")
@login_required
def bookings_group(rid):
    g = bk.group_of(rid)
    ds = sorted(x["fields"].get("Publish Date", "") for x in g)
    return {"count": len(g), "first": ds[0] if ds else "", "last": ds[-1] if ds else ""}


@app.post("/bookings/<rid>/edit")
@login_required
def bookings_edit(rid):
    fm = request.form
    r = at.get("promo", rid)["fields"]
    kind, state = bk.parse_status(r.get("Status"))
    whole = fm.get("scope") != "one"
    if fm.get("action") in ("delete", "delete_one"):
        if fm.get("action") == "delete_one":
            whole = False
        rows = bk.group_of(rid) if whole else [{"id": rid}]
        for x in rows:
            at.delete("promo", x["id"])
        flash(f"Removed the whole booking ({len(rows)} dates)." if len(rows) > 1 else "Removed that date.")
    elif whole and fm.get("state") in ("paid", "pencilled") and kind:
        rows = bk.group_of(rid)
        new = ("🤑 " if fm["state"] == "paid" else "✏️ ") + bk.TYPES[kind]["status"]
        for x in rows:
            if x["fields"].get("Status") != new:
                at.update("promo", x["id"], {"Status": new})
        if fm.get("date") and fm["date"] != r.get("Publish Date"):
            at.update("promo", rid, {"Publish Date": fm["date"]})
        if fm["state"] == "paid" and state != "paid":
            celebrate("Paid! 🤑", f"{r.get('Name', '')} · all {len(rows)} dates", "💸", big=True)
        else:
            flash(f"Updated the whole booking ({len(rows)} dates)." if len(rows) > 1 else "Updated.")
    else:
        fields = {}
        if fm.get("state") in ("paid", "pencilled") and kind:
            fields["Status"] = ("🤑 " if fm["state"] == "paid" else "✏️ ") + bk.TYPES[kind]["status"]
        if fm.get("date"):
            fields["Publish Date"] = fm["date"]
        if fields:
            at.update("promo", rid, fields)
            if fields.get("Status", "").startswith("🤑") and state != "paid":
                celebrate("Paid! 🤑", r.get("Name", ""), "💸", big=True)
            else:
                flash("Updated.")
    return redirect(request.referrer or url_for("bookings_page"))


# ---------- results ----------
def activity(days=7):
    """Everything Karlie actioned, with proof of what happened next (sent / waiting / not sent / failed) and what it learned."""
    since = iso((dt.datetime.now(BRIS) - dt.timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0))
    rows = at.list_records("drafts", f"IS_AFTER({{Decided At}}, '{since}')", sort=[("Decided At", "desc")], max_records=200)
    lessons = {}
    for l in at.list_records("lessons", f"IS_AFTER({{Learned At}}, '{since}')", fields=["Lesson", "Draft", "Source"]):
        for d in l["fields"].get("Draft", []):
            lessons.setdefault(d, []).append(l["fields"].get("Lesson"))
    out, now = [], now_utc()
    for d in rows:
        f = d["fields"]
        st, kind = f.get("Status"), f.get("Kind")
        who = f.get("To Name") or f.get("To Email") or ""
        if kind == "Reply":
            what = f"Replied to {who}"
        elif st == "Rejected":
            what = f"Binned “{f.get('Subject') or 'draft'}”"
        else:
            what = f"Approved “{f.get('Subject') or 'follow-up'}” to {who}"
        if st == "Sent":
            level, proof = "ok", "Sent " + (when(f.get("Sent At")) if f.get("Sent At") else "")
        elif st == "Approved":
            exp = parse_ts(f.get("Expected Send"))
            if exp and now - exp > dt.timedelta(hours=3):
                level, proof = "warn", "Late: expected " + when(f.get("Expected Send")) + ". Check sending isn't paused."
            else:
                level, proof = "wait", "Waiting to send" + (", goes " + _short_eta(exp).replace("goes out ", "") if exp else "")
        elif st == "Cancelled":
            level, proof = "warn", "Not sent: " + (f.get("Replan Note") or "cancelled")
        elif st == "Failed":
            level, proof = "bad", "Failed: " + (f.get("Replan Note") or "unknown error")
        elif st == "Rejected":
            level, proof = "wait", "Binned. Nothing sent."
        else:
            level, proof = "wait", st or ""
        edited = f.get("Edited By Karlie") and kind != "Reply" or (kind == "Reply" and f.get("AI Original Body") and f.get("Edited By Karlie"))
        learned = lessons.get(d["id"], [])
        learn_state = ("learned" if learned else ("done" if f.get("Learned From") else
                       ("pending" if st != "Cancelled" and (edited or f.get("Karlie Feedback") or st == "Rejected" or f.get("Written By Karlie")) else "")))
        out.append({"at": f.get("Decided At"), "what": what, "level": level, "proof": proof, "edited": bool(edited),
                    "note": f.get("Karlie Feedback"), "learned": learned, "learn_state": learn_state})
    for r in at.list_records("log", f"AND({{Event}}='Sent', {{By}}='Karlie', {{Summary}}='Karlie emailed them directly', IS_AFTER({{At}}, '{since}'))", fields=["At", "Email", "Reviewed"]):
        f = r["fields"]
        out.append({"at": f.get("At"), "what": f"Emailed {f.get('Email')} from Gmail", "level": "ok", "proof": "Seen by the system " + when(f.get("At")),
                    "edited": False, "note": None, "learned": [], "learn_state": "done" if f.get("Reviewed") else "pending"})
    out.sort(key=lambda x: x["at"] or "", reverse=True)
    return out


@app.route("/results")
@login_required
def results():
    since = (now_utc() - dt.timedelta(days=56)).strftime("%Y-%m-%d")
    ev = at.list_records("log", f"IS_AFTER({{At}}, '{since}')", sort=[("At", "desc")])
    now = dt.datetime.now(BRIS)
    at_of = lambda e: parse_ts(e["fields"].get("At") or "1970-01-01T00:00:00Z")
    weeks = []
    for w in range(7, -1, -1):
        start = (now - dt.timedelta(days=now.weekday() + 7 * w)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + dt.timedelta(days=7)
        inw = [e for e in ev if start <= at_of(e).astimezone(BRIS) < end]
        weeks.append({"label": start.strftime("%-d %b"),
                      "sent": sum(e["fields"].get("Event") == "Sent" for e in inw),
                      "replied": sum(e["fields"].get("Event") == "Replied" for e in inw)})
    last30 = [e for e in ev if at_of(e) > now_utc() - dt.timedelta(days=30)]
    c = lambda k: sum(e["fields"].get("Event") == k for e in last30)
    totals = {"sent": c("Sent"), "replied": c("Replied"), "handed": c("Handed To Karlie"),
              "bounced": c("Bounced") + c("Left Company")}
    totals["rate"] = round(100 * totals["replied"] / totals["sent"]) if totals["sent"] else None
    dead = at.list_records("contacts", "AND({Status}!='Active', {Status}!='')", fields=["Status"])
    peak = max([w["sent"] for w in weeks] + [1])
    names = at.partner_names([p for e in ev[:40] for p in e["fields"].get("Partner", [])])
    span = 1 if request.args.get("span", "today") == "today" else 7
    return render_template("results.html", weeks=weeks, peak=peak, totals=totals, acts=activity(span), span=span,
                           n_dead=len(dead), recent=ev[:40], names=names)


# ---------- engine endpoints (cron + the Claude routine) ----------
def engine_auth():
    tok = os.environ.get("ENGINE_TOKEN")
    return tok and request.headers.get("X-Engine-Token") == tok


@app.post("/tasks/tick")
def task_tick():
    if not engine_auth():
        return {"error": "unauthorised"}, 401
    try:
        with OpsLog("w6_pings_tick", job_name="W6 Pings: inbox sync + send", platform="Render", log_runs=False) as ops:
            out = engine.tick()
            for s in out.get("send", {}).get("skipped", []) or []:
                if str(s).startswith("failed"):
                    ops.warn(f"send {s}")
            return out
    except Exception as ex:
        import traceback
        return {"error": str(ex), "trace": traceback.format_exc()[-2000:]}, 500


@app.get("/api/engine/context")
def engine_context():
    if not engine_auth():
        return {"error": "unauthorised"}, 401
    mx = request.args.get("max_new", type=int)
    more = request.args.get("more", default=0, type=int)
    ro = request.args.get("requests") == "1"
    lo = request.args.get("learn") == "1"
    return app.response_class(json.dumps(engine.context(max_new=mx, more=more, requests_only=ro, learn_only=lo), default=str), mimetype="application/json")


@app.post("/api/engine/apply")
def engine_apply():
    if not engine_auth():
        return {"error": "unauthorised"}, 401
    with OpsLog("w6_pings_brain", job_name="W6 Pings: brain (drafts + learning)", platform="Render") as ops:
        out = engine.apply(request.get_json(force=True))
        ops.step(json.dumps(out))
        return out


VERSION = (os.environ.get("RENDER_GIT_COMMIT") or "dev")[:12]


@app.route("/version")
def version():
    return VERSION, 200, {"Cache-Control": "no-store", "Content-Type": "text/plain"}


@app.route("/healthz")
def healthz():
    return "ok"


def _enrich_brands(limit=4):
    """Fetch logos + 'what they do' for brands on screen that haven't been looked up yet (a few per cycle)."""
    recs = pending_drafts(fields=["Partner"]) + at.list_records("log", "AND({Event}='Handed To Karlie', NOT({Handled}))", fields=["Partner"])
    pids = list(dict.fromkeys(p for r in recs for p in r["fields"].get("Partner", [])[:1]))
    if not pids:
        return
    formula = "OR(" + ",".join(f"RECORD_ID()='{i}'" for i in pids[:90]) + ")"
    todo = [r["id"] for r in at.list_records("partners", formula, fields=["🤖 Brand Checked"]) if not r["fields"].get("🤖 Brand Checked")]
    for pid in todo[:limit]:
        try:
            brand.enrich_partner(pid)
        except Exception:
            pass


def _keep_warm():
    """Re-render every page in the background so the Airtable cache is always fresh and tab switches are instant."""
    import threading, time as _t

    def loop():
        _t.sleep(5)
        while True:
            try:
                c = app.test_client()
                with c.session_transaction() as sess:
                    sess["user"] = "warm"
                for path in ("/", "/approve", "/yours", "/bookings", "/bookings?view=list", "/rules", "/results"):
                    c.get(path)
                _enrich_brands()
            except Exception:
                pass
            _t.sleep(30)

    threading.Thread(target=loop, daemon=True).start()


if os.environ.get("RENDER"):
    _keep_warm()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8090)), debug=bool(os.environ.get("DEBUG")))
