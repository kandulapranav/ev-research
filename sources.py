"""Every data source the researchers use, as plain functions that return JSON-ready dicts.

Live, per question:   NHTSA Recalls API (+ units affected per campaign), NHTSA 5-Star Safety Ratings API
Offline, data/*:      NHTSA complaints and investigations (bulk files), EPA specs, WA registrations
                      (built by build/fetch_data.py)

Every record carries a `cite` URL, so the Writer can link each fact to where it came from.
"""

import csv
import difflib
import gzip
import re
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

import httpx

DATA = Path(__file__).parent / "data"
API = "https://api.nhtsa.gov"
_http = httpx.Client(timeout=45, headers={"User-Agent": "ev-research (class project)"})


def norm(text: str) -> str:
    """One spelling for comparisons: 'Mustang Mach-E' -> 'MUSTANG MACH E'."""
    return " ".join(str(text).upper().replace("-", " ").split())


def _get(path: str, **params) -> dict:
    r = _http.get(API + path, params=params)
    if r.status_code == 400:  # NHTSA answers 400 for a model it doesn't know
        return {}
    r.raise_for_status()
    return r.json()


def recall_url(campaign: str) -> str:
    return f"{API}/recalls/campaignNumber?campaignNumber={campaign}"


def complaint_url(odi) -> str:
    return f"{API}/complaints/odinumber?odinumber={odi}"


COMPLAINTS_DATASET_URL = "https://www.nhtsa.gov/nhtsa-datasets-and-apis#complaints"


@lru_cache(maxsize=1024)
def complaints_query_url(make: str, model: str, year) -> str:
    """A link that shows this model year's complaints on NHTSA's own API. The complaints API only
    answers to its exact model text ('MUSTANG MACH-E', not 'MUSTANG MACH E'), so try the spellings
    and keep the first that returns complaints. Some models (every Tesla) have no spelling that
    works; those cite the NHTSA complaints dataset the counts come from."""
    make, model = norm(make), norm(model)
    rows = [c for c in _by_vehicle[(make, model)] if c["model_year"] == str(year)]
    tries = [v for v, _ in Counter(c["model_variant"] for c in rows).most_common(3)]
    tries += [r["base_model"].upper() for r in EV_MODELS if r["make"].upper() == make and r["nhtsa_model"] == model]
    tries += [model, model.replace(" ", "-", 1)]
    for name in dict.fromkeys(tries):
        if _get("/complaints/complaintsByVehicle", make=make, model=name, modelYear=year).get("results"):
            return str(httpx.URL(f"{API}/complaints/complaintsByVehicle", params={"make": make, "model": name, "modelYear": year}))
    return COMPLAINTS_DATASET_URL


def recalls_query_url(make: str, model: str, year) -> str:
    return str(httpx.URL(f"{API}/recalls/recallsByVehicle", params={"make": norm(make), "model": model, "modelYear": year}))


def epa_url(epa_id: str) -> str:
    return f"https://www.fueleconomy.gov/feg/noframes/{epa_id}.shtml"


def investigation_url(action: str) -> str:
    return f"https://www.nhtsa.gov/?nhtsaId={action}"


# --- Offline tables ---

EV_MODELS = list(csv.DictReader((DATA / "ev_models.csv").open()))
COMPLAINTS = list(csv.DictReader(gzip.open(DATA / "ev_complaints.csv.gz", "rt")))
INVESTIGATIONS = list(csv.DictReader((DATA / "ev_investigations.csv").open()))
WA = list(csv.DictReader((DATA / "wa_registrations.csv").open()))

_by_vehicle: dict[tuple[str, str], list[dict]] = defaultdict(list)
for _c in COMPLAINTS:
    _by_vehicle[(_c["make"], _c["model"])].append(_c)

# Every EV name we know, as (MAKE, MODEL), from complaints, investigations and EPA.
KNOWN = set(_by_vehicle) | {(r["make"].upper(), r["nhtsa_model"]) for r in EV_MODELS}
for _r in INVESTIGATIONS:
    for _v in _r["vehicles"].split("; "):
        _y, _make, _model = _v.split(" ", 2)
        KNOWN.add((_make, _model))


# --- Vehicle lookup ---


MAKE_ALIASES = {"CHEVY": "CHEVROLET", "VW": "VOLKSWAGEN", "MERCEDES": "MERCEDES-BENZ", "BENZ": "MERCEDES-BENZ",
                "MERC": "MERCEDES-BENZ", "CADDY": "CADILLAC", "JAG": "JAGUAR", "VOLVO CARS": "VOLVO"}


