"""Sponsor bookings (W6 Promo Calendar): what's booked, what's open, adding and editing placements."""
import datetime as dt
import re
import airtable as at

# Placement types as they appear in the calendar's Status field ("✏️ " = pencilled in, "🤑 " = paid).
TYPES = {
    "marquee":  {"label": "DTC News Marquee",   "status": "DTC News: Marquee"},
    "shoutout": {"label": "DTC News Shout-Out", "status": "DTC News: Shout-Out"},
    "welcome":  {"label": "Welcome Flow",       "status": "W6: Welcome"},
    "groupbuy": {"label": "Group Buy",          "status": "W6: Group Buy"},
    "takeover": {"label": "DTC News Takeover",  "status": "DTC News Takeover"},
    "7fc":      {"label": "7FC Marquee",        "status": "7FC: Marquee"},
}
MARQUEE_DAYS = (1, 3)  # DTC News goes out Tuesday and Thursday; one Marquee per send


BLOCK = "⛔ Blockout: "   # Status prefix for a no-sponsor blockout, e.g. "⛔ Blockout: DTC News: Marquee"
MAIN_KINDS = ["marquee", "shoutout", "welcome", "groupbuy", "takeover"]


def parse_status(st):
    st = st or ""
    if st.startswith("⛔"):
        for key, t in TYPES.items():
            if t["status"] in st:
                return key, "blockout"
        return None, None
    paid = "🤑" in st
    swap = "SWAP" in st
    for key, t in TYPES.items():
        if t["status"] in st:
            return key, ("swap" if swap else "paid" if paid else "pencilled")
    return None, None


def load(start, end):
    """Bookings with a publish date in [start, end]."""
    f = f"AND(IS_AFTER({{Publish Date}}, '{(start - dt.timedelta(days=1)).isoformat()}'), IS_BEFORE({{Publish Date}}, '{(end + dt.timedelta(days=1)).isoformat()}'))"
    rows = at.list_records("promo", f, sort=[("Publish Date", "asc")])
    pids = [p for r in rows for p in r["fields"].get("Sponsor", [])]
    names = at.partner_names(pids) if pids else {}
    out = []
    for r in rows:
        f = r["fields"]
        kind, state = parse_status(f.get("Status"))
        if not kind or not f.get("Publish Date"):
            continue
        nm = f.get("Name", "")
        if state == "blockout":
            out.append({"id": r["id"], "date": dt.date.fromisoformat(f["Publish Date"]), "kind": kind, "state": state,
                        "sponsor": "Blocked out", "name": f.get("Name", ""), "seq": "", "partner": None})
            continue
        sponsor = (", ".join(names.get(p, "") for p in f.get("Sponsor", []))
                   or (nm.split(" · ")[0] if " · " in nm else re.sub(r"\s*(Package|DTC News|W6|Marquee|Welcome|Shout.?Out|Group Buy).*$", "", nm, flags=re.I))
                   or "Unknown")
        out.append({"id": r["id"], "date": dt.date.fromisoformat(f["Publish Date"]), "kind": kind, "state": state,
                    "sponsor": sponsor.strip(), "name": f.get("Name", ""), "seq": f.get("# / #", ""),
                    "partner": (f.get("Sponsor") or [None])[0]})
    return out


def group_of(rid):
    """Every date that belongs to the same booking as this row (same name, type, sponsor, 'x/N' run, created together)."""
    r = at.get("promo", rid)
    f = r["fields"]
    seq = f.get("# / #") or ""
    m = re.match(r"\s*\d+\s*/\s*(\d+)\s*$", seq)
    if not m or int(m.group(1)) <= 1:
        return [r]
    total = m.group(1)
    kind, _ = parse_status(f.get("Status"))
    base = TYPES[kind]["status"] if kind else (f.get("Status") or "")
    name = (f.get("Name") or "").replace("'", "\\'")
    formula = (f"AND({{Name}}='{name}', FIND('{base}', {{Status}}), REGEX_MATCH({{# / #}}&'', '/\\s*{total}\\s*$'))")
    rows = at.list_records("promo", formula, sort=[("Publish Date", "asc")])
    sp = f.get("Sponsor") or []
    rows = [x for x in rows if (x["fields"].get("Sponsor") or []) == sp]
    # two separate bookings can share a name and length: keep the ones created the same day as this row
    day = r.get("createdTime", "")[:10]
    same = [x for x in rows if x.get("createdTime", "")[:10] == day]
    return same if any(x["id"] == rid for x in same) else [r]


