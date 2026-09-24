"""Karlie's outbound dashboard: daily ping count, approvals, replies to handle, rules, results.
Airtable (W6 Media base) is the database; this app is only a friendlier screen over it."""
import os, json, datetime as dt
from functools import wraps
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort
from werkzeug.security import check_password_hash
import airtable as at

app = Flask(__name__)
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


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("user"):
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


@app.template_filter("when")
def when(s):
    t = parse_ts(s)
    if not t:
        return ""
    t = t.astimezone(BRIS)
    today = dt.datetime.now(BRIS).date()
    if t.date() == today:
        return "today " + t.strftime("%-I:%M%p").lower()
    if t.date() == today - dt.timedelta(days=1):
        return "yesterday"
    return t.strftime("%-d %b")


@app.context_processor
def inject():
    return {"user": session.get("user"), "zone_label": ZONE_LABEL}


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
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- today ----------
def sent_events(days=35):
    since = (dt.datetime.utcnow() - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    return at.list_records("log", f"AND({{Event}}='Sent', IS_AFTER({{At}}, '{since}'))", fields=["At"])


@app.route("/")
@login_required
def today():
    s = at.settings()
    f = s["fields"]
    tz = ZoneInfo(f.get("Send Timezone") or "America/New_York")
    sent = sent_events(1)
    sent_today = sum(1 for r in sent if parse_ts(r["fields"].get("At")) and
                     parse_ts(r["fields"]["At"]).astimezone(tz).date() == dt.datetime.now(tz).date())
    pending = at.list_records("drafts", "{Status}='Pending Approval'", fields=["Subject"])
    yours = at.list_records("log", "AND({Event}='Handed To Karlie', NOT({Handled}))", fields=["Summary"])
    return render_template("today.html", s=f, sid=s["id"], sent_today=sent_today,
                           n_pending=len(pending), n_yours=len(yours))


@app.post("/settings/quick")
@login_required
def quick_settings():
    s = at.settings()
    fields = {}
    if "limit" in request.form:
        fields["Daily Ping Limit"] = max(0, min(200, int(request.form["limit"] or 0)))
    if "paused" in request.form:
        fields["Paused"] = request.form["paused"] == "1"
    at.update("settings", s["id"], fields)
    return redirect(url_for("today"))


# ---------- approvals ----------
@app.route("/approve")
@login_required
def approve():
    drafts = at.list_records("drafts", "{Status}='Pending Approval'", sort=[("Scheduled For", "asc")])
    names = at.partner_names([p for d in drafts for p in d["fields"].get("Partner", [])])
    return render_template("approve.html", drafts=drafts, names=names)


@app.post("/approve/<rid>")
@login_required
def decide(rid):
    d = at.get("drafts", rid)["fields"]
    if d.get("Status") != "Pending Approval":
        flash("That email was already dealt with.")
        return redirect(url_for("approve"))
    action = request.form["action"]
    subject, body = request.form.get("subject", "").strip(), request.form.get("body", "").strip()
    fields = {"Subject": subject, "Body": body,
              "Karlie Feedback": request.form.get("feedback", "").strip()}
    if action in ("send", "reject"):
        fields["Decided At"] = dt.datetime.utcnow().isoformat() + "Z"
    fields["Edited By Karlie"] = (body != (d.get("AI Original Body") or "").strip()
                                  or subject != (d.get("AI Original Subject") or "").strip())
    if action == "send":
        fields["Status"] = "Approved"
        flash("Approved. It'll go out in the next send window.")
    elif action == "reject":
        fields["Status"] = "Rejected"
        flash("Binned. It'll learn from why.")
    else:
        flash("Edits saved.")
    at.update("drafts", rid, fields)
    return redirect(url_for("approve"))


# ---------- replies handed to her ----------
@app.route("/yours")
@login_required
def yours():
    ev = at.list_records("log", "AND({Event}='Handed To Karlie', NOT({Handled}))", sort=[("At", "desc")])
    names = at.partner_names([p for e in ev for p in e["fields"].get("Partner", [])])
    return render_template("yours.html", events=ev, names=names)


@app.post("/yours/<rid>/done")
@login_required
def yours_done(rid):
    at.update("log", rid, {"Handled": True})
    return redirect(url_for("yours"))


# ---------- rules ----------
NUM_FIELDS = ["Daily Ping Limit", "Send Window Start (Hour)", "Send Window End (Hour)", "Max Follow-ups",
              "Follow-up 1 After (Days)", "Follow-up 2 After (Days)", "Follow-up 3 After (Days)",
              "Days Before Re-pitching A Company", "Rescue Tries Per Company"]


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
        fields["Send Days"] = [d for d in DAYS if fm.get("day_" + d)]
        fields["Send Timezone"] = fm.get("Send Timezone") if fm.get("Send Timezone") in ZONES else None
        fields["Approval Mode"] = fm.get("Approval Mode") if fm.get("Approval Mode") in MODES else "Approve Every Draft"
        fields["Follow-ups Must Add Something New"] = bool(fm.get("Follow-ups Must Add Something New"))
        fields["Gone-Contact Rescue"] = bool(fm.get("Gone-Contact Rescue"))
        fields["Notes"] = fm.get("Notes", "")
        at.update("settings", s["id"], fields)
        flash("Rules saved.")
        return redirect(url_for("rules"))
    return render_template("rules.html", s=s["fields"], days=DAYS, zones=ZONES, modes=MODES)


# ---------- results ----------
@app.route("/results")
@login_required
def results():
    since = (dt.datetime.utcnow() - dt.timedelta(days=56)).strftime("%Y-%m-%d")
    ev = at.list_records("log", f"IS_AFTER({{At}}, '{since}')", sort=[("At", "desc")])
    now = dt.datetime.now(BRIS)
    weeks = []
    for w in range(7, -1, -1):
        start = (now - dt.timedelta(days=now.weekday() + 7 * w)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + dt.timedelta(days=7)
        inw = [e for e in ev if start <= parse_ts(e["fields"].get("At", "1970-01-01T00:00:00Z")).astimezone(BRIS) < end]
        weeks.append({"label": start.strftime("%-d %b"),
                      "sent": sum(e["fields"].get("Event") == "Sent" for e in inw),
                      "replied": sum(e["fields"].get("Event") == "Replied" for e in inw)})
    last30 = [e for e in ev if parse_ts(e["fields"].get("At", "1970-01-01T00:00:00Z")) >
              dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)]
    c = lambda k: sum(e["fields"].get("Event") == k for e in last30)
    totals = {"sent": c("Sent"), "replied": c("Replied"), "handed": c("Handed To Karlie"),
              "bounced": c("Bounced") + c("Left Company")}
    totals["rate"] = round(100 * totals["replied"] / totals["sent"]) if totals["sent"] else None
    dead = at.list_records("contacts", "AND({Status}!='Active', {Status}!='')", fields=["Status"])
    peak = max([w["sent"] for w in weeks] + [1])
    names = at.partner_names([p for e in ev[:40] for p in e["fields"].get("Partner", [])])
    return render_template("results.html", weeks=weeks, peak=peak, totals=totals,
                           n_dead=len(dead), recent=ev[:40], names=names)


@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8090)), debug=bool(os.environ.get("DEBUG")))
