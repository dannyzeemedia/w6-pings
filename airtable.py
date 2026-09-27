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
    "promo": "tblDgkaHQFHHPNAHA",  # W6 Promo Calendar: one row per placement per publish date
}
API = f"https://api.airtable.com/v0/{BASE}"


def _h():
    return {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}"}


_cache = {}
CACHE_TTL = 45  # seconds; any write clears it (single worker process, so the clear is seen everywhere)


STALE_OK = 900  # seconds: past the TTL, keep answering from the saved copy while a background refresh runs
_refreshing = set()
_gen = [0]  # bumped on every write; a refresh that started before a write must not store its older copy
import threading as _th
_local = _th.local()  # _local.fresh = True for the sending engine: never answer it from a stale copy


def _refresh(key, url, kw):
    try:
        _fetch("GET", url, key, gen=_gen[0], **kw)
    except Exception:
        pass
    finally:
        _refreshing.discard(key)


def _req(method, url, **kw):
    if method == "GET":
        key = (url, repr(sorted((kw.get("params") or {}).items())))
        hit = _cache.get(key)
        if hit:
            age = time.time() - hit[0]
            if age < CACHE_TTL:
                return hit[1]
            if age < STALE_OK and not getattr(_local, "fresh", False):  # never make her wait: answer now, freshen underneath
                if key not in _refreshing:
                    import threading
                    _refreshing.add(key)
                    threading.Thread(target=_refresh, args=(key, url, kw), daemon=True).start()
                return hit[1]
        return _fetch(method, url, key, gen=_gen[0], **kw)
    _cache.clear()  # a write: everything re-reads fresh, so she always sees her own change
    _gen[0] += 1
    try:
        return _fetch(method, url, None, **kw)
    finally:
        _cache.clear()
        _gen[0] += 1


def _fetch(method, url, key, gen=None, **kw):
    for attempt in range(4):
        r = requests.request(method, url, headers=_h(), timeout=30, **kw)
        if r.status_code == 429:
            time.sleep(1 + attempt)
            continue
        if r.status_code >= 400:
            raise requests.HTTPError(f"{r.status_code} {r.text[:500]}", response=r)
        j = r.json()
        if method == "GET" and key is not None and (gen is None or gen == _gen[0]):
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


def create(table, fields, typecast=False):
    return _req("POST", f"{API}/{TABLES[table]}", json={"fields": fields, "typecast": typecast})


def create_many(table, rows, typecast=False):
    out = []
    for i in range(0, len(rows), 10):
        out += _req("POST", f"{API}/{TABLES[table]}", json={"records": [{"fields": r} for r in rows[i:i + 10]], "typecast": typecast})["records"]
    return out


def delete(table, rid):
    return _req("DELETE", f"{API}/{TABLES[table]}/{rid}")


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