def open_marquees(bookings, start, end):
    taken = {b["date"] for b in bookings if b["kind"] == "marquee"}  # booked, pencilled or blocked out
    d, out = max(start, dt.date.today()), []
    while d <= end:
        if d.weekday() in MARQUEE_DAYS and d not in taken:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def dates_for(start, count, cadence):
    """Publish dates for a new booking: 'sends' = the date she picked, then the next Tue/Thu sends; 'daily', 'weekly', or 'once'."""
    out, d = [], start
    if cadence == "sends" and count >= 1:
        out, d = [start], start + dt.timedelta(days=1)
    while len(out) < count:
        if cadence == "sends":
            if d.weekday() in MARQUEE_DAYS:
                out.append(d)
            d += dt.timedelta(days=1)
        elif cadence == "daily":
            out.append(d); d += dt.timedelta(days=1)
        elif cadence == "weekly":
            out.append(d); d += dt.timedelta(days=7)
        else:
            out.append(d); break
    return out


def add(sponsor_name, partner_id, kind, state, start, count, cadence):
    t = TYPES[kind]
    status = ("🤑 " if state == "paid" else "✏️ ") + t["status"]
    ds = dates_for(start, max(1, min(count, 120)), cadence)
    rows = [{"Name": f"{sponsor_name} · {t['label']}", "Status": status, "Publish Date": d.isoformat(),
             "# / #": f"{i + 1}/{len(ds)}", **({"Sponsor": [partner_id]} if partner_id else {})} for i, d in enumerate(ds)]
    # typecast so a brand-new type (e.g. Group Buy) creates its option the first time
    at.create_many("promo", rows, typecast=True)
    return ds


def blocked_dates(kind, dates):
    """Which of these dates are blocked out for this placement."""
    if not dates:
        return []
    ds = sorted(dt.date.fromisoformat(d) if isinstance(d, str) else d for d in dates)
    b = load(ds[0], ds[-1])
    blocked = {x["date"] for x in b if x["state"] == "blockout" and x["kind"] == kind}
    return [d for d in ds if d in blocked]


def add_blockout(kind, start, end, note=""):
    kinds = MAIN_KINDS if kind == "all" else [kind]
    days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    rows = [{"Name": f"Blockout · {TYPES[k]['label']}" + (f" · {note}" if note else ""), "Status": BLOCK + TYPES[k]["status"],
             "Publish Date": d.isoformat()} for k in kinds for d in days]
    at.create_many("promo", rows, typecast=True)
    return days


def upcoming_for_brain(days=42):
    """Taken and open Marquee dates for the next few weeks, so drafts only offer real openings."""
    today = dt.date.today()
    b = load(today, today + dt.timedelta(days=days))
    return {"booked": [{"date": x["date"].isoformat(), "type": TYPES[x["kind"]]["label"], "sponsor": x["sponsor"], "state": x["state"]} for x in b if x["state"] != "blockout"],
            "blocked_out": [{"date": x["date"].isoformat(), "type": TYPES[x["kind"]]["label"]} for x in b if x["state"] == "blockout"],
            "open_marquee_dates": [d.isoformat() for d in open_marquees(b, today, today + dt.timedelta(days=days))]}


# ---------------------------------------------------------------- plain-English bookings
import difflib
from dateutil import parser as _dparser

_KIND_WORDS = [
    ("groupbuy", r"group\s*-?\s*buy|\bgb\b|\bdms?\b"),
    ("welcome", r"welcome"),
    ("shoutout", r"shout\s*-?\s*outs?|shoutouts?"),
    ("takeover", r"take\s*-?\s*over|takeover"),
    ("marquee", r"marquees?"),
]
_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
        "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a couple of": 2, "couple of": 2}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _num(tok):
    tok = tok.strip().lower()
    return int(tok) if tok.isdigit() else _NUM.get(tok)


