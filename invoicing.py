"""Sponsor invoices through Workspace6's Stripe: a draft built from the booking, previewed and changed in Pings
(plain-English change requests are turned into edits by the AI), then sent by Karlie. Paid = converted.

The deal's Sales row carries it all: Stripe Customer ID, Stripe Invoice ID, Invoice Email, Invoice #, Invoice (link),
Due Date, Status (Discussion -> Invoiced -> Paid), Invoiced At, Paid At, Value (what it finally comes to)."""
import datetime as dt
import json
import os
import re
import time
from zoneinfo import ZoneInfo
import requests
import airtable as at
import bookings as bk

API = "https://api.stripe.com/v1/"
ET = ZoneInfo("America/New_York")          # W6 bills in US time; "due Friday" means Friday there
MEMO = "Thanks for helping us make Workspace6 awesome!"
DEFAULT_DAYS = 7
PACKAGE_PRODUCT = "W6: Custom Sponsorship Package"


def _s():
    s = requests.Session()
    s.auth = (os.environ["W6_STRIPE_INVOICING"], "")
    return s


def _get(path, **params):
    r = _s().get(API + path, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _post(path, data):
    r = _s().post(API + path, data=data, timeout=30)
    if not r.ok:
        raise RuntimeError((r.json().get("error") or {}).get("message") or r.text[:200])
    return r.json()


def _del(path):
    _s().delete(API + path, timeout=30)


def _footer():
    """The footer W6's invoices already use (the W9 link), taken from the latest invoice that has one."""
    for inv in _get("invoices", limit=10).get("data", []):
        if inv.get("footer"):
            return inv["footer"]
    return ""


def _product_id(name):
    for p in _get("products", limit=100, active="true").get("data", []):
        if p["name"] == name:
            return p["id"]
    return None


def email_from(text):
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text or "")
    return m.group(0).lower() if m else None


def sale(sid):
    return at._fetch("GET", f"{at.API}/{at.TABLES['sales']}/{sid}", None)


PACKAGE_LINE = "Custom Sponsorship Package"


def booked(pid, sid=None):
    """What they booked, per placement: [(kind, [dates], any TBC)] from the calendar."""
    rows = [b for b in bk.current_for(pid) if b["state"] in ("pencilled", "paid", "swap") and (not sid or not b.get("sale") or b.get("sale") == sid)]
    by = {}
    for b in sorted(rows, key=lambda b: b["date"]):
        by.setdefault(b["kind"], []).append(b)
    return [(k, [b["date"] for b in v], any("date to confirm" in b["name"] for b in v)) for k, v in by.items()]


def includes_memo(pid, sid=None):
    """The memo: what the package includes as dot points with dates, then W6's usual thank-you."""
    lines = []
    for kind, dates, tbc in booked(pid, sid):
        label = bk.TYPES[kind]["label"] + ("s" if len(dates) > 1 else "")
        lines.append(f"• {len(dates)} × {label}: " + ", ".join(d.strftime("%a %-d %b") for d in dates) + (" (dates to confirm)" if tbc else ""))
    return ("Includes:\n" + "\n".join(lines) + "\n\n" + MEMO) if lines else MEMO


def lines_for(pid, value):
    """The first draft: one Custom Sponsorship Package line at the deal's value (what's in it goes in the memo)."""
    return [{"description": PACKAGE_LINE, "amount": int(value or 0)}]


def _set_lines(inv_id, customer, lines):
    for it in _get("invoiceitems", invoice=inv_id, limit=100).get("data", []):
        _del(f"invoiceitems/{it['id']}")
    prod = _product_id(PACKAGE_PRODUCT)
    for ln in lines:
        data = {"customer": customer, "invoice": inv_id, "currency": "usd", "description": ln["description"][:500]}
        cents = int(round(float(ln["amount"]) * 100))
        if prod:
            data.update({"price_data[product]": prod, "price_data[currency]": "usd", "price_data[unit_amount]": cents, "quantity": 1})
        else:
            data["amount"] = cents
        _post("invoiceitems", data)


def terms(f):
    """("days", 7) or ("date", "YYYY-MM-DD"): the payment terms Pings keeps for this deal."""
    t = (f.get("Invoice Terms") or f"days:{DEFAULT_DAYS}").split(":", 1)
    return (t[0], int(t[1])) if t[0] == "days" else ("date", t[1])


