"""Thin Airtable client for the outbound tables in the W6 Media base."""
import os, time, requests

BASE = "appdyjBrdeFXlffLu"
TABLES = {
    "settings": "tblYHD5AbjmyxYOsE",
    "contacts": "tblS5yU6mINsHeVX0",
    "drafts": "tblPOhostHQMYHYJ6",
    "log": "tblIepjDgookM42p1",
    "partners": "tbl7OH9U8ed4ZLLvI",
}
API = f"https://api.airtable.com/v0/{BASE}"


def _h():
    return {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}"}


def _req(method, url, **kw):
    for attempt in range(4):
        r = requests.request(method, url, headers=_h(), timeout=30, **kw)
        if r.status_code == 429:
            time.sleep(1 + attempt)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def list_records(table, formula=None, sort=None, fields=None, max_records=None):
    params, out = {}, []
    if formula:
        params["filterByFormula"] = formula
    if max_records:
        params["maxRecords"] = max_records
    for i, (f, d) in enumerate(sort or []):
        params[f"sort[{i}][field]"] = f
        params[f"sort[{i}][direction]"] = d
    if fields:
        params["fields[]"] = fields
    while True:
        j = _req("GET", f"{API}/{TABLES[table]}", params=params)
        out += j["records"]
        if not j.get("offset") or (max_records and len(out) >= max_records):
            return out
        params["offset"] = j["offset"]


def get(table, rid):
    return _req("GET", f"{API}/{TABLES[table]}/{rid}")


def update(table, rid, fields):
    return _req("PATCH", f"{API}/{TABLES[table]}/{rid}", json={"fields": fields, "typecast": False})


def settings():
    return list_records("settings", max_records=1)[0]


def partner_names(ids):
    """Map partner record ids -> names (one request)."""
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    formula = "OR(" + ",".join(f"RECORD_ID()='{i}'" for i in ids[:90]) + ")"
    return {r["id"]: r["fields"].get("Name", "") for r in list_records("partners", formula, fields=["Name"])}