def _match_sponsor(text, partners):
    """Best partner name mentioned in the text; else the words after 'in'/'for' as a new sponsor."""
    # the command words ("pencil in", "book", "paid") and placement words are never the sponsor
    # (there's a real partner called "Pencil")
    low = re.sub(r"\b(pencil(?:led)?(?:\s+in)?|book(?:ed)?|add|paid(?:\s+for)?|lock(?:ed)?\s+in|confirm(?:ed)?|"
                 r"welcome\s+flow|welcome|marquees?|shout\s*-?\s*outs?|group\s*buys?|takeover|dtc\s+news|sponsorship|weeks?|months?|days?)\b",
                 " ", text.lower())
    hits = [(len(n), pid, n) for pid, n in partners if len(n) > 2 and re.search(r"\b" + re.escape(n.lower()) + r"\b", low)]
    if hits:
        _, pid, n = max(hits)
        return n, pid
    m = re.search(r"(?:pencil(?:led)?\s+in|book(?:ed)?|add|paid(?:\s+for)?|lock\s+in)\s+(.+?)\s+(?:for|in|on|with|starting|from|\d)", text, re.I)
    cand = (m.group(1) if m else "").strip(" ,.'\"")
    cand = re.sub(r"^(a|an|the)\s+", "", cand, flags=re.I)
    if not cand:
        return None, None
    close = difflib.get_close_matches(cand.lower(), [n.lower() for _, n in partners], n=1, cutoff=0.82)
    if close:
        pid, n = next((p, n) for p, n in partners if n.lower() == close[0])
        return n, pid
    return cand[:1].upper() + cand[1:], None


