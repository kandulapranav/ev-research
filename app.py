"""EV Research: deep research on whether an electric vehicle is a good buy.

The deep research pattern from wikipedia-deep-research, pointed at vehicle-safety data:

  Lead        resolves the vehicle, asks the user to clarify if the question is vague, then calls
              `research` once per branch in the same turn so the Researchers run in parallel.
              It reads their findings and sends more Researchers when something doesn't add up
              (a complaint cluster with no recall), up to a cap.
  Researchers four specialists, each with its own tools and a fresh context:
              Recalls, Owner Complaints, Investigations & Safety, Specs & Ownership.
  Writer      sees only the question and the findings, and writes a cited buyer's report.

Endpoints: POST /chat (the Project 1 shape: response, session_id, tool_calls with an agent field)
and GET /run (the same run, streamed to the page as Server-Sent Events).
"""

import asyncio
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from agents import Agent, ModelSettings, RunContextWrapper, Runner, RunHooks, function_tool, set_tracing_disabled
from agents.extensions.models.litellm_model import LitellmModel
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import sources

load_dotenv(Path(__file__).parent / ".env")
# Traces upload to OpenAI by default, which needs an OpenAI key. We're on Gemini.
set_tracing_disabled(True)

# LiteLLM routes the call to Gemini. api_key is unused: Vertex AI uses your gcloud credentials.
MODEL = LitellmModel(model="vertex_ai/gemini-3.5-flash", api_key="unused")
# Researchers read data and take notes: the cheaper model is enough.
RESEARCHER_MODEL = LitellmModel(model="vertex_ai/gemini-3.5-flash-lite", api_key="unused")
# A stalled model call would hang the page: give up after 90 s, and retry twice.
SETTINGS = ModelSettings(extra_args={"vertex_location": "global", "timeout": 90, "num_retries": 2})

MAX_RESEARCHERS = 8  # in all, per question. Prompts ask for restraint; code enforces it.
MAX_RESULT_CHARS = 6000  # per tool result in /chat's tool_calls, so the response stays readable


@dataclass
class Research:
    """One run's state, shared by every agent and tool in it. The model never sees it."""

    emit: callable
    researchers: int = 0
    findings: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    raw_results: list[str] = field(default_factory=list)  # untruncated, for the citation check


# --- Data tools (sources.py does the work; these run it off the event loop) ---


async def _run(fn, *args) -> str:
    try:
        return json.dumps(await asyncio.to_thread(fn, *args), default=str)
    except Exception as e:
        # The model cannot see an exception. Return something it can reason about.
        return json.dumps({"error": f"{type(e).__name__}: {str(e)[:300]}. Try again or report the gap."})


@function_tool
async def find_vehicle(make: str, model: str) -> str:
    """Resolve the EV the user named ('Mach-E', 'Ioniq 5', 'Bolt') to the make and model name every
    other tool expects, with the model years that have data. If several EVs match, returns candidates.

    Args:
        make: The brand, e.g. 'Ford', 'Chevy', 'Tesla'.
        model: The model as the user said it, e.g. 'Mach-E'.
    """
    return await _run(sources.find_vehicle, make, model)


@function_tool
async def get_recalls(make: str, model: str, model_years: list[int]) -> str:
    """NHTSA recalls for those model years: campaign number, component, defect, whether the fix was
    an over-the-air update or a dealer visit, and how many vehicles each recall covered (live API).

    Args:
        make: Make from find_vehicle, e.g. 'FORD'.
        model: Model from find_vehicle, e.g. 'MUSTANG MACH E'.
        model_years: Model years to check, e.g. [2021, 2022, 2023].
    """
    return await _run(sources.recalls, make, model, model_years)


@function_tool
async def get_complaint_summary(make: str, model: str, model_years: list[int]) -> str:
    """NHTSA owner complaints: totals per model year, the most-complained-about components with two
    recent examples each, and crash, fire and injury counts.

    Args:
        make: Make from find_vehicle.
        model: Model from find_vehicle.
        model_years: Model years to include.
    """
    return await _run(sources.complaint_summary, make, model, model_years)


