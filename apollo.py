"""Find a replacement contact at a company via Apollo (only used for gone-contact rescues)."""
import os, requests

API = "https://api.apollo.io/api/v1"
TITLES = ["partnerships", "partner", "marketing", "growth", "brand", "founder", "ceo", "head of", "community", "sponsorship"]


def _h():
    return {"X-Api-Key": os.environ["APOLLO_API_KEY"], "Content-Type": "application/json", "Cache-Control": "no-cache"}


def people(domain, exclude=(), limit=3):
    """Up to `limit` verified people at `domain` with partnership/marketing-ish titles. Spends ~1 credit per reveal."""
    r = requests.post(f"{API}/mixed_people/api_search", headers=_h(), timeout=30,
                      json={"q_organization_domains_list": [domain], "person_titles": TITLES, "per_page": 15})
    r.raise_for_status()
    cands = [p for p in r.json().get("people", []) if p.get("id")][:8]
    if not cands:
        return []
    m = requests.post(f"{API}/people/bulk_match", headers=_h(), timeout=60,
                      json={"details": [{"id": p["id"]} for p in cands[:10]], "reveal_personal_emails": False})
    m.raise_for_status()
    out = []
    for p in m.json().get("matches", []) or []:
        if not p:
            continue
        e = (p.get("email") or "").lower()
        if not e or p.get("email_status") != "verified" or e in exclude or not e.endswith("@" + domain):
            continue
        out.append({"email": e, "name": p.get("name"), "title": p.get("title"), "linkedin": p.get("linkedin_url")})
        if len(out) >= limit:
            break
    return out


def person(first, last, domain, org=None):
    """One named person (e.g. a founder the brain found while researching), if Apollo has a verified email for them."""
    body = {k: v for k, v in {"first_name": first, "last_name": last, "domain": domain, "organization_name": org}.items() if v}
    r = requests.post(f"{API}/people/match", headers=_h(), timeout=30, json=body)
    r.raise_for_status()
    p = r.json().get("person") or {}
    e = (p.get("email") or "").lower()
    if e and p.get("email_status") == "verified":
        return {"email": e, "name": p.get("name"), "title": p.get("title"), "linkedin": p.get("linkedin_url")}
    return None


def anyone(domain, exclude=(), limit=2):
    """Tiny companies often have no partnership/marketing titles: fall back to anyone verified there."""
    r = requests.post(f"{API}/mixed_people/api_search", headers=_h(), timeout=30,
                      json={"q_organization_domains_list": [domain], "per_page": 10})
    r.raise_for_status()
    ids = [p["id"] for p in r.json().get("people", []) if p.get("id")][:6]
    if not ids:
        return []
    m = requests.post(f"{API}/people/bulk_match", headers=_h(), timeout=60, json={"details": [{"id": i} for i in ids]})
    m.raise_for_status()
    out = []
    for p in m.json().get("matches", []) or []:
        e = ((p or {}).get("email") or "").lower()
        if e and p.get("email_status") == "verified" and e not in exclude and e.endswith("@" + domain):
            out.append({"email": e, "name": p.get("name"), "title": p.get("title"), "linkedin": p.get("linkedin_url")})
    return out[:limit]