def _start(text, today):
    low = text.lower()
    m = re.search(r"(?:starting|from|beginning|begins|on|week\s+of)\s+(?:the\s+)?(?:week\s+of\s+)?([A-Za-z0-9 ,/\-]+?)(?:$|\.|\s+(?:for|and|at|as|until)\b)", text, re.I)
    for chunk in ([m.group(1)] if m else []) + [text]:
        c = chunk.strip()
        if re.search(r"\bnext week\b", c, re.I):
            return today + dt.timedelta(days=7 - today.weekday())
        if re.search(r"\bthis week\b", c, re.I):
            return today
        if re.search(r"\btomorrow\b", c, re.I):
            return today + dt.timedelta(days=1)
        wd = re.search(r"\b(next\s+)?(" + "|".join(_WEEKDAYS) + r")\b", c, re.I)
        if wd and not re.search(r"\d", c):
            target = _WEEKDAYS.index(wd.group(2).lower())
            ahead = (target - today.weekday()) % 7 or 7
            return today + dt.timedelta(days=ahead + (7 if wd.group(1) and ahead < 7 and False else 0))
        MON = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        bare = re.search(r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b(?:\s+of\s+(this|next)\s+month)?", c, re.I)
        if bare and not re.search(r"\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?[A-Za-z]{3,9}", c.replace("of this month", "").replace("of next month", "")) or (bare and bare.group(2)):
            import calendar
            dday, which = int(bare.group(1)), (bare.group(2) or "").lower()
            y, m = today.year, today.month
            if which == "next" or (not which and dday < today.day - 7):
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            return dt.date(y, m, min(dday, calendar.monthrange(y, m)[1]))
        dm = re.search(r"(\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?" + MON + r"\.?(?:,?\s+\d{4})?|" + MON + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?|\d{1,2}/\d{1,2}(?:/\d{2,4})?)", c, re.I)
        if dm:
            try:
                d = _dparser.parse(re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", dm.group(1)).replace(" of ", " "), dayfirst=True, default=dt.datetime(today.year, 1, 1)).date()
                if d < today - dt.timedelta(days=60) and not re.search(r"\d{4}", dm.group(1)):
                    d = d.replace(year=d.year + 1)
                return d
            except (ValueError, OverflowError):
                pass
    return None


_MON_RE = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"


def _date_range(t, today):
    """'from the 25th of June to the 31st', 'June 25 - July 3', '25/6 to 1/7' -> (start, end)."""
    import calendar
    day = r"(\d{1,2})(?:st|nd|rd|th)?"
    pats = [re.compile(day + r"\s+(?:of\s+)?(" + _MON_RE + r")\.?(?:,?\s+(\d{4}))?", re.I),
            re.compile(r"(" + _MON_RE + r")\.?\s+" + day + r"(?:,?\s+(\d{4}))?", re.I)]
    found = []
    for p in pats:
        for m in p.finditer(t):
            g = m.groups()
            d, mon, yr = (g[0], g[1], g[2]) if p is pats[0] else (g[1], g[0], g[2])
            found.append((m.start(), m.end(), int(d), mon, yr))
    found.sort()
    if not found:
        # bare days: "from the 26th to the 30th", "26-30", optionally "of next month"
        m = re.search(r"(?:from\s+|between\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\s*(?:to|until|till|through|thru|and|-|–)\s*(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\b(?:\s+of\s+(this|next)\s+month)?", t, re.I)
        if not m or not re.search(r"(st|nd|rd|th|from|between|the)", m.group(0), re.I):
            return None
        d1, d2, which = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
        y, mo = today.year, today.month
        if which == "next" or (not which and d1 < today.day - 7):
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        last = calendar.monthrange(y, mo)[1]
        start = dt.date(y, mo, min(d1, last))
        if d2 >= d1:
            end = dt.date(y, mo, min(d2, last))
        else:
            ny, nm = (y + 1, 1) if mo == 12 else (y, mo + 1)
            end = dt.date(ny, nm, min(d2, calendar.monthrange(ny, nm)[1]))
        return start, end
    _, e1, d1, mon1, yr1 = found[0]
    mnum = lambda mon: [x.lower()[:3] for x in calendar.month_abbr[1:]].index(mon.lower()[:3]) + 1
    y = int(yr1) if yr1 else today.year
    m1 = mnum(mon1)
    if len(found) > 1:
        _, _, d2, mon2, yr2 = found[1]
        m2, y2 = mnum(mon2), int(yr2) if yr2 else y
    else:
        bare = re.search(r"(?:to|until|till|through|thru|-|–)\s*(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\b", t[e1:], re.I)
        if not bare:
            return None
        d2, m2, y2 = int(bare.group(1)), m1, y
    clamp = lambda yy, mm, dd: dt.date(yy, mm, min(dd, calendar.monthrange(yy, mm)[1]))
    start, end = clamp(y, m1, d1), clamp(y2, m2, d2)
    if not yr1 and start < today - dt.timedelta(days=60):
        start, end = start.replace(year=start.year + 1), clamp(end.year + 1, end.month, end.day)
    if end < start:
        end = clamp(end.year + 1, end.month, end.day)
    return start, end


def parse_request(text, partners, today=None):
    """Turn 'Pencil in Omnisend for 3 weeks of welcome flow starting Oct 6, 2026' into a concrete booking plan."""
    today = today or dt.date.today()
    t = " ".join(text.split())
    low = t.lower()
    kind = next((k for k, pat in _KIND_WORDS if re.search(pat, low)), None)
    if re.search(r"\bblock\s*-?\s*out|\bblock(?:ed)?\b|\bno bookings\b|\bunavailable\b|\bclosed\b", low):
        if not kind and re.search(r"\b(everything|all|every publication|all placements)\b", low):
            kind = "all"
        if not kind:
            return {"ok": False, "error": "Which placement should be blocked out? Say Marquee, Shout-Out, Welcome Flow, Group Buy, or \"everything\"."}
        rng = _date_range(t, today)
        if not rng:
            return {"ok": False, "error": "I need a start and end date, e.g. \"block out DTC Marquee from 25 June to 30 June\"."}
        start, end = rng
        label = "Everything" if kind == "all" else TYPES[kind]["label"]
        n = (end - start).days + 1
        return {"ok": True, "blockout": True, "kind": kind, "label": label, "state": "blockout", "sponsor": "",
                "start": start.isoformat(), "end": end.isoformat(), "dates": [start.isoformat(), end.isoformat()],
                "summary": f"⛔ Block out: {label} · {start.strftime('%a %-d %b %Y')} to {end.strftime('%a %-d %b %Y')} · {n} day{'s' if n > 1 else ''}"}
    state = "paid" if re.search(r"\bpaid\b|\bconfirmed\b|🤑|locked in|has paid", low) else "pencilled"
    sponsor, pid = _match_sponsor(t, partners)
    start = _start(t, today)
    problems = []
    if not kind:
        problems.append("which placement (Marquee, Shout-Out, Welcome Flow or Group Buy)")
    if not sponsor:
        problems.append("the sponsor's name")
    if problems:
        return {"ok": False, "error": "I couldn't tell " + " or ".join(problems) + ". Try: \"Pencil in Omnisend for 3 Marquees starting next Tuesday\"."}
    # how much / how long
    unit_m = re.search(r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|a couple of)\s+(weeks?|months?|days?|sends?|marquees?|shout\s*-?\s*outs?|shoutouts?|dms?|times?|issues?)\b", low)
    count, unit = (None, None)
    if unit_m:
        count, unit = _num(unit_m.group(1)), unit_m.group(2)
    if not start:
        start = today if kind == "welcome" else next(today + dt.timedelta(days=i) for i in range(1, 8) if (today + dt.timedelta(days=i)).weekday() in MARQUEE_DAYS)
    sends_kind = kind in ("marquee", "shoutout", "takeover")
    dates = []
    rng = _date_range(t, today) if re.search(r"\b(to|until|till|through|thru|between)\b|\d\s*[-–]\s*\d", low) else None
    if rng:
        start, end = rng
        d = start
        while d <= end:
            if kind == "welcome" or d == start or (sends_kind and d.weekday() in MARQUEE_DAYS) or (kind == "groupbuy" and (d - start).days % 7 == 0):
                dates.append(d)
            d += dt.timedelta(days=1)
    elif unit and unit.startswith(("week", "month")):
        span = dt.timedelta(days=7 * count) if unit.startswith("week") else dt.timedelta(days=30 * count)
        end = start + span - dt.timedelta(days=1)
        if kind == "welcome":
            dates = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
        elif sends_kind:
            d = start
            while d <= end:
                if d.weekday() in MARQUEE_DAYS or d == start:
                    dates.append(d)
                d += dt.timedelta(days=1)
        else:  # group buy: one DM a week for the period
            dates = [start + dt.timedelta(days=7 * i) for i in range(count)]
    elif unit and unit.startswith("day") and kind == "welcome":
        dates = [start + dt.timedelta(days=i) for i in range(count)]
    else:
        n = count or (2 if kind == "groupbuy" and re.search(r"\bgroup\s*buy\b", low) and not unit_m else 1)
        if kind == "welcome" and not count:
            dates = [start + dt.timedelta(days=i) for i in range(30)]  # a Welcome Flow sponsorship defaults to a month
        elif sends_kind:
            dates = dates_for(start, n, "sends")
        else:
            dates = dates_for(start, n, "weekly")
    if not dates:
        return {"ok": False, "error": "That works out to no dates. Try giving a start date and how long."}
    clash = blocked_dates(kind, dates)
    if clash:
        return {"ok": False, "error": f"{TYPES[kind]['label']} is blocked out on " + ", ".join(d.strftime('%a %-d %b') for d in clash[:5])
                + (" and more" if len(clash) > 5 else "") + ". Pick other dates or remove the blockout first."}
    label = TYPES[kind]["label"]
    span = dates[0].strftime("%a %-d %b %Y") + ("" if len(dates) == 1 else " to " + dates[-1].strftime("%a %-d %b %Y"))
    return {"ok": True, "sponsor": sponsor, "partner_id": pid, "new_sponsor": pid is None, "kind": kind, "label": label,
            "state": state, "dates": [d.isoformat() for d in dates],
            "summary": f"{'🤑 Paid' if state == 'paid' else '✏️ Pencil in'}: {sponsor} · {label} · {len(dates)} "
                       f"{'day' if kind == 'welcome' else 'send' if sends_kind else 'date'}{'s' if len(dates) > 1 else ''} · {span}"}


def add_dates(sponsor_name, partner_id, kind, state, dates):
    t = TYPES[kind]
    status = ("🤑 " if state == "paid" else "✏️ ") + t["status"]
    rows = [{"Name": f"{sponsor_name} · {t['label']}", "Status": status, "Publish Date": d, "# / #": f"{i + 1}/{len(dates)}",
             **({"Sponsor": [partner_id]} if partner_id else {})} for i, d in enumerate(dates)]
    at.create_many("promo", rows, typecast=True)