def _due_on_send(f):
    kind, val = terms(f)
    if kind == "date":
        return val
    return (dt.datetime.now(ET).date() + dt.timedelta(days=val)).isoformat()


def _due_ts(date_str):
    d = dt.date.fromisoformat(date_str)
    return int(dt.datetime(d.year, d.month, d.day, 23, 59, tzinfo=ET).timestamp())


def prepare(sid, email=None, company=None):
    """Create (or reuse) their Stripe customer and a draft invoice from the booking. Returns the Stripe invoice id."""
    f = sale(sid)["fields"]
    if f.get("Stripe Invoice ID"):
        inv = _get(f"invoices/{f['Stripe Invoice ID']}")
        if inv.get("status") == "draft":
            return inv["id"]
    pid = (f.get("Partner") or [None])[0]
    name = company or (at.partner_names([pid]).get(pid) if pid else None) or f.get("Opportunity", "Sponsor")
    email = (email or f.get("Invoice Email") or "").strip().lower()
    if not email:
        raise RuntimeError("There's no email to send the invoice to yet. Add one first.")
    cust = f.get("Stripe Customer ID")
    if not cust:
        found = _get("customers", email=email, limit=1).get("data", [])
        cust = found[0]["id"] if found else _post("customers", {"name": name, "email": email,
                                                                "metadata[pings_partner]": pid or "", "metadata[source]": "W6 Pings"})["id"]
    inv = _post("invoices", {"customer": cust, "collection_method": "send_invoice", "days_until_due": DEFAULT_DAYS,
                             "auto_advance": "false", "description": includes_memo(pid, sid), "footer": _footer(),
                             "metadata[pings_sale]": sid, "pending_invoice_items_behavior": "exclude"})
    value = f.get("Value") or f.get("Pencilled Value") or 0
    _set_lines(inv["id"], cust, lines_for(pid, value))
    at.update("sales", sid, {"Stripe Customer ID": cust, "Stripe Invoice ID": inv["id"], "Invoice Email": email,
                             "Invoice Terms": f"days:{DEFAULT_DAYS}"})
    return inv["id"]


def view(sid):
    """Everything the preview shows, straight from Stripe."""
    f = sale(sid)["fields"]
    if not f.get("Stripe Invoice ID"):
        return None
    inv = _get(f"invoices/{f['Stripe Invoice ID']}", **{"expand[]": ["discounts", "customer"]})
    cust = inv.get("customer") if isinstance(inv.get("customer"), dict) else {}
    # a draft on "N days" terms shows a provisional date from today; the real one is set when it's sent
    kind, val = terms(f)
    if inv["status"] == "draft":  # the real date is set when it's sent
        due, days = (val, None) if kind == "date" else (None, val)
    else:
        due, days = (dt.datetime.fromtimestamp(inv["due_date"], ET).date().isoformat() if inv.get("due_date") else None), None
    return {"id": inv["id"], "status": inv["status"], "number": inv.get("number"), "customer_name": cust.get("name"),
            "email": cust.get("email"), "lines": [{"description": l.get("description"), "amount": l["amount"] / 100}
                                                  for l in inv["lines"]["data"]],
            "subtotal": inv["subtotal"] / 100, "total": inv["total"] / 100,
            "discounts": [{"name": (d.get("coupon") or {}).get("name") or "Discount",
                           "percent": (d.get("coupon") or {}).get("percent_off"),
                           "amount": ((d.get("coupon") or {}).get("amount_off") or 0) / 100}
                          for d in (inv.get("discounts") or []) if isinstance(d, dict)],
            "discount_total": sum(x["amount"] for x in inv.get("total_discount_amounts") or []) / 100,
            "due_date": due, "days_until_due": days, "memo": inv.get("description"),
            "footer": inv.get("footer"), "hosted_url": inv.get("hosted_invoice_url"), "pdf": inv.get("invoice_pdf"),
            "amount_paid": inv.get("amount_paid", 0) / 100, "amount_due": inv.get("amount_due", 0) / 100,
            "currency": (inv.get("currency") or "usd").upper(), "livemode": inv.get("livemode"),
            "issued": (dt.datetime.fromtimestamp(inv["status_transitions"]["finalized_at"], ET).date().isoformat()
                       if (inv.get("status_transitions") or {}).get("finalized_at") else None),
            "due_if_sent_today": _due_on_send(f) if inv["status"] == "draft" else None,
            "line_items": [{"description": l.get("description"), "qty": l.get("quantity") or 1,
                            "unit": (l["amount"] / 100) / (l.get("quantity") or 1), "amount": l["amount"] / 100}
                           for l in inv["lines"]["data"]]}