@function_tool
async def search_complaints(make: str, model: str, model_years: list[int], keywords: list[str]) -> str:
    """Find complaints whose text mentions any keyword, e.g. ['12v', '12 volt'] or ['loss of power',
    'lost power']. Returns counts per model year and up to 8 examples.

    Args:
        make: Make from find_vehicle.
        model: Model from find_vehicle.
        model_years: Model years to include.
        keywords: Words or phrases to search for, case-insensitive.
    """
    return await _run(sources.search_complaints, make, model, model_years, keywords)


@function_tool
async def get_investigations(make: str, model: str) -> str:
    """NHTSA defect investigations that name this model: type, subject, when opened and closed,
    and the recall it led to, if any.

    Args:
        make: Make from find_vehicle.
        model: Model from find_vehicle.
    """
    return await _run(sources.investigations, make, model)


@function_tool
async def get_safety_ratings(make: str, model: str, model_year: int) -> str:
    """NHTSA 5-Star crash-test ratings for one model year (live API).

    Args:
        make: Make from find_vehicle.
        model: Model from find_vehicle.
        model_year: One model year.
    """
    return await _run(sources.safety_ratings, make, model, model_year)


@function_tool
async def get_specs(make: str, model: str) -> str:
    """EPA range, efficiency (kWh/100 mi, MPGe), 240V charge time and yearly energy cost for every
    model year and trim.

    Args:
        make: Make from find_vehicle.
        model: Model from find_vehicle.
    """
    return await _run(sources.specs, make, model)


@function_tool
async def get_complaint_rate(make: str, model: str) -> str:
    """Washington State registrations by model year, and national complaints per 1,000 registered:
    a rough way to compare model years of the same car. Not a failure rate.

    Args:
        make: Make from find_vehicle.
        model: Model from find_vehicle.
    """
    return await _run(sources.registrations, make, model)


# --- The Researchers: one per branch question, each with its own tools ---

CITE_RULES = """\
Reply with the facts that answer your task and nothing else, at most 10 bullets. Every bullet
states numbers and ends with a markdown link labeled with the record's ID, e.g. ([22V-412](url)),
([ODI 11456789](url)), ([EPA](url)), ([PE24012](url)), ([2021 complaints](url)).
- Copy every URL exactly from a `cite` or `cite_per_model_year` field in the tool results. Never
  build or guess a URL: a fact with no URL in the results gets no link.
- Use only what the tools returned. If the data doesn't answer part of the task, say so in a last bullet."""

BEATS = {
    "recalls": Agent(
        name="Recalls",
        instructions=f"""You research a vehicle's NHTSA recalls. Call get_recalls for the model
years in your task. Report: how many recalls per model year, the most serious ones (do-not-drive
or park-outside warnings, fire, loss of power, crash risk), how many vehicles each covered, and how
many were fixed over the air vs at a dealer. Note which model years had the most.

{CITE_RULES}""",
        tools=[get_recalls],
    ),
    "complaints": Agent(
        name="Owner Complaints",
        instructions=f"""You research what owners report to NHTSA about a vehicle. Call
get_complaint_summary first. Then use search_complaints for the 1-2 biggest specific problems you
see in the examples (e.g. '12v', 'loss of power', 'ICCU', 'phantom braking') to count them by model
year. Call get_complaint_rate to compare model years per 1,000 registered. Report the top problems,
which model years got the most, and quote short owner excerpts.

{CITE_RULES}""",
        tools=[get_complaint_summary, search_complaints, get_complaint_rate],
    ),
    "investigations": Agent(
        name="Investigations & Safety",
        instructions=f"""You research NHTSA defect investigations and crash-test ratings for a
vehicle. Call get_investigations, and get_safety_ratings for 1-2 of the model years in your task.
For each investigation report its subject, whether it's still open, the model years, and the recall
it led to. If your task asks whether a problem was ever recalled, call get_recalls to check.

{CITE_RULES}""",
        tools=[get_investigations, get_safety_ratings, get_recalls],
    ),
    "specs": Agent(
        name="Specs & Ownership",
        instructions=f"""You research what a vehicle delivers, from EPA data. Call get_specs.
Report how range, efficiency and 240V charge time changed across model years and trims, and which
model year brought the biggest improvement. Compare the base and longest-range trims.

{CITE_RULES}""",
        tools=[get_specs],
    ),
}
for _agent in BEATS.values():
    _agent.model, _agent.model_settings = RESEARCHER_MODEL, SETTINGS