def _summary(make: str, model: str) -> dict:
    years = Counter(c["model_year"] for c in _by_vehicle[(make, model)])
    epa = sorted({int(r["year"]) for r in EV_MODELS if r["make"].upper() == make and r["nhtsa_model"] == model})
    return {"model": model, "complaints": sum(years.values()), "complaint_model_years": sorted(years), "epa_model_years": epa}


def find_vehicle(make: str, model: str) -> dict:
    """Resolve what the user called an EV ('Mach-E', 'Ioniq5', 'Bolt') to the name every tool uses.
    If several EVs match ('Bolt': BOLT, BOLT EV, BOLT EUV), returns them all as candidates."""
    make_u = MAKE_ALIASES.get(norm(make), norm(make))
    want = norm(model).replace(" ", "")
    names = {m: m.replace(" ", "") for k, m in KNOWN if k == make_u}
    if not names:
        makes = sorted({k for k, _ in KNOWN})
        return {"error": f"No EV from make '{make}'. Known EV makes: {makes}"}
    exact = [m for m, squashed in names.items() if squashed == want]
    contains = [m for m, squashed in names.items() if want in squashed or squashed in want]
    close = difflib.get_close_matches(norm(model), list(names), n=3, cutoff=0.6)
    picks = list(dict.fromkeys(exact + sorted(contains, key=len) + close))
    if not picks:
        return {"error": f"No EV '{model}' from {make_u}. Its EVs: {sorted(names)}"}
    cands = [_summary(make_u, m) for m in picks[:5]]
    # One clear match: the only one, or an exact name with owner data ('Bolt EV'). An exact name
    # with no data next to others that have it ('Bolt': the 2027 Bolt vs Bolt EV/EUV) is ambiguous.
    if len(cands) == 1 or (exact and (cands[0]["complaints"] or len(contains) <= 1)):
        return {"make": make_u, **cands[0], "note": "Use exactly this make and model in every research task."}
    return {"make": make_u, "candidates": cands,
            "note": "Several EVs match. If the question doesn't say which one, ask the user."}


# --- Recalls (live API) ---


def _recall_spellings(make: str, model: str) -> list[str]:
    """Recall API names: usually the canonical name; hyphenated or suffixed for some models."""
    out = [model, model.replace(" ", "-", 1)]
    out += [r["base_model"].upper() for r in EV_MODELS if r["make"].upper() == make and r["nhtsa_model"] == model]
    return list(dict.fromkeys(out))


@lru_cache(maxsize=512)
def _recalls_for(make: str, model: str, year: int) -> tuple[str, tuple]:
    """(the spelling that worked, the recalls)."""
    for name in _recall_spellings(make, model):
        rows = _get("/recalls/recallsByVehicle", make=make, model=name, modelYear=year).get("results", [])
        if rows:
            return name, tuple(rows)
    return model, ()


@lru_cache(maxsize=512)
def _units(campaign: str) -> int | None:
    rows = _get("/recalls/campaignNumber", campaignNumber=campaign).get("results", [])
    return rows[0].get("PotentialNumberofUnitsAffected") if rows else None


def recalls(make: str, model: str, years: list[int]) -> dict:
    """Every recall for the model in those years: component, defect, fix type, units affected."""
    make, model = norm(make), norm(model)
    by_campaign: dict[str, dict] = {}
    source_urls = {}
    for y in years:
        spelled, rows = _recalls_for(make, model, int(y))
        source_urls[y] = recalls_query_url(make, spelled, y)
        for r in rows:
            c = r["NHTSACampaignNumber"]
            rec = by_campaign.setdefault(c, {
                "campaign": c,
                "date": "-".join(reversed(r["ReportReceivedDate"].split("/"))),  # DD/MM/YYYY -> YYYY-MM-DD
                "component": r["Component"],
                "defect": r["Summary"][:600],
                "consequence": r["Consequence"][:300],
                "remedy": r["Remedy"][:350],
                "fix": "over-the-air software update" if r.get("overTheAirUpdate") else "dealer visit",
                "park_outside_warning": r.get("parkOutSide", False),
                "do_not_drive_warning": r.get("parkIt", False),
                "model_years": [],
                "cite": recall_url(c),
            })
            rec["model_years"].append(int(y))
    for rec in by_campaign.values():
        rec["units_affected_all_vehicles"] = _units(rec["campaign"])
    items = sorted(by_campaign.values(), key=lambda r: r["date"])
    return {
        "vehicle": f"{make} {model}", "years_checked": years, "total_recalls": len(items),
        "fixed_over_the_air": sum(r["fix"].startswith("over") for r in items),
        "recalls_per_model_year": {y: sum(int(y) in r["model_years"] for r in items) for y in years},
        "cite_per_model_year": source_urls,
        "recalls": items,
    }


