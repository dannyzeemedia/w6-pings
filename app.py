"""Karlie's outbound dashboard: daily ping count, approvals, replies to handle, rules, voice, results.
Airtable (W6 Media base) is the database; this app is only a friendlier screen over it."""
import os, json, datetime as dt
from functools import wraps
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, redirect as _redirect, url_for, session, flash
from werkzeug.security import check_password_hash
import airtable as at
import gmail
import engine
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
    return {"user": session.get("user"), "zone_label": ZONE_LABEL, "n_to_approve": n, "more_pending": mp}


def celebrate(title, msg="", emoji="🎉", big=False):
    flash(json.dumps({"title": title, "msg": msg, "emoji": emoji, "big": big}).replace("</", "<\\/"), "celebrate")


def once(key, ids):
    """Return the ids not yet celebrated in this browser, and remember them (bounded, fits in the cookie)."""
    seen = session.get(key, [])
    new = [i for i in ids if i not in seen]
    if new:
        session[key] = (seen + new)[-60:]
    return new


def pending_drafts(fields=None):
    horizon = iso(now_utc() + dt.timedelta(days=APPROVE_AHEAD_DAYS))
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
    since = (now_utc() - dt.timedelta(days=2)).strftime("%Y-%m-%d")
    sent = at.list_records("log", f"AND({{Event}}='Sent', IS_AFTER({{At}}, '{since}'))", fields=["At"])
    sent_today = sum(1 for r in sent if parse_ts(r["fields"].get("At")) and
                     parse_ts(r["fields"]["At"]).astimezone(tz).date() == dt.datetime.now(tz).date())
    yours = at.list_records("log", "AND({Event}='Handed To Karlie', NOT({Handled}))", fields=["Summary"])
    horizon = iso(now_utc() + dt.timedelta(days=APPROVE_AHEAD_DAYS))
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
                           upcoming=upcoming, names=names, handsoff=handsoff, partners=at.all_partners())


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
    names = at.partner_names([p for d in drafts + queued for p in d["fields"].get("Partner", [])])
    return render_template("approve.html", drafts=drafts, names=names, queued=queued, short_eta=lambda s: _short_eta(parse_ts(s)))


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
    fields["Edited By Karlie"] = (body != (d.get("AI Original Body") or "").strip()
                                  or subject != (d.get("AI Original Subject") or "").strip())
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
            return {"ok": True, "eta": eta, "left": max(left, 0), "subject": subject,
                    "eta_short": _short_eta(eta_t)}
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
            "pending": len(pending_drafts(fields=["Subject"]))}


@app.get("/api/engine/remix")
def engine_remix_list():
    if not engine_auth():
        return {"error": "unauthorised"}, 401
    return engine.remix_queue()


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
    return render_template("yours.html", events=ev, names=names, threads=threads)


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
    draft = at.create("drafts", {"Subject": "Reply from Karlie", "Status": "Sent", "Kind": "Reply",
                                 "To Email": to, "Body": text, "Written By Karlie": True,
                                 "Gmail Thread ID": tid, "Sent At": t, "Decided At": t, **link})
    at.create("log", {"Summary": "Karlie replied from the dashboard", "Event": "Sent", "Direction": "Out",
                      "Email": to, "At": t, "Snippet": text[:500], "Gmail Message ID": mid,
                      "Gmail Thread ID": tid, "Draft": [draft["id"]], "Handled": True, **link})
    at.update("log", rid, {"Handled": True})
    celebrate("Sent!", f"On its way to {to}. Logged as a ping, and it'll learn from how you wrote it.", "💌")
    return redirect(url_for("yours"))


# ---------- rules ----------
NUM_FIELDS = ["Daily Ping Limit", "Send Window Start (Hour)", "Send Window End (Hour)", "Max Follow-ups",
              "Follow-up 1 After (Days)", "Follow-up 2 After (Days)", "Follow-up 3 After (Days)",
              "Days Before Re-pitching A Company", "Rescue Tries Per Company"]
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


# ---------- results ----------
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
    return render_template("results.html", weeks=weeks, peak=peak, totals=totals,
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
    return app.response_class(json.dumps(engine.context(max_new=mx, more=more), default=str), mimetype="application/json")


@app.post("/api/engine/apply")
def engine_apply():
    if not engine_auth():
        return {"error": "unauthorised"}, 401
    with OpsLog("w6_pings_brain", job_name="W6 Pings: brain (drafts + learning)", platform="Render") as ops:
        out = engine.apply(request.get_json(force=True))
        ops.step(json.dumps(out))
        return out


@app.route("/healthz")
def healthz():
    return "ok"


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
                for path in ("/", "/approve", "/yours", "/rules", "/results"):
                    c.get(path)
            except Exception:
                pass
            _t.sleep(30)

    threading.Thread(target=loop, daemon=True).start()


if os.environ.get("RENDER"):
    _keep_warm()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8090)), debug=bool(os.environ.get("DEBUG")))
