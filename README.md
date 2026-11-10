# ⚡ EV Research

**Is this EV a good buy, new or used, and which model years should you avoid?**

EV Research is a deep research agent for people shopping for an electric car. Name an EV
and the model years you're looking at. A **Lead** agent resolves the vehicle and asks you to
clarify if the question is vague, then sends four **Researchers** out in parallel, each digging
through a different federal dataset. It reads what they find and sends follow-up researchers when
something doesn't add up (owners complaining about a part that was never recalled). A **Writer**
turns the findings into a buyer's report with a verdict, where every fact links to the recall,
complaint, investigation or EPA record it came from.

**Who it's for:** anyone buying a new or used EV who wants to know which model years had
problems, whether those problems were fixed, and what owners still report, without reading
hundreds of NHTSA records themselves.

**Live:** https://REPLACE-ME.run.app · **Path:** Deep Research

## The research question

**Main question:** Is this EV a good buy, new or used, and which model years should you avoid?

| Branch question | Researcher | Data |
|---|---|---|
| What has been officially recalled, how many cars did each recall cover, and was it fixed over the air or at a dealer? | 🛠️ Recalls | NHTSA Recalls API (live) |
| What do owners keep reporting, by component and model year, and what was never recalled? | 💬 Owner Complaints | NHTSA complaints (bulk files) + WA registrations |
| Is NHTSA investigating this model, did past investigations lead to recalls, and how did it crash-test? | 🔍 Investigations & Safety | NHTSA investigations (bulk file) + 5-Star Safety Ratings API (live) |
| How did range, efficiency and charge time change across model years? | 🔋 Specs & Ownership | EPA fueleconomy.gov |

## How it works

```
                         ┌──────────────────────────────────────────────┐
 question ──► Lead ──────┤ find_vehicle → vague? reply CLARIFY: …        │
                         │ research() ×4 in ONE turn  → run in parallel   │
                         └───┬───────────┬───────────┬───────────┬───────┘
                             ▼           ▼           ▼           ▼
                        🛠️ Recalls  💬 Complaints  🔍 Investig.  🔋 Specs    (fresh contexts,
                             └───────────┴─────┬─────┴───────────┘          own tools each)
                                               ▼
                         Lead reads findings → gap? research() again (≤ 2 rounds, ≤ 8 researchers)
                                               ▼
                         Writer (sees only question + findings) → cited report
                                               ▼
                         Citation check: every link must be a URL a data tool returned
```

- **Three agent shapes with different jobs** (`app.py`): the **Lead** plans, clarifies and fills
  gaps; four **Researcher** specialists each have their own tools and start from a fresh context;
  the **Writer** sees only the question and findings.
- **Parallel fan-out:** the Lead calls `research(beat, task, …)` once per beat in the same turn,
  and the Agents SDK runs those tool calls concurrently. The page shows a card per researcher as
  it works.
- **Going back for more:** after the first round, the Lead looks for gaps (a complaint cluster
  with no matching recall, an investigation with no outcome) and sends follow-up researchers. In
  the Bolt trace, the first round found the 2017–22 battery recalls, so the Lead sent three more
  recall researchers to check the 2022–23 Bolt EUV, the 2023 Bolt EV, and the 2020–22 diagnostic
  software remedy. Capped at 8 researchers per question in code, not just in the prompt.
- **Clarification:** if the question doesn't name one specific EV ("Are Teslas reliable?",
  "Should I buy an electric SUV?") or `find_vehicle` returns several candidates ("Chevy Bolt":
  the 2027 Bolt, Bolt EV or Bolt EUV?), the Lead replies `CLARIFY: …`. The app returns that
  question, remembers the original, and folds your answer into it on the next message.
