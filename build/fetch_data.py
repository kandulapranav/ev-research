"""The offline step: download the bulk files once and trim them to EVs.

The live NHTSA APIs (recalls, safety ratings) are called at question time. These sources are
bulk downloads, too big or too slow to fetch per question, so this script trims them to EVs and
writes small files in data/ that ship with the deploy:

  data/ev_models.csv           EPA fueleconomy.gov: every all-electric vehicle, by year and trim
  data/ev_complaints.csv.gz    NHTSA owner complaints about those EVs, 2010 onward
  data/ev_investigations.csv   NHTSA defect investigations (ODI) that touch those EVs
  data/wa_registrations.csv    Washington State: battery-electric vehicles registered, by model year

Why complaints come from the bulk files, not the Complaints API: NHTSA files complaints under
variant names ('MODEL Y 7-SEAT', 'MODEL Y  5-SEAT', 'MODEL S (ALL VARIANTS)') that the API only
answers to exactly, and its list of valid names is truncated. The bulk files have every row, and
canonical() maps each variant to one name per vehicle.

    uv run build/fetch_data.py
"""

import csv
import gzip
import io
import zipfile
from pathlib import Path

import httpx

DATA = Path(__file__).resolve().parent.parent / "data"
RAW = DATA / "raw"

EPA_URL = "https://www.fueleconomy.gov/feg/epadata/vehicles.csv.zip"
INV_URL = "https://static.nhtsa.gov/odi/ffdd/inv/FLAT_INV.zip"
CMPL_URL = "https://static.nhtsa.gov/odi/ffdd/cmpl/COMPLAINTS_RECEIVED_{span}.zip"
CMPL_SPANS = ["2010-2014", "2015-2019", "2020-2024", "2025-2026"]
WA_URL = "https://data.wa.gov/resource/f6w7-q2d2.json"

# FLAT_INV.txt has no header. Field order from NHTSA's INV.txt data dictionary.
INV_FIELDS = ["action_number", "make", "model", "year", "component", "manufacturer",
              "opened", "closed", "recall_campaign", "subject", "summary"]


def norm(model: str) -> str:
    """NHTSA spells models in capitals without hyphens: 'Mustang Mach-E' -> 'MUSTANG MACH E'."""
    return " ".join(model.upper().replace("-", " ").split())


def download(url: str, name: str) -> Path:
    RAW.mkdir(parents=True, exist_ok=True)
    out = RAW / name
    if not out.exists():
        print(f"downloading {url}")
        with httpx.stream("GET", url, timeout=300, follow_redirects=True) as r:
            r.raise_for_status()
            with out.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
    return out


# --- 1. EPA: every all-electric vehicle ---

with zipfile.ZipFile(download(EPA_URL, "vehicles.csv.zip")) as z:
    rows = list(csv.DictReader(io.TextIOWrapper(z.open("vehicles.csv"), encoding="utf-8")))

ev_rows = []
for r in rows:
    if r["atvType"] != "EV" or int(r["year"]) < 2011:
        continue
    ev_rows.append({
        "epa_id": r["id"],
        "year": int(r["year"]),
        "make": r["make"],
        "model": r["model"],
        "base_model": r["baseModel"],
        "nhtsa_model": norm(r["baseModel"]),
        "drive": r["drive"],
        "vehicle_class": r["VClass"],
        "range_mi": r["range"] or r["rangeA"],
        "kwh_per_100mi": r["combE"],
        "mpge_combined": r["comb08"],
        "mpge_city": r["city08"],
        "mpge_highway": r["highway08"],
        "charge_240v_hours": r["charge240"],
        "motor": r["evMotor"],
        "annual_fuel_cost_usd": r["fuelCost08"],
    })
ev_rows.sort(key=lambda r: (r["make"], r["base_model"], r["year"], r["model"]))
DATA.mkdir(exist_ok=True)
with (DATA / "ev_models.csv").open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(ev_rows[0]))
    w.writeheader()
    w.writerows(ev_rows)
ev_keys = {(r["make"].upper(), r["nhtsa_model"]) for r in ev_rows}
print(f"ev_models.csv: {len(ev_rows)} EV trims, {len(ev_keys)} make/models")

# EPA files some EVs under a base model they share with gas cars ('Blazer' = Blazer and Blazer EV).
# Those "mixed" names only count when the NHTSA model also says it's electric.
mixed = {(r["make"].upper(), norm(r["baseModel"])) for r in rows if r["atvType"] != "EV"} & ev_keys
EV_WORDS = {"EV", "ELECTRIC", "LIGHTNING", "EUV", "E TRON", "MACH E", "BEV"}
BASES = {}  # make -> its EV base models, longest first, so 'IONIQ 5' wins over 'IONIQ'
for make, base in ev_keys:
    BASES.setdefault(make, []).append(base)
for make in BASES:
    BASES[make].sort(key=len, reverse=True)


