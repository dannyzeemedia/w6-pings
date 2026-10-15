"""Karlie's outbound dashboard: daily ping count, approvals, replies to handle, rules, voice, results.
Airtable (W6 Media base) is the database; this app is only a friendlier screen over it."""
import os, json, re, time, datetime as dt
import requests
from functools import wraps
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, redirect as _redirect, url_for, session, flash
from werkzeug.security import check_password_hash
import airtable as at
import gmail
import engine
import bookings as bk
import richtext
import brand
from ops_log import OpsLog

app = Flask(__name__)


@app.before_request
def _partner_pages_only():
    """partners.workspace6.io serves partner pages and nothing else from Pings. Registered first, so it answers
    before any login check, page or API can."""
    import partner_pages
    if partner_pages.is_partner_host(request.host):
        return partner_pages.respond(request.path)


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


@app.after_request
def _no_search_engines(resp):
    # private tool: never in search results, never followed, never cached by search engines
    resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    return resp


@app.get("/robots.txt")
def robots_txt():
    from flask import Response
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")


@app.before_request
def _freshness():
    # pages may show the saved copy while it refreshes; the engine (sending, brain) always reads fresh
    at._local.fresh = request.path.startswith(("/tasks", "/api/engine"))


def _body_from_form(default=""):
    """(plain words, formatted HTML) from an editor form. The plain words are what the learner compares."""
    h = (request.form.get("body_html") or "").strip()
    if h:
        h = richtext.sanitize(h)
        return richtext.html_to_text(h), h
    return (request.form.get("body") or default).strip(), ""


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


def view_tz():
    """The timezone of whoever is looking: their browser reports it (a cookie the page keeps current), so every time on
    screen reads in their own local time, wherever they are. Display only: when things actually send never changes."""
    try:
        from urllib.parse import unquote
        z = unquote(request.cookies.get("tz") or "")
        return ZoneInfo(z) if z else BRIS
    except Exception:
        return BRIS


def tz_name(tz=None):
    """A short name for the viewer's zone, like 'New York' or 'Chicago'."""
    tz = tz or view_tz()
    return tz.key.split("/")[-1].replace("_", " ")


@app.template_filter("when")
def when(s):
    t = parse_ts(s) if isinstance(s, str) else s
    if not t:
        return ""
    tz = view_tz()
    t = t.astimezone(tz)
    today = dt.datetime.now(tz).date()
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


@app.template_filter("nicedate")
def nicedate(s):
    try:
        return dt.date.fromisoformat(str(s)[:10]).strftime("%a %-d %b")
    except ValueError:
        return s


@app.template_filter("datevalue")
def datevalue(s):
    t = parse_ts(s)
    return t.astimezone(view_tz()).strftime("%Y-%m-%d") if t else ""


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
    pa, goal, nudge, wk_n, wk_goal = None, 0, False, 0, 0
    if session.get("user") and request.endpoint not in ("login", "healthz", "static"):
        try:
            sf = at.settings()["fields"]
            pa, goal = actions_today(), int(sf.get("Daily Ping Limit") or 0)
            wk_n, wk_goal = week_progress(sf)
            if dt.datetime.now(BRIS).date().isoformat() not in _goal_history(sf):
                record_goal(goal, sf, at.settings()["id"])  # first visit of the day notes the day's goal
            nudge = session.get("user") == "karlie" and bool(sf.get("Autopilot Nudge Sent")) and not sf.get("Autopilot Nudge Seen")
        except Exception:
            pass
    return {"user": session.get("user"), "zone_label": ZONE_LABEL, "n_to_approve": n, "more_pending": mp, "n_yours": ny,
            "version": VERSION, "pings_today": pa, "goal": goal, "autopilot_nudge": nudge,
            "week_n": wk_n, "week_goal": wk_goal, "week_days_left": max(0, 5 - dt.datetime.now(BRIS).weekday())}


def actions_today():
    """Pings Karlie actioned today (her day in Brisbane): drafts she approved + replies she sent herself."""
    start = dt.datetime.now(BRIS).replace(hour=0, minute=0, second=0, microsecond=0)
    since = iso(start.replace(minute=0))
    approved = at.list_records("drafts", f"AND(IS_AFTER({{Decided At}}, '{since}'), OR({{Status}}='Approved', {{Status}}='Sent'), NOT({{Written By Karlie}}), {{Kind}}!='Reply')", fields=["Decided At"])
    replies = at.list_records("log", f"AND({{Event}}='Sent', {{By}}='Karlie', IS_AFTER({{At}}, '{since}'))", fields=["At"])
    return len(approved) + len(replies)


def progress_numbers():
    """The four numbers the two progress bars show, read fresh.

    A number we could not read comes back as None rather than 0. The page can then say it
    does not know, which is the truth; a zero would be a confident claim that nothing was
    done today. Never raises, so a failed count can never be reported as a failed save.
    """
    out = {"pings_today": None, "goal": None, "week_n": None, "week_goal": None}
    try:
        sf = at.settings()["fields"]
        out["goal"] = int(sf.get("Daily Ping Limit") or 0)
    except Exception:
        return out
    try:
        out["pings_today"] = actions_today()
    except Exception:
        pass
    try:
        out["week_n"], out["week_goal"] = week_progress(sf)
    except Exception:
        pass
    return out


def actions_between(start):
    """Karlie's actions since `start` (a Brisbane datetime): drafts she approved + emails she sent herself."""
    since = iso(start)
    approved = at.list_records("drafts", f"AND(IS_AFTER({{Decided At}}, '{since}'), OR({{Status}}='Approved', {{Status}}='Sent'), NOT({{Written By Karlie}}), {{Kind}}!='Reply')", fields=["Decided At"])
    replies = at.list_records("log", f"AND({{Event}}='Sent', {{By}}='Karlie', IS_AFTER({{At}}, '{since}'))", fields=["At"])
    return len(approved) + len(replies)


def _goal_history(sf):
    try:
        return json.loads(sf.get("Goal History") or "{}")
    except ValueError:
        return {}


def record_goal(new_goal, sf=None, rid=None):
    """Remember the daily goal as it stands now; the last value set on a day is that day's goal."""
    if sf is None:
        rec = at.settings(); sf, rid = rec["fields"], rec["id"]
    h = _goal_history(sf)
    today = dt.datetime.now(BRIS).date().isoformat()
    if h.get(today) != new_goal:
        h[today] = new_goal
        at.update("settings", rid, {"Goal History": json.dumps(dict(sorted(h.items())[-120:]))})