- **Accurate citations, enforced in code:** every data record carries a `cite` URL (a recall's
  campaign record, a complaint's ODI record, an EPA vehicle page). Researchers and the Writer are
  told to copy URLs, never build them. Then `check_citations()` compares every link in the report
  against the URLs the data tools actually returned, and strips any that don't match. (Early
  testing caught the Writer inventing `…/complaints/get_complaint_summary?…` links; this check is
  why it can't anymore.)

### `/chat` shape

`POST /chat {"message", "session_id"}` → `{"response", "session_id", "tool_calls"}`, the same
shape as Project 1. Every tool call has an `agent` field, including the sub-agents' own calls:

```json
{"agent": "Recalls #1", "name": "get_recalls", "args": {"make": "FORD", "model": "MUSTANG MACH E", "model_years": [2021]}, "result": "{...}"}
```

`GET /run?message=…&session_id=…` runs the same thing and streams it as Server-Sent Events,
which the page uses for the live research board.

## Where the data comes from

| Source | What it gives | How it's loaded |
|---|---|---|
| **NHTSA Recalls API** (`api.nhtsa.gov/recalls/recallsByVehicle`, `/recalls/campaignNumber`) | Every recall by make, model, year: component, defect, consequence, remedy, over-the-air flag, units affected | Live, per question |
| **NHTSA complaints** (bulk files `COMPLAINTS_RECEIVED_2010-2014` … `2025-2026`, `static.nhtsa.gov/odi/ffdd/cmpl/`) | 26,211 owner complaints about EVs: component, crash/fire/injury, mileage, the owner's description | `build/fetch_data.py` → `data/ev_complaints.csv.gz` |
| **NHTSA defect investigations** (`FLAT_INV.zip`) | 60 investigations naming an EV: type (PE/EA/RQ/DP), subject, opened/closed, resulting recall | `build/fetch_data.py` → `data/ev_investigations.csv` |
| **NHTSA 5-Star Safety Ratings API** | Crash-test ratings per model year and variant | Live, per question |
| **EPA fueleconomy.gov** (`vehicles.csv`) | Range, kWh/100 mi, MPGe, 240V charge time, motor, by year and trim, for 1,661 EV trims | `build/fetch_data.py` → `data/ev_models.csv` |
| **WA State EV Population Data** (data.wa.gov `f6w7-q2d2`) | 241,840 battery-electric vehicles registered in Washington, by make/model/year | `build/fetch_data.py` → `data/wa_registrations.csv` |

To rebuild the offline files: `uv run build/fetch_data.py` (downloads about 250 MB into
`data/raw/`, which is gitignored; the trimmed files in `data/` are about 7 MB and ship with the deploy).

**Why complaints come from the bulk files instead of the Complaints API:** the API only answers
to NHTSA's exact model text, which varies (`MODEL Y 7-SEAT`, `MUSTANG MACH-E` vs the recalls
API's `MUSTANG MACH E`), and its list of valid model names is truncated. In testing it returned
zero complaints for the 2023 Mach-E and every Model Y, while the bulk files have 129 and 4,359.
The build maps every variant to one name per EV (`canonical()` in `build/fetch_data.py`), and
filters out gas models that share a name with an EV (Blazer vs Blazer EV, Cayenne E-Hybrid).

## Sample queries

For each query: the answer you should get, where it comes from, and the same model's answer with
no tools and no system prompt next to EV Research's. Full answers and every tool call are in
[`traces/`](traces/) (regenerate with `uv run scripts/compare.py`).

### 1. "Is a used 2021 Ford Mustang Mach-E a safe bet?"

**Expected:** 13 recalls cover the 2021 model year, 12 of them fixed at a dealer and 1 over the
air. The worst is the high-voltage battery contactor (loss of drive power): **22V-412** (48,924
vehicles), **23V-687**, **25V-130**. Others: windshield (21V-711) and panoramic roof (21V-712)
bonding, rear differential pinion shaft (26V-417). Owners filed **353 complaints** (149
electrical, 15 crashes, 1 fire). NHTSA has **4 investigations**, one still open: **EA25001**,
"Collisions Involving Ford BlueCruise". Recall query **RQ23004** on the contactor recall ended in
recall 23V-687. EPA range for 2021: **211–305 mi** by trim.
**Sources:** NHTSA Recalls API (`recallsByVehicle`, `campaignNumber`), `data/ev_complaints.csv.gz`,
`data/ev_investigations.csv`, `data/ev_models.csv`.

| Gemini 3.5 Flash, no tools | EV Research |
|---|---|
| Calls it a "very safe bet" with recalls done. Gets the general story right (contactor "HVBJB" recall, glass bonding), but cites recall **"23V-865 or 22V-412"** (23V-865 isn't one of this car's recalls; the follow-up was 23V-687). Never mentions how many recalls or complaints there are, or the open BlueCruise investigation. | **Buy with caution, avoid 2021.** 13 recalls, 353 complaints, sudden power loss in owner reports, the open BlueCruise investigation EA25001, each linked. After the first round, the Lead sent two follow-ups: the status of EA25001, and the details of contactor recalls 23V-687 and 25V-130. |

### 2. "Hyundai Ioniq 5: which model years should I avoid buying used?"

**Expected:** The ICCU (charging-unit) failure that kills the 12V battery and cuts drive power is
covered by **24V-204** (99,055 vehicles) and **24V-868** (145,642), for **2022, 2023 and 2024**,
after investigation **PE23011** "Loss of Motive Power". **2022** has the most complaints per car:
**803 per 1,000** Washington-registered, vs 192 for 2023 and 109 for 2024. Owners mention the
ICCU in **213** complaints about 2022 and **193** about 2025. **2025** has the most recalls (9),
mostly small (brakes on the N, headlights, a few traction-battery campaigns).
**Sources:** NHTSA Recalls API, `data/ev_complaints.csv.gz` + `data/wa_registrations.csv`,
`data/ev_investigations.csv`.

| Gemini 3.5 Flash, no tools | EV Research |
|---|---|
| Avoid 2022, and calls **2024 "the safest bet"** because "the ICCU issues were largely addressed at the factory." The recall data says otherwise: both ICCU recalls cover 2024. Cites a "Recall 257" that isn't an NHTSA campaign. | **Avoid 2022–2024 unless the ICCU recall work is done; best year 2025**, while noting that 2025 has 9 recalls and owners still report ICCU failures on it (with ODI complaint links). |

### 3. "What's the history of the Chevy Bolt EV battery fire recall? Are 2022-23 Bolts affected?"

**Expected:** Investigation **PE20016** "Electric Vehicle Battery Fires" (2017–2020 models) led to
recall **20V-701** (Nov 2020, 2017–19, 50,932 vehicles, park outside). Then **21V-560** (Jul 2021,
2017–19, 57,414) and **21V-650** (Aug 2021, 2020–22, 52,414), both with park-outside warnings.
Two small follow-ups in 2024 (**24V-481**, **24V-812**) also cover 2020–22. **2022 Bolts are
covered; 2023 Bolts are in none of the battery recalls** (their recalls are roof-rail and airbag).
**Sources:** NHTSA Recalls API, `data/ev_investigations.csv`.

| Gemini 3.5 Flash, no tools | EV Research |
|---|---|
| A good narrative that matches the outcome (2022 affected, 2023 not), plus context the data doesn't have (LG's $1.9B payment, the cell defect). But the numbers are off: "approximately 69,000" in the first recall (NHTSA: 50,932) and ~141,000 in all, and it doesn't know about the 2024 follow-up recalls. | **2022 affected, 2023 not**, with each campaign, its model years and vehicle counts linked to the NHTSA record. Sent 7 researchers: 4 in parallel, then 3 recall follow-ups on the 2022–23 Bolt EUV, the 2023 Bolt EV and the 2020–22 remedy. |