@function_tool
async def research(ctx: RunContextWrapper[Research], beat: str, task: str, make: str, model: str,
                   model_years: list[int]) -> str:
    """Send a researcher to answer one task. Researchers run in parallel when you call this several
    times in the same turn. Returns its findings, each with a citation.

    Args:
        beat: Which researcher: 'recalls', 'complaints', 'investigations' or 'specs'.
        task: One specific thing to find out.
        make: Make from find_vehicle.
        model: Model from find_vehicle.
        model_years: The model years this task covers.
    """
    state = ctx.context
    if beat not in BEATS:
        return f"Unknown beat '{beat}'. Use one of {list(BEATS)}."
    if state.researchers >= MAX_RESEARCHERS:
        return "No researchers left. Reply DONE."
    # What researcher.as_tool() would do: run it in a fresh context and return its answer. Written
    # out so each run gets a numbered name of its own, and its own card on the page.
    state.researchers += 1
    agent = BEATS[beat].clone(name=f"{BEATS[beat].name} #{state.researchers}")
    state.emit({"type": "researcher", "agent": agent.name, "beat": beat, "task": task})
    prompt = f"Vehicle: {make} {model}\nModel years: {model_years}\nTask: {task}"
    result = await Runner.run(agent, prompt, context=state, hooks=Hooks(state), max_turns=10)
    state.findings.append(f"## {agent.name}\nTask: {task}\n\n{result.final_output}")
    state.emit({"type": "researcher_done", "agent": agent.name})
    return str(result.final_output)


# --- The Lead: clarifies, plans, fans out, checks for gaps ---

lead = Agent[Research](
    name="Lead",
    instructions=f"""\
You lead a research team that answers: is this electric vehicle a good buy, new or used, and which
model years should you avoid? Your team: 'recalls', 'complaints', 'investigations' and 'specs'
researchers. You have {MAX_RESEARCHERS} researchers in all.

1. Find the vehicle. If the question names an EV, call find_vehicle. If it doesn't name one
   specific EV ("are EVs reliable?", "is Tesla good?", "should I get an electric SUV?"), or
   find_vehicle returns several candidates and the question doesn't say which, or it's not about
   an EV at all, reply with exactly one line: CLARIFY: <one short question>, and nothing else.
   Example: "CLARIFY: Which Tesla, and which model year (or are you open to any)?"
2. Pick the model years: the ones the user asked about, otherwise every year with data.
3. Fan out: call research once per beat, all four in the same turn, so they run in parallel.
   Give each a specific task that serves the user's question.
4. Read the findings. Look for gaps: an owner-complaint cluster (e.g. '12V battery', 'loss of
   power') with no matching recall, or an investigation with no outcome. Send 1-2 follow-up
   researchers for just those gaps, e.g. beat 'investigations' with task "Was the 12V battery
   failure in 2021-22 ever recalled or investigated?". Do this at most twice.
5. When the question is covered, reply DONE and nothing else: a writer writes the report.""",
    model=MODEL,
    model_settings=SETTINGS,
    tools=[find_vehicle, research],
)

# --- The Writer: one call, after the research ends ---