def week_progress(sf):
    """(actions this week, weekly goal). Her week runs Monday to Sunday, Brisbane time. The goal adds up the
    daily goal for Monday to Friday: each past day's goal as it stood at the end of that day, and today's goal
    for today and the days still to come. Weekend actions count toward the week."""
    now = dt.datetime.now(BRIS)
    monday = (now - dt.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    current = int(sf.get("Daily Ping Limit") or 0)
    h = _goal_history(sf)
    goal = 0
    for i in range(5):
        day = (monday + dt.timedelta(days=i)).date()
        if day >= now.date():
            goal += current
        else:
            known = [v for k, v in h.items() if k <= day.isoformat()]
            goal += int(known[-1]) if known else current
    return actions_between(monday), goal


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
        u = (request.form.get("username") or request.form.get("user") or "").strip().lower()
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
    proms = engine.promises()
    pnames = at.partner_names([p for x in proms for p in x["contact"]["fields"].get("Partner", [])])
    return render_template("today.html", backup_stale=_backup_stale(f), s=f, sent_today=sent_today, n_yours=len(yours),
                           upcoming=upcoming, names=names, brands=brands_for(upcoming, "To Email"), handsoff=handsoff, partners=at.all_partners(),
                           promises=proms, pnames=pnames)


@app.post("/promise/<cid>/act")
@login_required
def promise_act(cid):
    """Karlie's call on a loose end: write something (her words go to the AI), remind her later, or drop it."""
    c = at.get("contacts", cid)["fields"]
    how = request.form.get("how", "")
    text = (request.form.get("text") or "").strip()
    today = dt.date.today()
    if how in ("snooze14", "snooze30"):
        at.update("contacts", cid, {"Call Back On": (today + dt.timedelta(days=14 if how == "snooze14" else 30)).isoformat()})
        flash("Got it. It'll come back to you " + ("in two weeks." if how == "snooze14" else "in a month."))
    elif how == "drop":
        at.update("contacts", cid, {"Call Back On": None, "Call Back Loose": False, "Call Back Kept": today.isoformat()})
        flash("Dropped. It won't come back.")
    else:  # write something: her instruction (or a light nudge) goes to the AI, which drafts it into To approve
        pid = (c.get("Partner") or [""])[0]
        ask = (f"[auto {today.isoformat()} {pid}] Write to {c.get('Name') or ''} <{c.get('Email')}> about a loose end from our "
               f"conversation: \"{(c.get('Call Back Why') or '')[:300]}\". Karlie says: {text or 'a light, friendly nudge to pick it back up'}."
               f" Reply in the latest thread with them. Short.")
        s_ = at.settings()
        at.update("settings", s_["id"], {"Ping Requests": ((s_["fields"].get("Ping Requests") or "").rstrip() + "\n" + ask).strip()})
        at.update("contacts", cid, {"Call Back On": (today + dt.timedelta(days=14)).isoformat()})  # back in 2 weeks if still open
        celebrate("On it", f"Writing it now. It'll be in To approve in a few minutes.", "✍️")
    return redirect(url_for("today") + "#promises")


@app.post("/promise/<cid>/done")
@login_required
def promise_done(cid):
    """Karlie: this promise doesn't need an email (sorted on a call, no longer relevant...)."""
    at.update("contacts", cid, {"Call Back On": None, "Call Back Kept": dt.date.today().isoformat()})
    flash("Crossed off. It won't be chased.")
    return redirect(url_for("today") + "#promises")


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
            record_goal(fields["Daily Ping Limit"], s["fields"], s["id"])
        except ValueError:
            pass
    if "paused" in request.form:
        fields["Paused"] = request.form["paused"] == "1"
    if fields:
        at.update("settings", s["id"], fields)
    if request.headers.get("X-Fetch") == "1":  # the Today page's goal box: send back the numbers the bars show
        return dict({"ok": True}, **progress_numbers())
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
    deals = {r["id"]: dict(r["fields"]) for r in at.list_records("sales", "OR(" + ",".join(f"RECORD_ID()='{i}'" for i in sale_ids) + ")",
                                                          fields=["Opportunity", "Status", "Created", "Value"])} if sale_ids else {}
    for v in deals.values():
        v["Opportunity"] = re.sub(r"^[?\s]*//\s*", "", v.get("Opportunity") or "")
        try:
            v["Created"] = dt.date.fromisoformat(v["Created"][:10]).strftime("%-d %b %Y") if v.get("Created") else None
        except ValueError:
            pass
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
    subject = request.form.get("subject", "").strip()
    body, body_html = _body_from_form()
    fields = {"Subject": subject, "Body": body, "Body HTML": body_html, "Karlie Feedback": request.form.get("feedback", "").strip()}
    fields["Edited By Karlie"] = (_norm(body) != _norm(d.get("AI Original Body"))
                                  or _norm(subject) != _norm(d.get("AI Original Subject")))
    if action == "save" and request.headers.get("X-Fetch") == "1":  # quiet background save while she types
        at.update("drafts", rid, fields)
        return {"ok": True}
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
            return dict({"ok": True, "eta": eta, "left": max(left, 0), "subject": subject,
                         "eta_short": _short_eta(eta_t)}, **progress_numbers())
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
    """(plain-English 'when will this go' in the viewer's local time and theirs, UTC datetime or None)."""
    try:
        w = engine.World()  # settings only; tables load lazily and we don't touch them here
        cid = (d.get("Contact") or [None])[0]
        contact = at.get("contacts", cid) if cid else None
        t = engine.next_send_time(w, contact, hot=engine.is_priority(w, d))
        if not t:
            return "It'll go out once there's a send day and time set on the Rules page.", None
        tz = w.tz_for(contact)
        fmt = lambda x, f: x.strftime(f).replace("AM", "am").replace("PM", "pm")
        who = (d.get("To Name") or "them").split()[0]
        place = tz.key.split("/")[-1].replace("_", " ")
        line = (f"Goes out around {fmt(t.astimezone(view_tz()), '%a %-d %b, %-I:%M%p')} your time "
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
    return "goes out ~" + t.astimezone(view_tz()).strftime("%a %-I:%M%p").replace("AM", "am").replace("PM", "pm")


@app.post("/approve/<rid>/undo")
@login_required
def approve_undo(rid):
    d = at.get("drafts", rid)["fields"]
    ok = d.get("Status") == "Approved"
    if ok:
        at.update("drafts", rid, {"Status": "Pending Approval", "Decided At": None, "Expected Send": None})
    if request.headers.get("X-Fetch") == "1":  # Catch-up's Undo
        return {"ok": ok, **progress_numbers()}
    flash("Pulled back. It's in To approve again." if ok else "Too late, that one has already gone.")
    return redirect(url_for("approve"))


# ---------- inbound: the sponsor inquiry form (sponsors.workspace6.io) ----------
INQUIRY_PREFIX = "inquiry:"


def _inquiry_text(d):
    chans = ", ".join(c if c != "Other" or not d.get("other_channel") else f"Other ({d['other_channel']})" for c in d.get("channels") or [])
    old = []
    if d.get("source") == "google_form":  # the old Google Form: nobody saw these at the time
        when = (d.get("created_at") or "")[:10]
        old = [f"Sent through our OLD Google Form on {when}. Nobody replied: the form wasn't connected to anything, so it never "
               "reached us. Now fixed. This reply is the first they hear from us.", ""]
    if d.get("history"):
        old += [f"What we already know about them: {d['history']}", ""]
    return "\n".join(old + [
        f"Company: {d.get('company')} ({d.get('company_url') or 'no website'})",
        f"From: {d.get('first_name', '')} {d.get('last_name', '')} <{d.get('email')}>".replace("  ", " "),
        f"Budget: {d.get('budget') or '-'}",
        f"Timing: {d.get('timing') or '-'}",
        *([f"Also from the same company: {d['also']}"] if d.get("also") else []),
        f"Channels: {chans or '-'}",
        f"Creator referrals: {'Yes' if d.get('referrals') else 'No'}",
        "",
        "What they want to advertise:",
        (d.get("advertising") or "").strip(),
    ])


KARLIE_SLACK_ID, W6_TEAM_CHANNEL = "U09JHPUV6N8", "C09J30RP80K"


def _slack_inquiry(d):
    """Tell #w6-team (as Clara, tagging Karlie) that an inquiry came in. Our own test inquiries (zee.media
    addresses) post there too, marked as a test and without tagging Karlie."""
    tok = os.environ.get("SLACK_BOT_TOKEN")
    if not tok:
        return
    esc = lambda v: str(v or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    email = (d.get("email") or "").strip().lower()
    test = email.endswith("@zee.media")
    chans = ", ".join(c if c != "Other" or not d.get("other_channel") else f"Other ({d['other_channel']})" for c in d.get("channels") or [])
    ad = (d.get("advertising") or "").strip()
    ad = ad[:500].rstrip() + "…" if len(ad) > 500 else ad
    url = (d.get("company_url") or "").replace("|", "%7C")
    company = f"*<{esc(url)}|{esc(d.get('company'))}>*" if url else f"*{esc(d.get('company'))}*"
    name = f"{d.get('first_name') or ''} {d.get('last_name') or ''}".strip()
    text = "\n".join([
        ("🧪 Test sponsor inquiry (from the team, not a real sponsor)" if test else f"New sponsor inquiry <@{KARLIE_SLACK_ID}>"),
        company,
        f"{esc(name)} · {esc(email)}",
        f"{esc(d.get('budget'))} · {esc(d.get('timing'))}",
        f"Channels: {esc(chans)}",
        ">" + esc(ad).replace("\n", "\n>"),
        f"Creator referrals: {'Yes' if d.get('referrals') else 'No'}",
        "A suggested reply will be ready in Your turn in a couple of minutes: https://pings.workspace6.io/yours",
    ])
    channel = W6_TEAM_CHANNEL
    r = requests.post("https://slack.com/api/chat.postMessage", headers={"Authorization": f"Bearer {tok}"}, timeout=15,
                      json={"channel": channel, "text": text, "unfurl_links": False}).json()
    if not r.get("ok"):
        app.logger.warning("inquiry slack post failed: %s", r.get("error"))
    return r


def _inquiry_subject(f):
    snip = f.get("Snippet") or ""
    company = snip.split("Company: ", 1)[-1].split(" (", 1)[0] if "Company: " in snip else ""
    return f"{company} x Workspace6" if company else "Your Workspace6 sponsorship inquiry"


@app.post("/api/inbound/sponsor")
def inbound_sponsor():
    """A sponsor filled in the inquiry form. It lands in Karlie's Your turn with the company and contact set up,
    and the AI writes a suggested reply within a couple of minutes (laptop job, on Danny's subscription)."""
    if not hmac.compare_digest(request.headers.get("X-Inbound-Token", ""), os.environ.get("INBOUND_TOKEN", "") or "\0"):
        return {"error": "unauthorised"}, 401
    d = request.get_json(silent=True) or {}
    email = (d.get("email") or "").strip().lower()
    company = (d.get("company") or "").strip()
    if not email or "@" not in email or not company:
        return {"error": "email and company are required"}, 400
    msg_id = f"{INQUIRY_PREFIX}{email}:{d.get('created_at') or ''}"
    if msg_id in _inquiries_seen or at.list_records("log", f"{{Gmail Message ID}}='{msg_id.replace(chr(39), '')}'", fields=["Event"], max_records=1):
        return {"ok": True, "duplicate": True}
    _inquiries_seen.add(msg_id)
    # Setting it up takes ~10s (it reads the whole pipeline), so answer the form now and do it in the background.
    _threading.Thread(target=_file_inquiry, args=(d, email, company, msg_id), daemon=True).start()
    return {"ok": True}


_inquiries_seen = set()


def _file_inquiry(d, email, company, msg_id):
    try:
        _file_inquiry_now(d, email, company, msg_id)
    except Exception as ex:
        _inquiries_seen.discard(msg_id)
        app.logger.exception("filing inquiry %s failed: %s", msg_id, ex)


def _file_inquiry_now(d, email, company, msg_id):
    import brand
    w = engine.World()
    dom = brand.domain_from(d.get("company_url"), email)
    pid = (w.dom2partner.get(dom) if dom else None) or next(
        (i for i, p_ in w.partners.items() if p_["fields"].get("Name", "").strip().lower() == company.lower()), None)
    today = dt.date.today().isoformat()
    if not pid:
        pid = at.create("partners", {"Name": company, "Stage": "Engaged", **({"Website": f"https://{dom}"} if dom else {}),
                                     "Notes": f"🤖 {today}: came in through the sponsor inquiry form."}, typecast=True)["id"]
    else:
        pf = w.partners[pid]["fields"]
        at.update("partners", pid, {"Notes": ((pf.get("Notes") or "").rstrip() + f"\n\n🤖 {today}: sent an inquiry through the sponsor form.").strip()})
    name = f"{(d.get('first_name') or '').strip()} {(d.get('last_name') or '').strip()}".strip()
    c = engine._existing_contact(w, email)
    if c:
        at.update("contacts", c["id"], {"Status": "Active", **({"Name": name} if name and not c["fields"].get("Name") else {})})
    else:
        c = at.create("contacts", {"Email": email, "Name": name or None, "Status": "Active", "Source": "Inquiry Form", "Partner": [pid]}, typecast=True)
    text = _inquiry_text(d)
    engine.log("Handed To Karlie", "In", "Them", email, partner=pid, contact=c["id"],
               summary="New inquiry from the sponsor form. Summary coming shortly.", snippet=text, msg_id=msg_id,
               handled=False, reviewed=False)
    try:
        engine._cancel_pending(w, pid, email, "They sent an inquiry through the sponsor form, so it's with Karlie now.")
    except Exception:
        pass
    for x in d.get("extra_contacts") or []:  # a colleague who filled in the form too
        x = (x or "").strip().lower()
        if "@" in x and not engine._existing_contact(w, x):
            at.create("contacts", {"Email": x, "Status": "Active", "Source": "Inquiry Form", "Partner": [pid]}, typecast=True)
    if d.get("quiet"):  # a backfill of old submissions: no Slack post for each one
        return
    try:
        _slack_inquiry(d)
    except Exception as ex:
        app.logger.warning("inquiry slack post failed: %s", ex)


# ---------- sponsor onboarding (sponsors who bought placements send us their assets) ----------
import onboarding as ob


def _sponsor_name(rec):
    pid = (rec["fields"].get("Partner") or [None])[0]
    return (at.partner_names([pid]).get(pid) if pid else "") or rec["fields"].get("Name", "").split(" \u00b7 ")[0] or "your brand"


@app.get("/onboard/<token>")
def onboard_page(token):
    if os.environ.get("ONBOARD_ON_PINGS") != "1":  # sponsors use the Lovable page; this one only stays as a fallback
        return redirect(ob.url_for_token(token), 302)
    rec = ob.by_token(token)
    if not rec:
        return render_template("onboard.html", gone=True), 404
    rec = ob.fresh(rec["id"])  # never show a sponsor a saved-a-while-ago copy of their own answers
    order = ob.order_of(rec)
    answers = ob.answers_of(rec)
    if not (answers.get("contact") or {}).get("email") and rec["fields"].get("Contact Email"):
        answers.setdefault("contact", {})["email"] = rec["fields"]["Contact Email"]
    if not (answers.get("contact") or {}).get("name") and rec["fields"].get("Contact Name"):
        answers.setdefault("contact", {})["name"] = rec["fields"]["Contact Name"]
    return render_template("onboard.html", gone=False, token=token, sponsor=_sponsor_name(rec), order=order,
                           answers=answers, files=ob.files_of(rec), questions=ob.QUESTIONS, contact_qs=ob.CONTACT,
                           kinds=[k for k in ob.KINDS if any(it["kind"] == k for it in order.get("items", []))],
                           label=ob.LABEL, item_title=ob.item_title, summary=ob.summary(order),
                           done=rec["fields"].get("Status") == "Complete")


def _onboard_rec(token):
    rec = ob.by_token(token)
    if not rec:
        return None, ({"error": "This link isn't valid any more. Ask Karlie for a fresh one."}, 404)
    return ob.fresh(rec["id"]), None


@app.post("/onboard/<token>/save")
def onboard_save(token):
    rec, err = _onboard_rec(token)
    if err:
        return err
    ob.save_answers(rec, request.get_json(silent=True) or {})
    return {"ok": True}


@app.post("/onboard/<token>/upload")
def onboard_upload(token):
    rec, err = _onboard_rec(token)
    if err:
        return err
    key = request.form.get("key", "")
    valid = {f"{it['key']}/{q['key']}" for it in ob.order_of(rec).get("items", []) for q in ob.QUESTIONS[it["kind"]] if q["type"] == "file"}
    if key not in valid:
        return {"error": "Unknown upload"}, 400
    errors = []
    for up in request.files.getlist("files"):
        data = up.read()
        if data:
            e = ob.upload(rec, key, up.filename, up.mimetype, data)
            if e:
                errors.append(e)
    rec = ob.fresh(rec["id"])
    if rec["fields"].get("Status") == "Sent":
        at.update("onboard", rec["id"], {"Status": "Started", "Started At": iso(now_utc())})
    return {"files": ob.files_of(rec).get(key, []), "errors": errors}


@app.post("/onboard/<token>/remove")
def onboard_remove(token):
    rec, err = _onboard_rec(token)
    if err:
        return err
    d = request.get_json(silent=True) or {}
    ob.remove_file(rec, d.get("id", ""))
    return {"files": ob.files_of(ob.fresh(rec["id"])).get(d.get("key", ""), [])}


@app.post("/onboard/<token>/submit")
def onboard_submit(token):
    rec, err = _onboard_rec(token)
    if err:
        return err
    if request.is_json and request.get_json(silent=True):
        ob.save_answers(rec, request.get_json(silent=True))
        rec = ob.fresh(rec["id"])
    gaps = ob.missing(rec)
    if gaps:
        return {"ok": False, "missing": gaps}
    if rec["fields"].get("Status") != "Complete":
        ob.complete(rec, _sponsor_name(rec))
    return {"ok": True}


# --- the same, as a JSON API for the Lovable page (sponsors.workspace6.io/onboarding/<token>) ---
def _api_auth():
    return hmac.compare_digest(request.headers.get("X-Inbound-Token", ""), os.environ.get("INBOUND_TOKEN", "") or "\0")


def _api_payload(rec):
    order, answers = ob.order_of(rec), ob.answers_of(rec)
    c = answers.setdefault("contact", {})
    if not c.get("email") and rec["fields"].get("Contact Email"):
        c["email"] = rec["fields"]["Contact Email"]
    if not c.get("name") and rec["fields"].get("Contact Name"):
        c["name"] = rec["fields"]["Contact Name"]
    if not c.get("company"):
        c["company"] = _sponsor_name(rec)
    items = [dict(it, title=ob.item_title(it)) for it in order.get("items", [])]
    return {"sponsor": _sponsor_name(rec), "summary": ob.summary(order), "items": items,
            "kinds": [k for k in ob.KINDS if any(it["kind"] == k for it in items)], "labels": ob.LABEL,
            "questions": ob.QUESTIONS, "contact_questions": ob.CONTACT, "answers": answers,
            "files": ob.files_of(rec), "done": rec["fields"].get("Status") == "Complete"}


@app.route("/api/onboard/<token>", methods=["GET", "POST"])
def api_onboard(token):
    if not _api_auth():
        return {"error": "unauthorised"}, 401
    rec, err = _onboard_rec(token)
    if err:
        return err
    return _api_payload(rec)


@app.post("/api/onboard/<token>/save")
def api_onboard_save(token):
    if not _api_auth():
        return {"error": "unauthorised"}, 401
    rec, err = _onboard_rec(token)
    if err:
        return err
    ob.save_answers(rec, (request.get_json(silent=True) or {}).get("answers") or {})
    return {"ok": True}


@app.post("/api/onboard/<token>/upload")
def api_onboard_upload(token):
    """One file, base64 in JSON (the Lovable server passes it through)."""
    if not _api_auth():
        return {"error": "unauthorised"}, 401
    rec, err = _onboard_rec(token)
    if err:
        return err
    import base64 as _b64
    d = request.get_json(silent=True) or {}
    key = d.get("key", "")
    valid = {f"{it['key']}/{q['key']}" for it in ob.order_of(rec).get("items", []) for q in ob.QUESTIONS[it["kind"]] if q["type"] == "file"}
    if key not in valid:
        return {"error": "Unknown upload"}, 400
    try:
        data = _b64.b64decode(d.get("data") or "")
    except Exception:
        return {"error": "That file didn't come through. Try again?"}, 400
    e = ob.upload(rec, key, d.get("filename") or "file", d.get("contentType") or "", data) if data else "Empty file"
    rec = ob.fresh(rec["id"])
    if rec["fields"].get("Status") == "Sent":
        at.update("onboard", rec["id"], {"Status": "Started", "Started At": iso(now_utc())})
    return {"files": ob.files_of(rec).get(key, []), "error": e}


@app.post("/api/onboard/<token>/remove")
def api_onboard_remove(token):
    if not _api_auth():
        return {"error": "unauthorised"}, 401
    rec, err = _onboard_rec(token)
    if err:
        return err
    d = request.get_json(silent=True) or {}
    ob.remove_file(rec, d.get("id", ""))
    return {"files": ob.files_of(ob.fresh(rec["id"])).get(d.get("key", ""), [])}


@app.post("/api/onboard/<token>/details")
def api_onboard_details(token):
    """Page one's "Send to Workspace6" button: their details go to Karlie now, assets come after."""
    if not _api_auth():
        return {"error": "unauthorised"}, 401
    rec, err = _onboard_rec(token)
    if err:
        return err
    miss = ob.send_details(rec, _sponsor_name(rec), (request.get_json(silent=True) or {}).get("answers") or {})
    return {"ok": not miss, "missing": miss}


@app.post("/api/onboard/<token>/submit")
def api_onboard_submit(token):
    if not _api_auth():
        return {"error": "unauthorised"}, 401
    rec, err = _onboard_rec(token)
    if err:
        return err
    a = (request.get_json(silent=True) or {}).get("answers")
    if a:
        ob.save_answers(rec, a)
        rec = ob.fresh(rec["id"])
    gaps = ob.missing(rec)
    if gaps:
        return {"ok": False, "missing": gaps}
    if rec["fields"].get("Status") != "Complete":
        ob.complete(rec, _sponsor_name(rec))
    return {"ok": True}


@app.get("/onboarding")
@login_required
def onboarding_list():
    rows = sorted(ob.all_links(), key=lambda r: r.get("createdTime", ""), reverse=True)
    # logos without their website link: each row is already a link to the sponsor's page
    brands = {k: (dict(v, website=None) if v else v) for k, v in brands_for(rows, "Contact Email").items()}
    return render_template("onboarding_list.html", rows=rows, url_for_token=ob.url_for_token, brands=brands)


@app.get("/onboarding/new")
@login_required
def onboarding_new():
    pid = request.args.get("partner")
    if not pid and request.args.get("from"):
        pid = (at.get("promo", request.args["from"])["fields"].get("Sponsor") or [None])[0]
    if not pid:
        flash("That booking isn't linked to a sponsor yet, so there's nothing to build a link from.")
        return redirect(url_for("bookings_page"))
    sponsor = at.partner_names([pid]).get(pid, "")
    return render_template("onboarding_new.html", pid=pid, sponsor=sponsor, upcoming=ob.upcoming_for(pid),
                           label=ob.LABEL, types=bk.TYPES, contacts=ob.contacts_for(pid))


@app.post("/onboarding/create")
@login_required
def onboarding_create():
    fm = request.form
    pid = fm.get("partner", "")
    chosen = set(fm.getlist("booking"))
    picked = [b for b in ob.upcoming_for(pid) if b["id"] in chosen]
    if not picked:
        flash("Tick at least one placement for the link to ask about.")
        return redirect(url_for("onboarding_new", partner=pid))
    rec = ob.create_link(pid, at.partner_names([pid]).get(pid, "Sponsor"), picked,
                         fm.get("contact_name", "").strip(), fm.get("contact_email", "").strip().lower(),
                         session.get("user", ""))
    flash("Link made. Copy it into your email.")
    return redirect(url_for("onboarding_view", rid=rec["id"]))


@app.post("/onboarding/<rid>/delete")
@login_required
def onboarding_delete(rid):
    """Delete a sponsor's onboarding link: the link stops working and their answers and files go with it."""
    if re.match(r"rec\w+$", rid):
        name = (at.get("onboard", rid)["fields"].get("Name") or "That link")
        at.delete("onboard", rid)
        flash(f"Deleted {name}. That link no longer works.")
    return redirect(url_for("onboarding_list"))


@app.get("/onboarding/<rid>")
@login_required
def onboarding_view(rid):
    if not re.match(r"rec\w+$", rid):
        return redirect(url_for("onboarding_list"))
    rec = ob.fresh(rid)
    order = ob.order_of(rec)
    return render_template("onboarding_view.html", rec=rec, f=rec["fields"], sponsor=_sponsor_name(rec), order=order,
                           answers=ob.answers_of(rec), files=ob.files_of(rec), questions=ob.QUESTIONS,
                           item_title=ob.item_title, link=ob.url_for_token(rec["fields"].get("Token", "")),
                           summary=ob.summary(order), covered=lambda it: ob._covered(order, ob.answers_of(rec), it),
                           shown=ob._shown, clean=richtext.sanitize, brand=brands_for([rec], "Contact Email").get(rec["id"]))


# ---------- notices: pop-ups for things that arrive while she works (onboarding details, finished assets) ----------
NOTICE_EVENTS = ("Details Received", "Assets Received", "Paid", "Overdue")


@app.get("/api/notices")
@login_required
def notices():
    me = session.get("user") or ""
    since = (now_utc() - dt.timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = at.list_records("log", f"AND(OR({','.join(f'{{Event}}={chr(39)}{e}{chr(39)}' for e in NOTICE_EVENTS)}), IS_AFTER({{At}}, '{since}'))",
                           sort=[("At", "desc")], fields=["Event", "Summary", "Link", "At", "Seen By"], max_records=20)
    out = [{"id": r["id"], "event": r["fields"].get("Event"), "text": r["fields"].get("Summary"), "link": r["fields"].get("Link"),
            "when": when(r["fields"].get("At"))} for r in rows if me not in (r["fields"].get("Seen By") or "").split("\n")]
    return {"notices": out}


@app.post("/api/notices/<rid>/seen")
@login_required
def notice_seen(rid):
    if not re.match(r"rec\w+$", rid):
        return {"ok": False}, 400
    me = session.get("user") or ""
    seen = (at.get("log", rid)["fields"].get("Seen By") or "").split("\n")
    if me not in seen:
        at.update("log", rid, {"Seen By": "\n".join([x for x in seen if x] + [me])})
    return {"ok": True}


# ---------- "Publish to W6" connector (MCP) for the team's Claude: pages go live at partners.workspace6.io/<slug> ----------
@app.route("/mcp/pages/<token>", methods=["GET", "POST", "DELETE"])
def pages_connector(token):
    import pages_mcp
    if request.method != "POST":
        return "", 405, {"Allow": "POST"}
    msg = request.get_json(silent=True)
    if isinstance(msg, list):
        out = [r for r in (pages_mcp.handle(token, m) for m in msg) if r is not None]
        return (json.dumps(out), 200, {"Content-Type": "application/json"}) if out else ("", 202)
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, 400
    out = pages_mcp.handle(token, msg)
    return (out, 200) if out is not None else ("", 202)


# ---------- invoices (W6 Stripe): draft from the booking, preview, plain-English changes, send, paid = converted ----------
import invoicing


def _invoice_context(sid):
    """The deal, its sponsor, and the details they gave on their onboarding page (for the first draft)."""
    f = invoicing.sale(sid)["fields"]
    pid = (f.get("Partner") or [None])[0]
    name = at.partner_names([pid]).get(pid) if pid else None
    email, company = f.get("Invoice Email"), None
    for r in at.list_records("onboard", fields=["Partner", "Answers"]):  # what they told us on page one
        if pid and pid in (r["fields"].get("Partner") or []):
            c = (json.loads(r["fields"].get("Answers") or "{}").get("contact") or {})
            email = email or invoicing.email_from(c.get("invoices")) or (c.get("email") or "").lower() or None
            company = c.get("company") or company
    return f, pid, name, email, company


def _sale_for_partner(pid, name):
    """The deal's Sales row: the one their booked dates link to, else their open one, else a new one."""
    linked = next((b["sale"] for b in bk.current_for(pid) if b.get("sale")), None) if pid else None
    if linked:
        return linked
    sid = engine.booking_sale(pid, name, None)
    for b in bk.current_for(pid):  # link their dates to it, so the invoice knows what was booked
        at.update("promo", b["id"], {"Sale": [sid]})
    return sid


@app.get("/invoice/for/<pid>")
@login_required
def invoice_for(pid):
    """From the onboarding page or Results: open (or start) this sponsor's invoice."""
    if not re.match(r"rec\w+$", pid):
        return redirect(url_for("results"))
    return redirect(url_for("invoice_page", sid=_sale_for_partner(pid, at.partner_names([pid]).get(pid) or "Sponsor")))


@app.get("/invoice/<sid>")
@login_required
def invoice_page(sid):
    if not re.match(r"rec\w+$", sid):
        return redirect(url_for("results"))
    try:  # in case Stripe's paid message didn't arrive
        invoicing.check_payments()
    except Exception:
        pass
    f, pid, name, email, company = _invoice_context(sid)
    try:
        v = invoicing.view(sid)
    except Exception as ex:
        v, err = None, str(ex)
    else:
        err = None
    brand = brands_for([{"id": sid, "fields": {"Partner": f.get("Partner") or []}}], "x").get(sid)
    checks = None
    if v and v["status"] == "draft":
        try:
            checks = invoicing.preflight(sid, v)
        except Exception as ex:
            checks = [(False, f"The final check couldn't run: {ex}")]
    heads_up = None
    if v and v["status"] != "draft":  # the "invoice is on its way" email Pings wrote when it was sent
        opp = (f.get("Opportunity") or "").replace("'", "\\'")
        for d in at.list_records("drafts", f"AND({{Kind}}='Follow-up', FIND('{opp}', ARRAYJOIN({{Sale}})))",
                                 fields=["Status", "Sale", "Sent At", "Expected Send"], sort=[("Status", "asc")]):
            if sid in (d["fields"].get("Sale") or []):
                heads_up = {"id": d["id"], **d["fields"]}
    fmt_long = lambda d: dt.date.fromisoformat(d).strftime("%B %-d, %Y") if d else ""
    return render_template("invoice.html", sid=sid, f=f, name=name or f.get("Opportunity"), email=email, company=company or name,
                           v=v, err=err, brand=brand, pending=bool((f.get("Invoice Request") or "").strip()), checks=checks, fmt_long=fmt_long,
                           heads_up=heads_up, due=invoicing.due_label(f.get("Status"), (v or {}).get("due_date") or f.get("Due Date")))


@app.post("/invoice/<sid>/prepare")
@login_required
def invoice_prepare(sid):
    try:
        invoicing.prepare(sid, email=request.form.get("email"), company=request.form.get("company"))
    except Exception as ex:
        flash(f"Couldn't make the draft: {ex}")
    return redirect(url_for("invoice_page", sid=sid))


@app.post("/invoice/<sid>/change")
@login_required
def invoice_change(sid):
    ask = (request.form.get("request") or "").strip()
    if ask:
        at.update("sales", sid, {"Invoice Request": ask, "Invoice Note": ""})
    return redirect(url_for("invoice_page", sid=sid))


@app.get("/invoice/<sid>/status")
@login_required
def invoice_status(sid):
    f = invoicing.sale(sid)["fields"]
    return {"pending": bool((f.get("Invoice Request") or "").strip()), "note": f.get("Invoice Note")}


_dns_cache = {"at": 0, "rows": None}


def _domain_checks(domain="workspace6.io"):
    """Is the sending domain set up so inboxes trust it? Read live from public DNS, at most once an hour."""
    if _dns_cache["rows"] is not None and time.time() - _dns_cache["at"] < 3600:
        return _dns_cache["rows"]
    def txt(name):
        try:
            j = requests.get("https://dns.google/resolve", params={"name": name, "type": "TXT"}, timeout=8).json()
            return [a["data"].strip('"').replace('" "', "") for a in j.get("Answer", [])]
        except Exception:
            return None
    spf, dkim, dmarc = txt(domain), txt(f"google._domainkey.{domain}"), txt(f"_dmarc.{domain}")
    spf_ok = spf is not None and any(t.startswith("v=spf1") and "_spf.google.com" in t for t in spf)
    dkim_ok = dkim is not None and any("v=DKIM1" in t for t in dkim)
    dm = next((t for t in dmarc or [] if t.startswith("v=DMARC1")), "")
    policy = (re.search(r"p=(\w+)", dm) or [None, None])[1] if dm else None
    rows = [("ok" if spf_ok else "bad", "SPF", "Google is allowed to send for workspace6.io." if spf_ok else "Missing or doesn't include Google."),
            ("ok" if dkim_ok else "bad", "DKIM", "Emails carry Google's signature, so inboxes can tell they're really from us." if dkim_ok
             else "No signature found. Inboxes can't confirm emails are really from us."),
            (("ok" if policy in ("quarantine", "reject") else "warn") if dm else "bad", "DMARC",
             (f"Set up, policy \"{policy}\"." + (" Fine for now. Tightening it later protects the name from spoofers." if policy == "none" else ""))
             if dm else "Missing. Big inboxes increasingly expect it.")]
    if spf is None and dkim is None:
        return _dns_cache["rows"] or []
    _dns_cache.update(at=time.time(), rows=rows)
    return rows


@app.get("/deliverability")
@login_required
def deliverability():
    et = ZoneInfo("America/New_York")
    now_et = dt.datetime.now(et)
    first = (now_et - dt.timedelta(days=now_et.weekday() + 7 * 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    since = min(first, dt.datetime.now(dt.timezone.utc).astimezone(et) - dt.timedelta(days=60)).strftime("%Y-%m-%d")
    ev = at.list_records("log", f"AND(IS_AFTER({{At}}, '{since}'), OR({{Event}}='Sent', {{Event}}='Bounced', {{Event}}='Replied', "
                                f"{{Event}}='Handed To Karlie', {{Event}}='Auto-Reply', {{Event}}='Left Company'))",
                         fields=["Event", "By", "At", "Email", "Summary"])
    at_of = lambda e: parse_ts(e["fields"].get("At") or "1970-01-01T00:00:00Z")
    email = lambda e: (e["fields"].get("Email") or "").lower()

    def stats(rows):
        sent = [e for e in rows if e["fields"].get("Event") == "Sent"]
        pitched = {email(e) for e in sent if e["fields"].get("By") == "System"} - {""}
        bounced = sum(e["fields"].get("Event") == "Bounced" for e in rows)
        replied = {email(e) for e in rows if e["fields"].get("Event") in ("Replied", "Handed To Karlie")} - {""}
        return {"sent": len(sent), "bounced": bounced, "bounce": round(100 * bounced / len(sent), 1) if sent else None,
                "replied": len(replied), "reply": round(100 * len(replied & pitched) / len(pitched)) if pitched else None,
                "gone": bounced + sum(e["fields"].get("Event") == "Left Company" for e in rows),
                "auto": sum(e["fields"].get("Event") == "Auto-Reply" for e in rows)}
    weeks = []
    for w in range(7, -1, -1):
        start = first + dt.timedelta(days=7 * (7 - w))
        st = stats([e for e in ev if start <= at_of(e).astimezone(et) < start + dt.timedelta(days=7)])
        weeks.append(dict(st, label=start.strftime("%-d %b")))
    while len(weeks) > 4 and not weeks[0]["sent"]:
        weeks.pop(0)  # before Pings started sending
    cut = now_utc() - dt.timedelta(days=30)
    now30 = stats([e for e in ev if at_of(e) > cut])
    prev30 = stats([e for e in ev if cut - dt.timedelta(days=30) < at_of(e) <= cut])
    b = now30["bounce"]
    health = ("good" if b is None or b < 2 else "watch" if b < 5 else "risk")
    bounces = [e["fields"] for e in sorted([e for e in ev if e["fields"].get("Event") in ("Bounced", "Left Company")], key=at_of, reverse=True)][:12]
    peak = max([w["sent"] for w in weeks] + [1])
    bpeak = max([w["bounce"] or 0 for w in weeks] + [5])
    return render_template("deliverability.html", weeks=weeks, now=now30, prev=prev30, health=health, bounces=bounces,
                           checks=_domain_checks(), peak=peak, bpeak=bpeak)


@app.get("/invoices")
@login_required
def invoices_page():
    try:  # in case Stripe's paid message didn't arrive
        invoicing.check_payments()
    except Exception:
        pass
    rows = at.list_records("sales", "{Stripe Invoice ID}!=''", fields=["Opportunity", "Partner", "Status", "Value", "Due Date",
                                                                       "Invoiced At", "Paid At", "Invoice #"])
    names = at.partner_names([p for r in rows for p in r["fields"].get("Partner", [])])
    out = []
    for r in rows:
        f = r["fields"]
        st = f.get("Status")
        stage = {"Paid": "paid", "Overdue": "overdue", "Invoiced": "invoiced"}.get(st, "draft")
        pid = (f.get("Partner") or [None])[0]
        out.append({"id": r["id"], "name": names.get(pid) or re.sub(r"^.*//\s*", "", f.get("Opportunity") or ""), "stage": stage,
                    "value": int(f.get("Value") or 0), "number": f.get("Invoice #"), "sent": f.get("Invoiced At"), "paid": f.get("Paid At"),
                    "due": invoicing.due_label(st, f.get("Due Date")) if stage != "draft" else None, "due_date": f.get("Due Date") or "",
                    "fields": {"Partner": f.get("Partner") or []}})
    order = {"overdue": 0, "invoiced": 1, "draft": 2, "paid": 3}
    out.sort(key=lambda x: (order[x["stage"]], x["due_date"]))
    paid = sorted([x for x in out if x["stage"] == "paid"], key=lambda x: x["paid"] or "", reverse=True)
    out = [x for x in out if x["stage"] != "paid"] + paid
    totals = {k: (sum(x["value"] for x in out if x["stage"] == k), sum(1 for x in out if x["stage"] == k))
              for k in ("invoiced", "overdue", "paid", "draft")}
    brands = {k: (dict(v, website=None) if v else v) for k, v in brands_for(out, "x").items()}
    return render_template("invoices.html", rows=out, totals=totals, brands=brands)


@app.get("/invoice/<sid>/live")
@login_required
def invoice_live(sid):
    if not re.match(r"rec\w+$", sid):
        return {}, 400
    lab = invoicing.live(sid)
    return {"state": lab[0], "text": lab[1]} if lab else {}


@app.post("/invoice/<sid>/send")
@login_required
def invoice_send(sid):
    try:
        inv = invoicing.send(sid)
        celebrate("Invoice sent! 🧾", f"${inv['total'] / 100:,.0f} is on its way to {invoicing.sale(sid)['fields'].get('Invoice Email')}. "
                                      "A heads-up email with the link is waiting in To approve. It turns Paid by itself when they pay.", "💸", big=True)
    except Exception as ex:
        flash(f"That didn't send: {ex}. Nothing went out.")
    return redirect(url_for("invoice_page", sid=sid))


@app.post("/invoice/<sid>/paid")
@login_required
def invoice_paid(sid):
    """For a payment that came in outside Stripe (bank transfer, PayPal): mark it paid by hand."""
    amt = re.sub(r"[^\d.]", "", request.form.get("amount") or "")
    if invoicing.mark_paid(sid, float(amt) if amt else None, how="hand"):
        celebrate("Paid! 🤑", "Marked as paid. It now counts as converted.", "💸", big=True)
    return redirect(url_for("invoice_page", sid=sid))


@app.post("/api/stripe/webhook")
def stripe_webhook():
    """Stripe tells us an invoice was paid: flip the deal to Paid straight away."""
    import hmac as _h, hashlib as _hl, time as _t
    secret, sig, body = os.environ.get("W6_STRIPE_WEBHOOK_SECRET", ""), request.headers.get("Stripe-Signature", ""), request.get_data()
    parts = dict(x.split("=", 1) for x in sig.split(",") if "=" in x)
    good = secret and "t" in parts and any(
        _h.compare_digest(v, _h.new(secret.encode(), f"{parts['t']}.".encode() + body, _hl.sha256).hexdigest())
        for k, v in (x.split("=", 1) for x in sig.split(",") if x.startswith("v1=")))
    if not good or abs(_t.time() - int(parts.get("t", 0))) > 600:
        return {"error": "bad signature"}, 400
    ev = request.get_json(silent=True) or {}
    obj = (ev.get("data") or {}).get("object") or {}
    if ev.get("type") in ("invoice.paid", "invoice.payment_succeeded") and (obj.get("metadata") or {}).get("pings_sale"):
        invoicing.mark_paid(obj["metadata"]["pings_sale"], (obj.get("amount_paid") or obj.get("total") or 0) / 100)
    return {"ok": True}


@app.get("/api/engine/invoices")
def engine_invoices():
    """Change requests waiting for the AI (the laptop job), with the draft as it stands."""
    if not engine_auth():
        return {"error": "unauthorised"}, 401
    out = []
    for r in at.list_records("sales", "AND({Invoice Request}!='', {Stripe Invoice ID}!='')", fields=["Invoice Request", "Partner"]):
        try:
            out.append({"sale_id": r["id"], "request": r["fields"]["Invoice Request"], "invoice": invoicing.view(r["id"])})
        except Exception as ex:
            out.append({"sale_id": r["id"], "request": r["fields"]["Invoice Request"], "error": str(ex)})
    today = dt.datetime.now(ZoneInfo("America/New_York")).date()
    return {"items": out, "today_us_eastern": today.isoformat(), "weekday": today.strftime("%A"),
            "offer_rules": at.settings()["fields"].get("Offer Rules")}


@app.post("/api/engine/invoices")
def engine_invoices_apply():
    if not engine_auth():
        return {"error": "unauthorised"}, 401
    results = []
    for it in (request.get_json(force=True) or {}).get("items", []):
        try:
            invoicing.apply(it["sale_id"], it.get("ops") or {}, it.get("note") or "")
            results.append({"sale_id": it["sale_id"], "ok": True})
        except Exception as ex:
            at.update("sales", it["sale_id"], {"Invoice Request": "", "Invoice Note": f"Couldn't do that: {ex}"[:2000]})
            results.append({"sale_id": it["sale_id"], "ok": False, "error": str(ex)})
    return {"results": results}


# ---------- attachments (any email she sends) ----------
ATTACH_FIELD = {"drafts": "Attachments", "log": "Reply Attachments"}
ATTACH_MAX = 5 * 1024 * 1024  # Airtable's upload limit per file


def _att_view(files):
    return [{"id": a.get("id"), "filename": a.get("filename"), "size": a.get("size"), "type": a.get("type"),
             "thumb": ((a.get("thumbnails") or {}).get("small") or {}).get("url")} for a in files or []]


@app.post("/attach/<kind>/<rid>")
@login_required
def attach_add(kind, rid):
    """Upload files onto the email's record. They go out with it whenever it sends."""
    field = ATTACH_FIELD.get(kind)
    if not field or not re.match(r"rec\w+$", rid):
        return {"error": "Unknown email"}, 400
    import base64 as _b64
    errors = []
    for up in request.files.getlist("files"):
        data = up.read()
        if not data:
            continue
        if len(data) > ATTACH_MAX:
            errors.append(f"{up.filename} is over 5 MB")
            continue
        r = requests.post(f"https://content.airtable.com/v0/{at.BASE}/{rid}/{field}/uploadAttachment", headers=at._h(), timeout=120,
                          json={"contentType": up.mimetype or "application/octet-stream", "filename": up.filename or "attachment",
                                "file": _b64.b64encode(data).decode()})
        if not r.ok:
            errors.append(f"{up.filename} didn't upload ({r.status_code})")
    at._forget(at.TABLES[kind])
    files = at.get(kind, rid)["fields"].get(field) or []
    return {"files": _att_view(files), "errors": errors}


@app.post("/attach/<kind>/<rid>/remove")
@login_required
def attach_remove(kind, rid):
    field = ATTACH_FIELD.get(kind)
    if not field:
        return {"error": "Unknown email"}, 400
    gone = request.form.get("id")
    files = [{"id": a["id"]} for a in (at.get(kind, rid)["fields"].get(field) or []) if a.get("id") != gone]
    at.update(kind, rid, {field: files})
    return {"files": _att_view(at.get(kind, rid)["fields"].get(field) or [])}


app.jinja_env.globals["att_view"] = _att_view


@app.get("/catchup")
@login_required
def catchup():
    """Catch-up: the To approve queue one email at a time, full screen. Send it, or leave it for later."""
    drafts = [d for d in pending_drafts() if not d["fields"].get("Remix Request")]
    names = at.partner_names([p for d in drafts for p in d["fields"].get("Partner", [])])
    for d in drafts:
        f = d["fields"]
        d["html"] = richtext.sanitize(f.get("Body HTML")) if (f.get("Body HTML") or "").strip() else richtext.text_to_html(f.get("Body") or "")
    return render_template("catchup.html", drafts=drafts, names=names, brands=brands_for(drafts, "To Email"))


@app.post("/approve/<rid>/remix")
@login_required
def remix(rid):
    d = at.get("drafts", rid)["fields"]
    instr = request.form.get("instruction", "").strip()
    if not instr or d.get("Status") not in ("Pending Approval", "Approved"):
        return redirect(url_for("approve"))
    body, body_html = _body_from_form(d.get("Body", ""))
    at.update("drafts", rid, {"Subject": request.form.get("subject", d.get("Subject", "")).strip(),
                              "Body": body, "Body HTML": body_html, "Remix Request": instr})
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


@app.get("/api/engine/cache")
def engine_cache():
    if not engine_auth():
        return {"error": "unauthorised"}, 401
    return at.cache_health()


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
                team = next((m["from"].split("<")[0].strip(' "') for m in msgs if not m["is_me"]
                             and "@workspace6.io" in m["from"].lower()), None)  # a teammate looped her in
                subj = next((m["subject"] for m in reversed(msgs) if not m["is_me"] and m.get("subject")), None) or msgs[0].get("subject") or ""
                threads[tid] = {"msgs": gmail.with_earlier(msgs, 10), "earlier": max(0, len(msgs) - 10), "via": team,
                                "subject": subj if subj.lower().startswith("re:") else f"Re: {subj}",
                                "plan": gmail.reply_plan(msgs),
                                "link": gmail.open_link(msgs[-1]["msgid"]) if msgs[-1]["msgid"] else None}
            except Exception:
                threads[tid] = None
    for e in ev:  # sponsor-form inquiries: no email thread yet, the form itself is the first message
        f = e["fields"]
        if not f.get("Gmail Thread ID") and (f.get("Gmail Message ID") or "").startswith(INQUIRY_PREFIX):
            nm = (f.get("Snippet") or "").split("From: ", 1)[-1].split(" <", 1)[0] if "From: " in (f.get("Snippet") or "") else f.get("Email")
            at_ms = int((parse_ts(f.get("At")) or now_utc()).timestamp() * 1000)
            threads["inquiry:" + e["id"]] = {
                "msgs": [{"from": f"{nm} <{f.get('Email')}> · sponsor form", "is_me": False, "ts": at_ms, "body": f.get("Snippet") or "",
                          "msgid": "inq-" + e["id"]}],
                "earlier": 0, "via": None, "link": None, "inquiry": True, "subject": f.get("Suggested Subject") or _inquiry_subject(f),
                "plan": {"people": [{"email": f.get("Email"), "name": nm or f.get("Email"), "role": "to", "team": False}], "note": None}}
    sums = {}
    for e in ev:
        try:
            sums[e["id"]] = json.loads(e["fields"].get("Message Summaries") or "{}")
        except ValueError:
            sums[e["id"]] = {}
    return render_template("yours.html", events=ev, names=names, threads=threads, brands=brands_for(ev, "Email"),
                           sums=sums, msg_key=engine.msg_key)


_GREETING = re.compile(r"^(hi|hey|hello|dear|good (morning|afternoon|evening)|thanks|thank you)\b[^\n]{0,40}[,!.]?\s*$", re.I)


app.jinja_env.globals["text_to_html"] = richtext.text_to_html


# ---------- logos, served from our own address ----------
# A sponsor's logo on their own domain gets blocked when that domain is on a tracker list (Funnelytics is an
# analytics company, so Brave and ad blockers block funnelytics.io outright) or when the site refuses hotlinking.
# Serving every logo from pings.workspace6.io fixes both. Signed, so this can't be used as an open proxy.
import hmac, hashlib, threading as _threading
_logo_cache, _logo_lock = {}, _threading.Lock()


def _logo_sig(url):
    return hmac.new(app.secret_key.encode() if isinstance(app.secret_key, str) else app.secret_key,
                    url.encode(), hashlib.sha256).hexdigest()[:20]


def logo_src(url):
    if not url or url.startswith("data:"):
        return url
    import base64 as _b64  # the address is encoded so blockers matching "funnelytics.io" anywhere in a URL can't see it
    return url_for("logo_proxy", key=_b64.urlsafe_b64encode(url.encode()).decode().rstrip("="), s=_logo_sig(url))


app.jinja_env.globals["logo_src"] = logo_src
app.jinja_env.globals["due_label"] = invoicing.due_label


@app.get("/logo/<key>")
def logo_proxy(key):
    import base64 as _b64
    try:
        u = _b64.urlsafe_b64decode(key + "=" * (-len(key) % 4)).decode()
    except Exception:
        return "", 404
    sig = request.args.get("s", "")
    if not u.startswith(("https://", "http://")) or not hmac.compare_digest(sig, _logo_sig(u)):
        return "", 404
    hit = _logo_cache.get(u)
    if not hit or dt.datetime.now().timestamp() - hit[2] > 86400:
        try:
            r = requests.get(u, timeout=8, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 Chrome/126 Safari/537.36"})
            ctype = r.headers.get("content-type", "").split(";")[0]
            if r.status_code != 200 or not ctype.startswith("image/") or len(r.content) > 1_500_000:
                return "", 404
            hit = (r.content, ctype, dt.datetime.now().timestamp())
            with _logo_lock:
                if len(_logo_cache) > 800:
                    _logo_cache.clear()
                _logo_cache[u] = hit
        except Exception:
            return "", 404
    from flask import Response
    return Response(hit[0], mimetype=hit[1], headers={"Cache-Control": "public, max-age=604800",
                                                      "X-Content-Type-Options": "nosniff",
                                                      "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})


@app.template_filter("gist")
def gist(body):
    """Until the AI summary lands: the email's opening line, skipping the greeting."""
    for line in (body or "").splitlines():
        line = line.strip()
        if line and not _GREETING.match(line) and not line.startswith(">"):
            m = re.match(r"(.+?[.!?])(\s|$)", line)  # first whole sentence, never cut mid-way
            return m.group(1) if m else line
    return ""


@app.post("/yours/<rid>/done")
@login_required
def yours_done(rid):
    e = at.get("log", rid)["fields"]
    at.update("log", rid, {"Handled": True})
    if request.form.get("outcome") == "mine":
        names = at.partner_names(e.get("Partner", []))
        company = ", ".join(names.values()) or e.get("Email", "")
        where = request.form.get("where") or "Somewhere else"
        until = request.form.get("until") or None
        note = " ".join(filter(None, [f"Taken offline ({where}) from Your turn.", request.form.get("note", "").strip()]))
        at.create("handsoff", {"Company": company, "Partner": e.get("Partner", []), "Active": True,
                               "Reason": {"WhatsApp": "Talking On WhatsApp", "Somewhere else": "Other"}.get(where, "Karlie Took Over"),
                               "Until": until, "Added By": session["user"], "Added At": iso(now_utc()), "Note": note})
        when = f"until {dt.date.fromisoformat(until).strftime('%-d %b')}" if until else "until you press Release under Hands off on the Today page"
        celebrate("All yours", f"The system won't email anyone at {company} {when}.", "📵")
    else:
        flash("Done. The system carries on with them as normal.")
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
    body, body_html = _body_from_form()
    subj = request.form.get("subject")
    at.update("log", rid, {"Suggested Reply": body, "Suggested Reply HTML": body_html,
                           **({"Suggested Subject": subj.strip()[:200]} if subj is not None else {})})
    if request.headers.get("X-Fetch") == "1":  # quiet background save while she types
        return {"ok": True}
    flash("Edits saved.")
    return redirect(url_for("yours") + f"#d-{rid}")


@app.post("/yours/<rid>/remix")
@login_required
def yours_remix(rid):
    instr = request.form.get("instruction", "").strip()
    if instr:
        body, body_html = _body_from_form()
        at.update("log", rid, {"Suggested Reply": body, "Suggested Reply HTML": body_html, "Remix Request": instr})
        flash("Rewriting it now. The new version usually shows up here in about a minute.")
    return redirect(url_for("yours") + f"#d-{rid}")


def _after_reply(rid, e, text, text_html, tid, mid, to):
    """Record a reply Karlie sent from Your turn (draft row for learning, ping log, close the item, tidy her inbox)."""
    t = iso(now_utc())
    link = {"Partner": e.get("Partner", []), "Sale": e.get("Sale", [])}
    if e.get("Contact"):
        link["Contact"] = e["Contact"]
    sug = (e.get("Suggested Reply") or "").strip()
    hist = e.get("Remix History") or ""
    first = hist.rsplit("\nBefore:\n", 1)[-1].split("\n\n[", 1)[0].strip() if hist else ""  # the very first suggestion, before any rewrite
    draft = at.create("drafts", {"Subject": "Reply from Karlie", "Status": "Sent", "Kind": "Reply",
                                 "To Email": to, "Body": text, "Body HTML": text_html, "Written By Karlie": not sug,
                                 **({"AI Original Body": first or sug, "Edited By Karlie": bool(hist) or _norm(text) != _norm(sug)} if sug else {}),
                                 **({"Remix History": hist} if hist else {}),
                                 **({"Karlie Feedback": request.form.get("feedback", "").strip()} if request.form.get("feedback", "").strip() else {}),
                                 "Gmail Thread ID": tid, "Sent At": t, "Decided At": t, **link})
    at.create("log", {"Summary": "Karlie replied from the dashboard", "Event": "Sent", "Direction": "Out", "By": "Karlie",
                      "Email": to, "At": t, "Snippet": text[:500], "Gmail Message ID": mid,
                      "Gmail Thread ID": tid, "Draft": [draft["id"]], "Handled": True, **link})
    at.update("log", rid, {"Handled": True})
    try:  # her reply keeps any promise to get back to them (other emails do this when they're logged; this one is logged here)
        engine._keep_promise((e.get("Contact") or [None])[0], (to or "").split(",")[0].strip())
    except Exception:
        pass
    try:
        gmail.mark_done(tid)  # read + archived in her inbox, since she's dealt with it
    except Exception:
        pass
    celebrate("Sent!", f"On its way to {to}. Marked read and archived in your inbox, and it'll learn from how you wrote it.", "💌")
    return redirect(url_for("yours"))


@app.post("/yours/<rid>/reply")
@login_required
def yours_reply(rid):
    e = at.get("log", rid)["fields"]
    text, text_html = _body_from_form()
    tid = e.get("Gmail Thread ID")
    pid = (e.get("Partner") or [None])[0]
    text, err = ob.fill_placeholder(text, pid)
    text_html, err2 = ob.fill_placeholder(text_html, pid, html=True)
    if err or err2:
        flash(f"Couldn't send that one: {err or err2}. Nothing went out; your text is below.")
        session["unsent_" + rid] = text
        return redirect(url_for("yours"))
    if text and not tid and (e.get("Gmail Message ID") or "").startswith(INQUIRY_PREFIX):
        # a sponsor-form inquiry: no thread yet, so this starts one (with her real signature)
        to_list = [x for x in (request.form.get("to_list") or "").split(",") if x.strip()] or [e.get("Email")]
        subject = request.form.get("subject", "").strip() or e.get("Suggested Subject") or _inquiry_subject(e)
        m = re.match(r"\s*Subject:\s*(.+?)\s*(?:\n|$)", text)
        if m:  # an old suggested reply still carrying "Subject:" on top: take it out of the body
            subject = request.form.get("subject", "").strip() or m.group(1).strip()
            text = text[m.end():].lstrip("\n")
            if text_html:
                text_html = re.sub(r"^\s*<div>\s*Subject:.*?</div>(\s*<div><br></div>)?", "", text_html, count=1, flags=re.S)
        try:
            mid, tid = gmail.send(", ".join(to_list), subject, text, signature=True, body_html=text_html,
                                  attachments=at.get("log", rid)["fields"].get("Reply Attachments"))
        except Exception as ex:
            flash(f"Couldn't send that one: {ex}. Nothing went out; your text is below.")
            session["unsent_" + rid] = text
            return redirect(url_for("yours"))
        at.update("log", rid, {"Gmail Thread ID": tid})
        to = ", ".join(to_list)
        return _after_reply(rid, e, text, text_html, tid, mid, to)
    if not text or not tid:
        flash("Nothing to send.")
        return redirect(url_for("yours"))
    try:
        chosen = {}
        if request.form.get("rcpt_set"):  # she used the "Sending to" chips; otherwise the default plan applies
            chosen = {k: [x for x in (request.form.get(f"{k}_list") or "").split(",") if x.strip()] for k in ("to", "cc", "bcc")}
        mid, to = gmail.reply(tid, text, body_html=text_html, attachments=at.get("log", rid)["fields"].get("Reply Attachments"), **chosen)
    except Exception as ex:
        flash(f"Couldn't send that one: {ex}. Nothing went out; your text is below.")
        session["unsent_" + rid] = text
        return redirect(url_for("yours"))
    return _after_reply(rid, e, text, text_html, tid, mid, to)


# ---------- rules ----------
NUM_FIELDS = ["Daily Ping Limit", "Daily Send Cap", "Send Window Start (Hour)", "Send Window End (Hour)", "Max Follow-ups",
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
        if fields.get("Daily Ping Limit") is not None:
            record_goal(fields["Daily Ping Limit"])
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
        if f.get("Subject") == "(skipped)":
            continue  # the overnight run deciding not to write one: not something Karlie did
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


def _deal_value(partners, field, paid_only):
    """Dollars on the Sales rows behind these sponsors' bookings: "Pencilled Value" (what it was pencilled in at) or
    "Value" (what it finally booked for)."""
    if not partners:
        return 0
    today = dt.date.today()
    sales = {b["sale"] for b in bk.load(today - dt.timedelta(days=120), today + dt.timedelta(days=365))
             if b["partner"] in partners and b.get("sale") and (b["state"] == "paid" or not paid_only)}
    tot = 0
    for sid in sales:
        f = at.get("sales", sid)["fields"]
        tot += int(f.get(field) or (f.get("Value") if field == "Pencilled Value" else 0) or 0)
    return tot


def _backup_stale(sf):
    """For Danny only: how long since the nightly backup last worked, if it's over 36 hours (else None)."""
    if session.get("user") != "danny":
        return None
    last = parse_ts(sf.get("Last Backup"))
    if last and now_utc() - last < dt.timedelta(hours=36):
        return None
    return when(sf.get("Last Backup")) if last else "never"


@app.route("/results")
@login_required
def results():
    since = (now_utc() - dt.timedelta(days=56)).strftime("%Y-%m-%d")
    ev = at.list_records("log", f"IS_AFTER({{At}}, '{since}')", sort=[("At", "desc")])
    now = dt.datetime.now(BRIS)
    at_of = lambda e: parse_ts(e["fields"].get("At") or "1970-01-01T00:00:00Z")
    # weeks run Monday to Sunday on US Eastern time, the same as the Leadership Scorecard, so the two always agree
    et = ZoneInfo("America/New_York")
    now_et = dt.datetime.now(et)
    weeks = []
    for w in range(7, -1, -1):
        start = (now_et - dt.timedelta(days=now_et.weekday() + 7 * w)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + dt.timedelta(days=7)
        inw = [e for e in ev if start <= at_of(e).astimezone(et) < end]
        bw = lambda k: {(e["fields"].get("Partner") or [None])[0] or (e["fields"].get("Email") or "").lower()
                        for e in inw if e["fields"].get("Event") == k} - {""}
        pw, cw = bw("Pencilled In"), bw("Paid") | bw("Confirmed")
        weeks.append({"label": start.strftime("%-d %b"),
                      "sent": sum(e["fields"].get("Event") == "Sent" for e in inw),
                      "replied": sum(e["fields"].get("Event") == "Replied" for e in inw),
                      "pencilled": len(pw), "converted": len(cw),
                      "pencilled_usd": _deal_value(pw, "Pencilled Value", False) if pw else 0,
                      "converted_usd": _deal_value(cw, "Value", True) if cw else 0})
    last30 = [e for e in ev if at_of(e) > now_utc() - dt.timedelta(days=30)]
    c = lambda k: sum(e["fields"].get("Event") == k for e in last30)
    totals = {"sent": c("Sent"), "replied": c("Replied"), "handed": c("Handed To Karlie"),
              "bounced": c("Bounced") + c("Left Company")}
    totals["rate"] = round(100 * totals["replied"] / totals["sent"]) if totals["sent"] else None
    # the funnel by business: replied -> pencilled in -> converted (confirmed; invoices will count here once they're built)
    biz = lambda k: {(e["fields"].get("Partner") or [None])[0] or (e["fields"].get("Email") or "").lower() for e in last30 if e["fields"].get("Event") == k} - {""}
    rb, pb, ib, cb = biz("Replied"), biz("Pencilled In"), biz("Invoiced"), biz("Paid") | biz("Confirmed")
    totals.update(replied_biz=len(rb), pencilled=len(pb), converted=len(cb),
                  pencil_rate=round(100 * len(pb) / len(rb)) if rb else None,
                  pencilled_usd=_deal_value(pb, "Pencilled Value", False), converted_usd=_deal_value(cb, "Value", True),
                  invoiced=len(ib), invoiced_usd=_deal_value(ib, "Value", False))
    try:  # in case Stripe's paid message didn't arrive
        invoicing.check_payments()
    except Exception:
        pass
    totals["value_rate"] = round(100 * totals["converted_usd"] / totals["pencilled_usd"]) if totals["pencilled_usd"] else None
    dead = at.list_records("contacts", "AND({Status}!='Active', {Status}!='')", fields=["Status"])
    peak = max([w["sent"] for w in weeks] + [1])
    deal_peak = max([max(w["pencilled"], w["converted"]) for w in weeks] + [1])
    # latest activity: one line per thing that happened (a reply and its "your turn" hand-off are the same moment)
    handoffs = {(e["fields"].get("Gmail Thread ID"), (e["fields"].get("At") or "")[:16]) for e in ev
                if e["fields"].get("Event") == "Handed To Karlie"}
    q = (request.args.get("q") or "").strip()
    kind = request.args.get("kind") or ""
    KINDS = {"sent": {"Sent"}, "yours": {"Handed To Karlie"}, "replied": {"Replied", "Handed To Karlie"},
             "bounced": {"Bounced", "Left Company", "Unsubscribed"}}
    pool = ev
    if q or kind:  # searching: look through the whole history, not just the last 8 weeks
        pool = at.list_records("log", sort=[("At", "desc")], fields=["At", "Event", "Email", "Partner", "Summary", "Snippet",
                                                                       "Gmail Thread ID", "By", "Handled"])
        handoffs = {(e["fields"].get("Gmail Thread ID"), (e["fields"].get("At") or "")[:16]) for e in pool
                    if e["fields"].get("Event") == "Handed To Karlie"}
    recent = [e for e in pool if not (e["fields"].get("Event") == "Replied" and
                                      (e["fields"].get("Gmail Thread ID"), (e["fields"].get("At") or "")[:16]) in handoffs)]
    if kind in KINDS:
        recent = [e for e in recent if e["fields"].get("Event") in KINDS[kind]]
    matches = None
    if q:
        pnames = at.partner_names([p for e in recent for p in e["fields"].get("Partner", [])])
        # names of just the people in this activity (not all ~1,800 contacts), a few at a time
        emails = sorted({(e["fields"].get("Email") or "").lower() for e in recent if e["fields"].get("Email")})
        people = {}
        for i in range(0, len(emails), 40):
            ors = ",".join("LOWER({Email})='" + x.replace("'", "\\'") + "'" for x in emails[i:i + 40])
            for c in at.list_records("contacts", f"OR({ors})", fields=["Email", "Name"]):
                people[(c["fields"].get("Email") or "").lower()] = c["fields"].get("Name") or ""
        words = q.lower().split()

        def hay(e):
            f = e["fields"]
            return " ".join([" ".join(pnames.get(p, "") for p in f.get("Partner", [])), f.get("Email") or "",
                             people.get((f.get("Email") or "").lower(), ""), f.get("Summary") or "", f.get("Snippet") or "",
                             f.get("Event") or "", "your turn" if f.get("Event") == "Handed To Karlie" else ""]).lower()
        recent = [e for e in recent if all(w in hay(e) for w in words)]
        matches = len(recent)
    recent = recent[:100 if (q or kind) else 40]
    # the paper trail: everything that's happened with the same person, oldest first
    by_email = {}
    for e in sorted(pool, key=lambda e: e["fields"].get("At") or ""):
        em = (e["fields"].get("Email") or "").lower()
        if em:
            by_email.setdefault(em, []).append(e)
    story = {e["id"]: by_email.get((e["fields"].get("Email") or "").lower(), [])[-12:] for e in recent}
    names = at.partner_names([p for e in recent for p in e["fields"].get("Partner", [])])
    span = 1 if request.args.get("span", "today") == "today" else 7
    return render_template("results.html", weeks=weeks, peak=peak, deal_peak=deal_peak, totals=totals,
                           n_dead=len(dead), recent=recent, names=names, brands=brands_for(recent, "Email"),
                           q=q, kind=kind, matches=matches, story=story, deals=_safe(bk.deals, []),
                           gmail_thread=lambda t: f"https://mail.google.com/mail/u/{gmail.ME}/#all/{t}")


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


@app.post("/deals/confirm")
@login_required
def deal_confirm():
    """Karlie marks a pencilled-in deal as confirmed: every one of its dates turns 🤑 Paid in the calendar."""
    ids = [x for x in request.form.get("ids", "").split(",") if re.match(r"rec\w+$", x)]
    final = re.sub(r"[^\d.]", "", request.form.get("value") or "")
    sale = request.form.get("sale") or ""
    if final and re.match(r"rec\w+$", sale):  # what it finally booked for (the pencilled-in value stays as it was)
        at.update("sales", sale, {"Value": int(float(final))})
    name = ""
    for rid in ids:
        r = at.get("promo", rid)["fields"]
        kind, state = bk.parse_status(r.get("Status"))
        name = name or (r.get("Name") or "").split(" · ")[0]
        if kind and state != "paid":
            at.update("promo", rid, {"Status": "🤑 " + bk.TYPES[kind]["status"]})
    if ids:
        pid = (at.get("promo", ids[0])["fields"].get("Sponsor") or [None])[0]
        at.create("log", {"Event": "Confirmed", "Direction": "Out", "By": "Karlie", "At": iso(now_utc()), "Handled": True,
                          "Summary": f"Confirmed {name}: {len(ids)} date{'s' if len(ids) > 1 else ''}", **({"Partner": [pid]} if pid else {})}, typecast=True)
        celebrate("Confirmed! 🤑", f"{name} · all {len(ids)} dates are now confirmed in the calendar.", "💸", big=True)
    return redirect(url_for("results") + "#deals")


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
                for path in ("/", "/approve", "/yours", "/bookings", "/bookings?view=list", "/rules", "/results",
                             "/results?q=warm"):  # keeps the full history ready, so searching Results is instant
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