def apply(sid, ops, note=""):
    """Apply the AI's reading of a change request to the draft. ops keys (all optional): lines [{description, amount}]
    (replaces every line), discount {percent|amount, name} or null (removes), due {days} or {date: YYYY-MM-DD},
    memo, email."""
    f = sale(sid)["fields"]
    inv_id, cust = f.get("Stripe Invoice ID"), f.get("Stripe Customer ID")
    inv = _get(f"invoices/{inv_id}")
    if inv["status"] != "draft":
        raise RuntimeError("This invoice has already gone out, so it can't be changed here. Void it in Stripe and start again.")
    if ops.get("lines"):
        _set_lines(inv_id, cust, ops["lines"])
    upd = {}
    if "discount" in ops:
        d = ops["discount"]
        if not d:
            upd["discounts"] = ""
        else:
            c = {"duration": "once", "name": (d.get("name") or "Discount")[:40]}
            if d.get("percent"):
                c["percent_off"] = float(d["percent"])
            else:
                c.update({"amount_off": int(round(float(d.get("amount") or 0) * 100)), "currency": "usd"})
            upd["discounts[0][coupon]"] = _post("coupons", c)["id"]
    due = ops.get("due") or {}
    if due.get("date"):
        dt.date.fromisoformat(due["date"])
        at.update("sales", sid, {"Invoice Terms": f"date:{due['date']}"})
    elif due.get("days") is not None:
        at.update("sales", sid, {"Invoice Terms": f"days:{int(due['days'])}"})
    if ops.get("memo") is not None:
        upd["description"] = ops["memo"]
    if upd:
        _post(f"invoices/{inv_id}", {k: v for k, v in upd.items()})
    if ops.get("email"):
        e = email_from(ops["email"])
        if e:
            _post(f"customers/{cust}", {"email": e})
            at.update("sales", sid, {"Invoice Email": e})
    at.update("sales", sid, {"Invoice Request": "", "Invoice Note": (note or "Done.")[:2000]})


def send(sid):
    """Karlie's Send: finalize and email it through Stripe, then mark the deal Invoiced."""
    f = sale(sid)["fields"]
    inv_id = f["Stripe Invoice ID"]
    inv = _get(f"invoices/{inv_id}")
    if inv["status"] == "draft":
        _post(f"invoices/{inv_id}", {"due_date": _due_ts(_due_on_send(f))})  # the terms, counted from today
        inv = _post(f"invoices/{inv_id}/finalize", {"auto_advance": "false"})
    if inv["status"] == "open":
        inv = _post(f"invoices/{inv_id}/send", {})
    due = dt.datetime.fromtimestamp(inv["due_date"], ET).date().isoformat() if inv.get("due_date") else None
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    at.update("sales", sid, {"Status": "Invoiced", "Invoice #": inv.get("number") or "", "Invoice": inv.get("hosted_invoice_url"),
                             **({"Due Date": due} if due else {}), "Invoiced At": now, "Value": int(inv["total"] / 100)})
    pid = (f.get("Partner") or [None])[0]
    name = at.partner_names([pid]).get(pid) if pid else f.get("Opportunity")
    at.create("log", {"Event": "Invoiced", "Direction": "Out", "By": "Karlie", "At": now, "Handled": True,
                      "Email": f.get("Invoice Email"), "Summary": f"Invoiced {name}: ${inv['total'] / 100:,.0f}, due {due or 'on receipt'}"[:250],
                      "Link": f"/invoice/{sid}", **({"Partner": [pid]} if pid else {})}, typecast=True)
    return inv