writer = Agent(
    name="Writer",
    instructions="""\
Write a buyer's report in markdown that answers the question, using only the findings you're given.

The first line is exactly: VERDICT: <Buy | Buy with caution | Avoid> | Best years: <years or "n/a"> | Avoid: <years or "none">
Then these sections, each 2-5 sentences or bullets:
## The short answer
## Recalls
## What owners report
## Investigations & crash tests
## Range & specs by model year
## What this data can't tell you

- Keep the links: end every sentence that states a fact with the link(s) from its finding, with the
  same labels, e.g. ([22V-412](...)). Always write links as [label](url), never a bare URL. Copy
  URLs exactly as they appear in the findings. Never write a URL that isn't in the findings, and
  never invent a number.
- Complaints are unverified reports and aren't divided by sales, so never call them failure rates.
- If the findings don't answer part of the question, say so.
- Write plain, specific sentences for a car shopper. No "profoundly", "pivotal", "it is worth noting".""",
    model=MODEL,
    model_settings=SETTINGS,
)


class Hooks(RunHooks):
    """Records every tool call (with the agent that made it) and streams progress to the page."""

    def __init__(self, state: Research):
        self.state = state

    async def on_agent_start(self, context, agent):
        self.state.emit({"type": "activity", "agent": agent.name, "text": "started"})

    async def on_tool_start(self, context, agent, tool):
        self.state.emit({"type": "tool", "agent": agent.name, "name": tool.name,
                         "args": _args(getattr(context, "tool_arguments", None))})

    async def on_tool_end(self, context, agent, tool, result):
        text = str(result)
        if tool.name != "research":  # a researcher's prose isn't a source; only data tools are
            self.state.raw_results.append(text)
        self.state.tool_calls.append({
            "agent": agent.name, "name": tool.name, "args": _args(getattr(context, "tool_arguments", None)),
            "result": text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + " …[truncated]",
        })


def _args(raw):
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        return raw


# --- Sessions: a pending clarification, and the last vehicle researched ---

sessions: dict[str, dict] = {}


def build_question(session: dict, message: str) -> str:
    """Fold a clarification answer (or a follow-up) into the question the Lead sees."""
    if session.get("pending"):
        return f"{session['pending']}\n(The user clarified: {message})"
    if session.get("last"):
        return f"{message}\n(Earlier in this conversation the user asked: {session['last']})"
    return message


async def deep_research(session: dict, message: str, emit) -> dict:
    question = build_question(session, message)
    state = Research(emit=emit)
    hooks = Hooks(state)

    # 1. Research: clarify or plan, fan out, check for gaps
    plan = await Runner.run(lead, question, context=state, hooks=hooks, max_turns=14)
    lead_reply = str(plan.final_output).strip()
    if lead_reply.upper().startswith("CLARIFY:") and not state.findings:
        session["pending"] = session.get("pending") or message
        return {"response": lead_reply.split(":", 1)[1].strip(), "tool_calls": state.tool_calls, "clarify": True}
    session.pop("pending", None)
    if not state.findings:
        return {"response": lead_reply, "tool_calls": state.tool_calls}

    # 2. Write: one call, outside the loop. The Writer sees only the question and the findings.
    emit({"type": "activity", "agent": "Writer", "text": "writing the report"})
    findings = "\n\n---\n\n".join(state.findings)
    report = await Runner.run(writer, f"Question: {question}\n\nFindings:\n\n{findings}", hooks=hooks)
    session["last"] = question.split("\n(")[0]
    text, dropped = check_citations(str(report.final_output), state.raw_results)
    if dropped:
        emit({"type": "activity", "agent": "Citation check", "text": f"removed {dropped} link(s) not found in the data"})
    return {"response": text, "tool_calls": state.tool_calls, "stats": stats(state.raw_results)}


LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
BARE = re.compile(r"https?://[^\s)\]>\"'\\,]+")


