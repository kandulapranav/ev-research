# ⚡ EV Research

**Live:** https://ev-research-git-485071834023.europe-west1.run.app/ · **Path:** Deep Research

## The project and who it's for

EV Research answers one question: **is this EV a good buy, new or used, and which model years
should you avoid?** It's for anyone shopping for an electric car who wants to know which years had
problems, whether they were fixed, and what owners still report, without reading hundreds of
NHTSA records.

A **Lead** agent identifies the vehicle (and asks you to clarify if the question is vague), then
sends four **Researchers** out in parallel: recalls, owner complaints, investigations and crash
ratings, and EPA specs. When the findings leave a gap, such as a complaint pattern with no recall,
it sends follow-up researchers. A **Writer** turns the findings into a verdict where every fact
links to its source, and the app removes any link that a data tool didn't return.

## Where the data comes from

| Source | What it gives | How it's loaded |
|---|---|---|
| **NHTSA Recalls API** (`api.nhtsa.gov/recalls/recallsByVehicle`, `/recalls/campaignNumber`) | Recalls by make, model and year: defect, remedy, over-the-air fix, vehicles affected | Live, per question |
| **NHTSA 5-Star Safety Ratings API** (`api.nhtsa.gov/SafetyRatings`) | Crash-test ratings by model year | Live, per question |
| **NHTSA complaints** (bulk files `COMPLAINTS_RECEIVED_*.zip`, `static.nhtsa.gov/odi/ffdd/cmpl/`) | 26,211 EV owner complaints: component, crash/fire/injury, description | `data/ev_complaints.csv.gz` |
| **NHTSA defect investigations** (`FLAT_INV.zip`) | 60 investigations naming an EV, and whether each led to a recall | `data/ev_investigations.csv` |
| **EPA fueleconomy.gov** (`vehicles.csv`) | Range, efficiency and charge time for 1,661 EV trims | `data/ev_models.csv` |
| **Washington State EV Population Data** (data.wa.gov `f6w7-q2d2`) | 241,840 registered EVs by make, model and year (used to put complaint counts in proportion) | `data/wa_registrations.csv` |

**To load it:** the trimmed files in `data/` (about 7 MB) are in the repo, and the APIs need no
key. To rebuild the files from the original downloads, run `uv run build/fetch_data.py`.
Complaints come from the bulk files because the Complaints API only matches NHTSA's exact model
spelling and returned nothing for every Tesla.

To run the app locally: `gcloud auth application-default login`, then `uv run app.py`
and open http://localhost:8000.

## Sample queries

Each query below has three parts:
- **Expected:** the answer the agent should give.
- **Sources:** the file or API endpoint the answer comes from.
- **A comparison table:** the same model (Gemini 3.5 Flash) with no tools and no system prompt, next to EV Research.

### 1. "Is a used 2021 Ford Mustang Mach-E a safe bet?"

**Expected:**
- **Recalls:** 13 cover the 2021 model year (1 fixed over the air). The worst is the
  high-voltage battery contactor, which can cut drive power: **22V-412** (48,924 vehicles),
  **23V-687** and **25V-130**.
- **Complaints:** **353** from owners (149 electrical, 15 crashes, 1 fire).
- **Investigations:** **4**, with **EA25001** (BlueCruise collisions) still open.
- **Range:** **211–305 mi** depending on trim.

**Sources:** NHTSA Recalls API (`recallsByVehicle`, `campaignNumber`),
`data/ev_complaints.csv.gz`, `data/ev_investigations.csv`, `data/ev_models.csv`.

| Gemini 3.5 Flash, no tools | EV Research |
|---|---|
| Calls it a "very safe bet" with the recalls done. Gets the contactor and glass-bonding recalls right in general, but cites **"23V-865"**, which isn't one of this car's recalls. No recall or complaint counts, and no mention of the open BlueCruise investigation. | **Buy with caution; avoid 2021.** Lists the 13 recalls, 353 complaints, owner reports of sudden power loss and open investigation EA25001, each linked to its NHTSA record. |

### 2. "Hyundai Ioniq 5: which model years should I avoid buying used?"

**Expected:**
- **The main problem:** the ICCU charging unit can fail and cut drive power. It's covered by
  recalls **24V-204** (99,055 vehicles) and **24V-868** (145,642) for **2022, 2023 and 2024**,
  after investigation **PE23011**.
- **Complaints by year:** **2022** is worst, with **803 complaints per 1,000** cars registered in
  Washington, against 192 for 2023 and 109 for 2024.
- **2025** has the most recalls (9), mostly minor.

**Sources:** NHTSA Recalls API, `data/ev_complaints.csv.gz` + `data/wa_registrations.csv`,
`data/ev_investigations.csv`.

| Gemini 3.5 Flash, no tools | EV Research |
|---|---|
| Says avoid 2022, and calls **2024 "the safest bet"** because the ICCU issue was "largely addressed." Both ICCU recalls cover 2024. Also cites a "Recall 257" that isn't an NHTSA campaign. | **Avoid 2022–2024 unless the ICCU recall work is done; best year 2025.** Notes that 2025 still has 9 recalls and ICCU complaints, with links to the complaint records. |

### 3. "What's the history of the Chevy Bolt EV battery fire recall? Are 2022-23 Bolts affected?"

**Expected:** The recalls came in this order:
- **PE20016** (2017–2020 models), the investigation into battery fires.
- **20V-701** (Nov 2020): 2017–19 models, 50,932 vehicles.
- **21V-560** (Jul 2021): 2017–19 models, 57,414 vehicles.
- **21V-650** (Aug 2021): 2020–22 models, 52,414 vehicles.
- **24V-481** and **24V-812** (2024): follow-ups for 2020–22.

**2022 Bolts are covered. 2023 Bolts aren't in any battery recall.**

**Sources:** NHTSA Recalls API, `data/ev_investigations.csv`.

| Gemini 3.5 Flash, no tools | EV Research |
|---|---|
| Gets the overall story and the outcome right (2022 affected, 2023 not), but the numbers are wrong: "approximately 69,000" in the first recall (NHTSA says 50,932). Doesn't know about the 2024 follow-up recalls. | **2022 affected, 2023 not.** Gives every campaign with its model years and vehicle count, each linked to NHTSA. The Lead sent 3 follow-up researchers to check the 2022–23 Bolt EUV, the 2023 Bolt EV and the 2020–22 software remedy. |

**What the comparison shows:** the model already knows the headline story for famous recalls, so
the agent's value is in the specifics it gets wrong or doesn't have. That means exact campaign
numbers and vehicle counts, which years each recall covers, complaint counts and open
investigations, each with a source link.

**What the data can't answer:** true failure rates (complaints aren't divided by sales), battery
degradation, repair costs and resale value. The agent says so when asked.