def mark_paid(sid, amount=None, how="Stripe"):
    """Paid = converted: the deal goes to Paid, its dates go 🤑 in the calendar, Karlie gets a pop-up."""
    f = sale(sid)["fields"]
    if f.get("Status") == "Paid":
        return False
    pid = (f.get("Partner") or [None])[0]
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    amt = int(amount if amount is not None else (f.get("Value") or 0))
    at.update("sales", sid, {"Status": "Paid", "Paid At": now, "Value": amt})
    for b in (bk.current_for(pid) if pid else []):
        if b.get("sale") == sid and b["state"] != "paid":
            at.update("promo", b["id"], {"Status": "🤑 " + bk.TYPES[b["kind"]]["status"]})
    name = at.partner_names([pid]).get(pid) if pid else f.get("Opportunity")
    at.create("log", {"Event": "Paid", "Direction": "In", "By": "Them", "At": now, "Handled": True,
                      "Summary": f"{name} paid ${amt:,}" + (" (marked by hand)" if how != "Stripe" else ""),
                      "Link": f"/invoice/{sid}", **({"Partner": [pid]} if pid else {})}, typecast=True)
    return True


def check_payments():
    """Fallback to the webhook: any Invoiced deal whose Stripe invoice is now paid."""
    done = []
    for r in at.list_records("sales", "AND({Status}='Invoiced', {Stripe Invoice ID}!='')", fields=["Stripe Invoice ID"]):
        inv = _get(f"invoices/{r['fields']['Stripe Invoice ID']}")
        if inv.get("status") == "paid" and mark_paid(r["id"], (inv.get("amount_paid") or inv.get("total") or 0) / 100):
            done.append(r["id"])
    return done


def preflight(sid, v=None):
    """The last look before Send: is it going to the right people, for the right thing, at the right price, due on a
    sensible date? Each check is (ok, text). Nothing here blocks sending; it makes sure Karlie sees anything odd."""
    f = sale(sid)["fields"]
    v = v or view(sid)
    pid = (f.get("Partner") or [None])[0]
    name = at.partner_names([pid]).get(pid) if pid else None
    out = []
    contact, company = {}, None
    for r in at.list_records("onboard", fields=["Partner", "Answers"]):
        if pid and pid in (r["fields"].get("Partner") or []):
            contact = json.loads(r["fields"].get("Answers") or "{}").get("contact") or {}
            company = contact.get("company")
    told = email_from(contact.get("invoices"))
    if told:
        out.append((v["email"] == told, f"Goes to {v['email']}" + (", the invoice contact they gave us." if v["email"] == told
                                                                   else f". They asked for invoices to go to {told}.")))
    else:
        out.append((bool(v["email"]), f"Goes to {v['email']}. (They haven't given us an invoice contact.)"))
    want = company or name
    out.append(((v["customer_name"] or "").strip().lower() == (want or "").strip().lower(),
                f"Billed to {v['customer_name']}" + ("." if (v["customer_name"] or "").strip().lower() == (want or "").strip().lower()
                                                     else f", but their confirmed company name is {want}.")))
    deal = int(f.get("Value") or 0)
    out.append((round(v["total"]) == deal or not deal,
                f"${v['total']:,.2f} total" + (", matching the deal." if round(v["total"]) == deal else f", the deal is ${deal:,}. Fine if you changed it on purpose.")))
    memo = v.get("memo") or ""
    missing = [d for kind, dates, _ in booked(pid, sid) for d in dates if d.strftime("%-d %b") not in memo]
    n = sum(len(d) for _, d, _ in booked(pid, sid))
    out.append((not missing, f"Lists all {n} booked placements with their dates." if not missing
                else "Not listed on the invoice: " + ", ".join(d.strftime("%a %-d %b") for d in missing) + "."))
    due = v.get("due_date") or v.get("due_if_sent_today")
    if due:
        d = dt.date.fromisoformat(due)
        today = dt.datetime.now(ET).date()
        good = d >= today and d.weekday() < 5
        out.append((good, f"Due {d.strftime('%A %-d %B')} if sent today" + ("." if good else (", which is a weekend." if d.weekday() >= 5 else ", which has already passed."))))
    out.append((bool(v.get("livemode")), "A real invoice in W6's live Stripe." if v.get("livemode") else "This is in Stripe TEST mode."))
    if (f.get("Invoice Request") or "").strip():
        out.append((False, "A change is still being made. Wait for it before sending."))
    return out
