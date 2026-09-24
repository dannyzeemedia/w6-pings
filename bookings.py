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


def parse_status(st):
    st = st or ""
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
        sponsor = (", ".join(names.get(p, "") for p in f.get("Sponsor", []))
                   or (nm.split(" · ")[0] if " · " in nm else re.sub(r"\s*(Package|DTC News|W6|Marquee|Welcome|Shout.?Out|Group Buy).*$", "", nm, flags=re.I))
                   or "Unknown")
        out.append({"id": r["id"], "date": dt.date.fromisoformat(f["Publish Date"]), "kind": kind, "state": state,
                    "sponsor": sponsor.strip(), "name": f.get("Name", ""), "seq": f.get("# / #", ""),
                    "partner": (f.get("Sponsor") or [None])[0]})
    return out


def open_marquees(bookings, start, end):
    taken = {b["date"] for b in bookings if b["kind"] == "marquee"}
    d, out = max(start, dt.date.today()), []
    while d <= end:
        if d.weekday() in MARQUEE_DAYS and d not in taken:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def dates_for(start, count, cadence):
    """Publish dates for a new booking: 'sends' = next Tue/Thu sends, 'daily', 'weekly', or 'once'."""
    out, d = [], start
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


def upcoming_for_brain(days=42):
    """Taken and open Marquee dates for the next few weeks, so drafts only offer real openings."""
    today = dt.date.today()
    b = load(today, today + dt.timedelta(days=days))
    return {"booked": [{"date": x["date"].isoformat(), "type": TYPES[x["kind"]]["label"], "sponsor": x["sponsor"], "state": x["state"]} for x in b],
            "open_marquee_dates": [d.isoformat() for d in open_marquees(b, today, today + dt.timedelta(days=days))]}