# --- Complaints (bulk file) ---


def _complaint_rows(make: str, model: str, years: list[int]) -> list[dict]:
    ys = {str(y) for y in years}
    return [c for c in _by_vehicle[(norm(make), norm(model))] if not ys or c["model_year"] in ys]


def _example(c: dict, length: int = 300) -> dict:
    return {"odi": c["odi"], "model_year": c["model_year"], "received": c["received"],
            "miles": c["miles"] or None, "components": c["components"],
            "excerpt": c["description"][:length], "cite": complaint_url(c["odi"])}


def complaint_summary(make: str, model: str, years: list[int]) -> dict:
    """Owner complaints: counts by model year and component, crash/fire/injury totals, and the two
    most recent complaints for each of the top components."""
    rows = _complaint_rows(make, model, years)
    components, examples = Counter(), defaultdict(list)
    for c in rows:
        for comp in c["components"].split("; "):
            components[comp] += 1
            examples[comp].append(c)
    return {
        "vehicle": f"{norm(make)} {norm(model)}", "total_complaints": len(rows),
        "per_model_year": dict(sorted(Counter(c["model_year"] for c in rows).items())),
        "cite_per_model_year": {y: complaints_query_url(make, model, y) for y in sorted({c["model_year"] for c in rows})},
        "crashes": sum(c["crash"] == "True" for c in rows), "fires": sum(c["fire"] == "True" for c in rows),
        "injuries": sum(int(c["injured"] or 0) for c in rows), "deaths": sum(int(c["deaths"] or 0) for c in rows),
        "top_components": [
            {"component": comp, "complaints": n,
             "share_pct": round(100 * n / len(rows), 1),
             "recent_examples": [_example(e) for e in sorted(examples[comp], key=lambda e: int(e["odi"]), reverse=True)[:2]]}
            for comp, n in components.most_common(8)
        ],
        "note": "Complaints are unverified owner reports, not failure rates: they aren't divided by how many cars were sold. "
                "Counts come from NHTSA's complaint bulk files; cite a count with its cite_per_model_year link.",
    }


def search_complaints(make: str, model: str, years: list[int], keywords: list[str]) -> dict:
    """Complaints whose text mentions any keyword ('12v', 'loss of power', 'ICCU'), by model year."""
    pats = [re.compile(re.escape(k.strip()), re.I) for k in keywords if k.strip()]
    hits = [c for c in _complaint_rows(make, model, years) if any(p.search(c["description"]) for p in pats)]
    hits.sort(key=lambda c: int(c["odi"]), reverse=True)
    return {
        "vehicle": f"{norm(make)} {norm(model)}", "keywords": keywords, "matching_complaints": len(hits),
        "per_model_year": dict(sorted(Counter(c["model_year"] for c in hits).items())),
        "cite_per_model_year": {y: complaints_query_url(make, model, y) for y in sorted({c["model_year"] for c in hits})},
        "crashes_among_them": sum(c["crash"] == "True" for c in hits),
        "examples": [_example(c, 360) for c in hits[:8]],
    }


# --- Investigations (bulk file) and safety ratings (live API) ---

_INV_TYPES = {"PE": "Preliminary Evaluation", "EA": "Engineering Analysis", "RQ": "Recall Query",
              "DP": "Defect Petition", "AQ": "Audit Query", "RP": "Recall Petition"}


def investigations(make: str, model: str) -> dict:
    """NHTSA defect investigations (ODI) that name this model, newest first."""
    key = f"{norm(make)} {norm(model)}"
    out = []
    for r in INVESTIGATIONS:
        years = sorted({v.split(" ", 1)[0] for v in r["vehicles"].split("; ") if v.split(" ", 1)[1] == key})
        if years:
            out.append({
                "action_number": r["action_number"], "type": _INV_TYPES.get(r["action_number"][:2], r["action_number"][:2]),
                "subject": r["subject"], "component": r["component"],
                "opened": r["opened"], "closed": r["closed"] or "still open",
                "led_to_recall": r["recall_campaign"] or None, "model_years": years,
                "summary": r["summary"][:700], "cite": investigation_url(r["action_number"]),
            })
    return {"vehicle": key, "total": len(out), "investigations": out}