def check_citations(report: str, results: list[str]) -> tuple[str, int]:
    """Keep only links whose URL a data tool actually returned. A made-up markdown link becomes its
    label; a made-up bare URL is removed."""
    seen = {u.rstrip(".;") for text in results for u in BARE.findall(text)}
    kept: list[str] = []
    dropped = 0

    def link(m: re.Match) -> str:
        nonlocal dropped
        if m.group(2) not in seen:
            dropped += 1
            return m.group(1)
        kept.append(m.group(0))
        return f"\x00{len(kept) - 1}\x00"  # park it so the bare-URL pass skips it

    def bare(m: re.Match) -> str:
        nonlocal dropped
        url = m.group(0).rstrip(".,;")
        if url in seen:
            return f"[source]({url})" + m.group(0)[len(url):]
        dropped += 1
        return ""

    text = BARE.sub(bare, LINK.sub(link, report))
    text = re.sub(r"\(\s*\)", "", text)  # "()" left where a bare URL was removed
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], text), dropped


def stats(results: list[str]) -> dict:
    """Headline numbers for the page's tiles, from the full (untruncated) data-tool results."""
    out: dict = {}
    for text in results:
        try:
            d = json.loads(text)
        except ValueError:
            continue
        if "total_recalls" in d and d["total_recalls"] >= out.get("recalls", -1):
            out["recalls"], out["recalls_ota"] = d["total_recalls"], d["fixed_over_the_air"]
        if "total_complaints" in d and d["total_complaints"] >= out.get("complaints", -1):
            out["complaints"], out["crashes"], out["fires"] = d["total_complaints"], d["crashes"], d["fires"]
        if "investigations" in d and isinstance(d["investigations"], list):
            out["investigations"] = d["total"]
            out["investigations_open"] = sum(i["closed"] == "still open" for i in d["investigations"])
        if "by_model_year" in d:
            ranges = [int(t["range_mi"]) for trims in d["by_model_year"].values() for t in trims if str(t["range_mi"]).isdigit()]
            if ranges:
                out["range_max"], out["range_min"] = max(ranges), min(ranges)
    return out


# --- FastAPI App ---

HERE = Path(__file__).parent
app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    # no-cache: browsers otherwise keep serving an old page after a redeploy
    return FileResponse(HERE / "index.html", headers={"Cache-Control": "no-cache"})


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """The whole run in one response: the report (or a clarifying question) and every tool call."""
    session_id = request.session_id or str(uuid.uuid4())
    session = sessions.setdefault(session_id, {})
    try:
        out = await deep_research(session, request.message, emit=lambda event: None)
    except Exception as e:
        out = {"response": f"Research failed: {type(e).__name__}: {str(e)[:300]}", "tool_calls": []}
    return ChatResponse(response=out["response"], session_id=session_id, tool_calls=out["tool_calls"])


@app.get("/run")
async def run(message: str, session_id: str | None = None):
    """The same run, streamed as Server-Sent Events so the page can show each researcher working."""
    session_id = session_id or str(uuid.uuid4())
    session = sessions.setdefault(session_id, {})
    queue: asyncio.Queue = asyncio.Queue()

    async def work():
        try:
            out = await deep_research(session, message, queue.put_nowait)
            queue.put_nowait({"type": "done", "session_id": session_id, **out})
        except Exception as e:
            # Auth, quota, a model that won't stop: show it on the page, not as a dropped stream.
            queue.put_nowait({"type": "error", "message": f"{type(e).__name__}: {str(e)[:500]}"})

    task = asyncio.create_task(work())

    async def events():
        try:
            yield f"data: {json.dumps({'type': 'start', 'session_id': session_id})}\n\n"
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event, default=str)}\n\n"
                if event["type"] in ("done", "error"):
                    break
        finally:
            task.cancel()  # the page closed the stream: stop spending tokens

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/clear")
def clear(session_id: str | None = None):
    sessions.pop(session_id, None)
    return {"status": "ok"}


if __name__ == "__main__":
    # Cloud Run sets $PORT; 0.0.0.0 so requests from outside the container reach us.
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