**What the comparison shows:** the model already knows the headline story for famous recalls
(Bolt fires, Ioniq ICCU), so the agent's value is in the specifics the model gets wrong or
doesn't have: exact campaign numbers and vehicle counts, which model years each recall covers,
complaint counts by year, open investigations, and a link for every one.

## What it can't answer (and says so)

- **Failure rates.** Complaints are unverified and aren't divided by sales. The agent can show
  complaints per 1,000 cars registered in Washington, labeled as a rough proxy for comparing model
  years of the same car, never as a national failure rate.
- **Battery degradation, repair costs, resale value, insurance.** Not in any of these sources.
- **Crash ratings NHTSA hasn't published** (many EVs aren't crash-tested every model year).
- **Gas cars, hybrids and plug-in hybrids.** The data is filtered to battery-electric vehicles.

## Run it

```bash
gcloud auth application-default login
uv run app.py                 # http://localhost:8000
uv run scripts/compare.py     # regenerate traces/: no tools vs EV Research
```

## Deploy (Cloud Run, continuous deploy from GitHub)

Connect the repo in Cloud Run → *Continuously deploy from a repository* (buildpacks; the
`Procfile` runs `python app.py`, which listens on `$PORT`). Set the **request timeout to 600 s**
(a full run takes 1.5–2.5 minutes), **memory to 1 GiB**, and give the service account the
**Vertex AI User** role.