def safety_ratings(make: str, model: str, year: int) -> dict:
    """NHTSA 5-Star crash-test ratings for each variant of the model in that year."""
    variants = []
    for name in _recall_spellings(norm(make), norm(model)):
        variants = _get(f"/SafetyRatings/modelyear/{year}/make/{norm(make)}/model/{name}").get("Results", [])
        if variants:
            break
    out = []
    for v in variants[:4]:
        rows = _get(f"/SafetyRatings/VehicleId/{v['VehicleId']}").get("Results", [])
        if rows:
            r = rows[0]
            out.append({k: r.get(k) for k in (
                "VehicleDescription", "OverallRating", "OverallFrontCrashRating", "OverallSideCrashRating",
                "RolloverRating", "RolloverPossibility", "NHTSAForwardCollisionWarning", "NHTSALaneDepartureWarning")}
                | {"cite": f"{API}/SafetyRatings/VehicleId/{v['VehicleId']}"})
    return {"vehicle": f"{year} {norm(make)} {norm(model)}",
            "variants": out or f"NHTSA hasn't crash-tested the {year} model."}


# --- EPA specs and Washington registrations ---


def specs(make: str, model: str) -> dict:
    """EPA range, efficiency and charge time for every model year and trim."""
    rows = [r for r in EV_MODELS if r["make"].upper() == norm(make) and r["nhtsa_model"] == norm(model)]
    if not rows:  # mixed names, e.g. 'BLAZER EV' is under EPA base 'Blazer' with 'EV' in the trim
        base = norm(model).rsplit(" ", 1)[0]
        rows = [r for r in EV_MODELS if r["make"].upper() == norm(make) and r["nhtsa_model"] == base]
    by_year = defaultdict(list)
    for r in rows:
        by_year[int(r["year"])].append(r)
    if not rows:
        return {"error": f"EPA lists no all-electric {make} {model}."}
    return {"vehicle": f"{norm(make)} {norm(model)}", "by_model_year": {y: [{
        "trim": r["model"], "drive": r["drive"], "range_mi": r["range_mi"], "kwh_per_100mi": r["kwh_per_100mi"],
        "mpge_combined": r["mpge_combined"], "charge_240v_hours": r["charge_240v_hours"], "motor": r["motor"],
        "annual_energy_cost_usd": r["annual_fuel_cost_usd"], "cite": epa_url(r["epa_id"])} for r in trims]
        for y, trims in sorted(by_year.items())}}


def registrations(make: str, model: str) -> dict:
    """Battery-electric registrations in Washington State by model year, and complaints per 1,000
    registered: a rough sense of how common problems are. One state's registrations vs national
    complaints, so it's a proxy for comparing model years, not a failure rate."""
    rows = {r["model_year"]: int(r["registered"]) for r in WA
            if r["make"].upper() == norm(make) and r["nhtsa_model"] == norm(model)}
    per_year = Counter(c["model_year"] for c in _complaint_rows(make, model, []))
    return {
        "vehicle": f"{norm(make)} {norm(model)}",
        "registered_in_wa_by_model_year": rows,
        "national_complaints_per_1000_wa_registered": {
            y: round(1000 * per_year[y] / n, 1) for y, n in sorted(rows.items()) if n >= 50},
        "note": "Washington State registrations only, divided into national complaint counts. Use it to compare "
                "model years of the same car, never as a failure rate.",
        "cite": "https://data.wa.gov/Transportation/Electric-Vehicle-Population-Data/f6w7-q2d2",
    }


if __name__ == "__main__":
    # Smoke test: uv run sources.py
    import json
    import time

    for make, model in [("Ford", "Mach-E"), ("Hyundai", "Ioniq5"), ("Chevy", "Bolt"), ("Chevrolet", "Bolt"),
                        ("Tesla", "Model Y"), ("Chevrolet", "Blazer EV"), ("Volkswagen", "ID4")]:
        t = time.time()
        v = find_vehicle(make, model)
        if "error" in v:
            print(make, model, "->", v["error"][:120])
            continue
        m, ys = v["model"], [int(y) for y in list(v["complaints_by_model_year"])[:3]]
        r = recalls(make, m, ys)
        cs = complaint_summary(make, m, ys)
        print(f"{make} {model} -> {m} | years {ys} | recalls {r['total_recalls']} {r['recalls_per_model_year']} "
              f"| complaints {cs['total_complaints']} {[(c['component'], c['complaints']) for c in cs['top_components'][:3]]} "
              f"| inv {investigations(make, m)['total']} | specs {list(specs(make, m).get('by_model_year', {}))[:3]} "
              f"| per1000 {registrations(make, m)['national_complaints_per_1000_wa_registered']} | {time.time() - t:.1f}s")
    print(search_complaints("Ford", "MUSTANG MACH E", [2021, 2022], ["12v", "12 volt"])["matching_complaints"])
    print(json.dumps(safety_ratings("Ford", "MUSTANG MACH E", 2021))[:300])