def canonical(make: str, model: str) -> str | None:
    """One name per EV for every NHTSA spelling of it, or None if it isn't an EV.
    'MODEL Y 7 SEAT' -> 'MODEL Y', 'MUSTANG MACH E' -> 'MUSTANG MACH E', 'MUSTANG' -> None,
    'BLAZER EV' -> 'BLAZER EV', 'BLAZER' -> None, 'CAYENNE E HYBRID' -> None."""
    if "HYBRID" in model or "PHEV" in model or "PLUG IN" in model:
        return None
    for base in BASES.get(make, []):
        if model != base and not model.startswith(base + " "):
            continue
        if (make, base) not in mixed:
            return base
        if any(f" {w} " in f" {model[len(base):]} " for w in EV_WORDS):
            return model
    return None


# --- 2. NHTSA investigations that touch an EV model ---

inv: dict[str, dict] = {}
with zipfile.ZipFile(download(INV_URL, "FLAT_INV.zip")) as z:
    name = next(n for n in z.namelist() if n.upper().endswith(".TXT"))
    for line in io.TextIOWrapper(z.open(name), encoding="utf-8", errors="replace"):
        parts = line.rstrip("\r\n").split("\t")
        if len(parts) < len(INV_FIELDS):
            continue
        r = dict(zip(INV_FIELDS, parts))
        make = r["make"].strip().upper()
        model = canonical(make, norm(r["model"]))
        if not model:
            continue
        key = r["action_number"].strip()
        rec = inv.setdefault(key, {**{k: r[k].strip() for k in INV_FIELDS}, "vehicles": set()})
        rec["vehicles"].add(f"{r['year'].strip()} {make} {model}")

with (DATA / "ev_investigations.csv").open("w", newline="") as f:
    fields = [k for k in INV_FIELDS if k not in ("make", "model", "year")] + ["vehicles"]
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    for rec in sorted(inv.values(), key=lambda r: r["opened"], reverse=True):
        w.writerow({**{k: rec[k] for k in fields if k != "vehicles"}, "vehicles": "; ".join(sorted(rec["vehicles"]))})
print(f"ev_investigations.csv: {len(inv)} investigations")

# --- 3. NHTSA owner complaints about those EVs ---

# CMPL.txt fields used here (no header row): 1 ODI number, 3 make, 4 model, 5 model year,
# 6 crash, 7 failure date, 8 fire, 9 injured, 10 deaths, 11 component, 13 state, 15 date received,
# 17 miles, 19 description.
complaints: dict[str, dict] = {}
for span in CMPL_SPANS:
    with zipfile.ZipFile(download(CMPL_URL.format(span=span), f"CMPL_{span}.zip")) as z:
        for line in io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8", errors="replace"):
            p = line.rstrip("\r\n").split("\t")
            if len(p) < 20:
                continue
            make = p[3].strip().upper()
            model = canonical(make, norm(p[4]))
            if not model:
                continue
            c = complaints.setdefault(p[1], {
                "odi": p[1], "make": make, "model": model, "model_variant": p[4].strip(), "model_year": p[5],
                "received": p[15], "failed": p[7], "crash": p[6] == "Y", "fire": p[8] == "Y",
                "injured": p[9] or "0", "deaths": p[10] or "0", "miles": p[17], "state": p[13],
                "components": set(), "description": p[19][:1200],
            })
            c["components"].add(p[11].strip())  # one row per component: collect them

with gzip.open(DATA / "ev_complaints.csv.gz", "wt", newline="") as f:
    fields = ["odi", "make", "model", "model_variant", "model_year", "received", "failed", "crash", "fire",
              "injured", "deaths", "miles", "state", "components", "description"]
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    for c in sorted(complaints.values(), key=lambda c: int(c["odi"])):
        w.writerow({**c, "components": "; ".join(sorted(c["components"]))})
print(f"ev_complaints.csv.gz: {len(complaints):,} complaints")

# --- 4. Washington State: battery-electric registrations by make, model, model year ---

params = {
    "$select": "make, model, model_year, count(*) AS registered",
    "$where": "ev_type = 'Battery Electric Vehicle (BEV)'",
    "$group": "make, model, model_year",
    "$limit": 50000,
}
r = httpx.get(WA_URL, params=params, timeout=120)
r.raise_for_status()
wa = sorted(r.json(), key=lambda x: (x["make"], x["model"], x["model_year"]))
with (DATA / "wa_registrations.csv").open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["make", "model", "nhtsa_model", "model_year", "registered"])
    w.writeheader()
    for x in wa:
        w.writerow({"make": x["make"], "model": x["model"], "nhtsa_model": norm(x["model"]),
                    "model_year": x["model_year"], "registered": x["registered"]})
print(f"wa_registrations.csv: {len(wa)} make/model/year rows, {sum(int(x['registered']) for x in wa):,} BEVs")
