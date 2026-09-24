"""Thin Airtable client for the outbound tables in the W6 Media base."""
import os, time, requests

BASE = "appdyjBrdeFXlffLu"
TABLES = {
    "settings": "tblYHD5AbjmyxYOsE",
    "contacts": "tblS5yU6mINsHeVX0",
    "drafts": "tblPOhostHQMYHYJ6",
    "log": "tblIepjDgookM42p1",
    "lessons": "tblgMPagOFFHzNpxC",
    "partners": "tbl7OH9U8ed4ZLLvI",
    "sales": "tblakxHy3Dke93A3b",
    "handsoff": "tblUuIEQoMGOAv8r0",
}
API = f"https://api.airtable.com/v0/{BASE}"


def _h():
    return {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}"}


_cache = {}
CACHE_TTL = 15  # seconds; any write clears it, so the dashboard never shows stale data after an action


def _req(method, url, **kw):
    if method == "GET":
        key = (url, repr(sorted((kw.get("params") or {}).items())))
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
    else:
        _cache.clear()
    for attempt in range(4):
        r = requests.request(method, url, headers=_h(), timeout=30, **kw)
        if r.status_code == 429:
            time.sleep(1 + attempt)
            continue
        r.raise_for_status()
        j = r.json()
        if method == "GET":
            _cache[key] = (time.time(), j)
        return j
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


def create(table, fields):
    return _req("POST", f"{API}/{TABLES[table]}", json={"fields": fields, "typecast": False})


def settings():
    return list_records("settings", max_records=1)[0]


def partner_names(ids):
    """Map partner record ids -> names (one request)."""
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    formula = "OR(" + ",".join(f"RECORD_ID()='{i}'" for i in ids[:90]) + ")"
    return {r["id"]: r["fields"].get("Name", "") for r in list_records("partners", formula, fields=["Name"])}


_names_cache = {"at": 0, "rows": []}


def all_partners():
    """[(id, name)] for every partner, cached for 10 minutes (used for the company picker)."""
    if time.time() - _names_cache["at"] > 600:
        rows = list_records("partners", fields=["Name"])
        _names_cache.update(at=time.time(), rows=sorted(
            [(r["id"], r["fields"]["Name"].strip()) for r in rows if r["fields"].get("Name", "").strip()],
            key=lambda x: x[1].lower()))
    return _names_cache["rows"]
