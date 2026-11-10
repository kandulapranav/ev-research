"""Run the README's sample queries two ways and save both answers:

  1. the same model (Gemini 3.5 Flash) with no tools and no system prompt
  2. EV Research (Lead + Researchers + Writer)

    uv run scripts/compare.py          # all three
    uv run scripts/compare.py 1        # just the second

Writes traces/<n>-<slug>.md, with the agent's full tool-call trace.
"""

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import litellm  # noqa: E402

import app  # noqa: E402

QUESTIONS = [
    "Is a used 2021 Ford Mustang Mach-E a safe bet?",
    "Hyundai Ioniq 5: which model years should I avoid buying used?",
    "What's the history of the Chevy Bolt EV battery fire recall? Are 2022-23 Bolts affected?",
]
OUT = Path(__file__).resolve().parent.parent / "traces"


def no_tools(question: str) -> str:
    r = litellm.completion(model="vertex_ai/gemini-3.5-flash", vertex_location="global",
                           messages=[{"role": "user", "content": question}])
    return r.choices[0].message.content


async def run(i: int, question: str) -> None:
    out = await app.deep_research({}, question, emit=lambda e: None)
    calls = "\n".join(
        f"{n}. **{c['agent']}** → `{c['name']}`\n\n```json\n{json.dumps(c['args'])}\n```\n\n"
        f"```\n{c['result'][:1200]}{' …' if len(c['result']) > 1200 else ''}\n```\n"
        for n, c in enumerate(out["tool_calls"], 1))
    text = (f"# {question}\n\n## 1. Gemini 3.5 Flash, no tools, no system prompt\n\n{no_tools(question)}\n\n"
            f"## 2. EV Research\n\n{out['response']}\n\n## Trace ({len(out['tool_calls'])} tool calls)\n\n{calls}")
    slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:48]
    OUT.mkdir(exist_ok=True)
    (OUT / f"{i}-{slug}.md").write_text(text)
    print(f"wrote traces/{i}-{slug}.md")


async def main(which: list[int]) -> None:
    for i in which:
        await run(i, QUESTIONS[i])


if __name__ == "__main__":
    asyncio.run(main([int(a) for a in sys.argv[1:]] or list(range(len(QUESTIONS)))))
