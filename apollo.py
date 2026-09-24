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
