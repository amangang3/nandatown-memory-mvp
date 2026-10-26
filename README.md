# Memory as a file

An MVP of a git-like memory layer for AI agents, built as a plugin for
[NANDA Town](https://github.com/projnanda/nandatown).

Two Claude agents trade with each other. Each keeps its memory in a plain
markdown file it reads and writes itself. The file **is** the store, not a log
of it — edit a line by hand and the agent believes you.

**▶ [Watch the interactive demo](https://amangang3.github.io/nandatown-memory-mvp/)**
— press play, watch both memory files read, write and refuse in step, then see
what happens when one line of one agent's memory is altered.

Every step in the demo is replayed from a recorded run. Nothing is mocked.

## The three runs

| Run | What was edited | What happened |
|---|---|---|
| Normal | nothing | Sale completed, both sides agree, two messages |
| Buyer's memory altered | the price it thinks a widget costs ($19.95 → $9.99) | Buyer believed it. The marketplace blocked the sale — the memory layer never noticed |
| Seller's memory altered | its customer note, replaced with an accusation | Buyer's price was correct and it was refused anyway, twice, on a history that never happened |

The contrast is the finding: **a corrupted price is caught, because a price is
checkable. A corrupted reputation is caught by nothing.**

[LEARNINGS.md](LEARNINGS.md) has the full write-up — measurements, takeaways,
the tamper experiment, and what a genuinely git-like memory layer would still
need.

## What's here

| Path | What it is |
|---|---|
| `index.html` | the demo site (self-contained, no build step) |
| `LEARNINGS.md` | the written report |
| `memory_mvp/memory_markdown.py` | the `markdown.v1` memory layer, its write contract, the scripted agents, the validator |
| `memory_mvp/memory_schema.json` | the write boundary: types by exact key, by pattern, and a default |
| `memory_mvp/llm_agents.py` | model-driven agents, their tool surface, the LLM validator |
| `memory_mvp/llm_brains.py` | three interchangeable backends: deterministic mock, Anthropic, any OpenAI-compatible endpoint |
| `memory_mvp/memory_none.py` | `none.v1`, an amnesiac layer, as the counterfactual |
| `memory_mvp/memory/`, `memory-llm/` | the agents' actual memory files |

## Running it

Needs [nandatown](https://github.com/projnanda/nandatown) and Python 3.11+.

```bash
git clone https://github.com/projnanda/nandatown && cd nandatown
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
cp -R /path/to/this/repo/memory_mvp .

# scripted agents, ~1 second, no account needed
nandatown run memory_mvp/memory_mvp.yaml

# no memory at all, and the stock in-process memory, for contrast
nandatown run memory_mvp/memory_mvp.yaml --plugin memory_mvp/memory_none.py --layer memory=none.v1
nandatown run memory_mvp/memory_mvp.yaml --layer memory=kv.v1

# model-driven agents (deterministic mock brain by default)
TOWN_MEMORY_DIR=$PWD/memory_mvp/memory-llm nandatown run memory_mvp/memory_mvp_llm.yaml

# ...on a real model
ANTHROPIC_API_KEY=sk-ant-... TOWN_LLM_MODEL=claude-sonnet-5 \
  TOWN_MEMORY_DIR=$PWD/memory_mvp/memory-llm nandatown run memory_mvp/memory_mvp_llm.yaml

# carry memory between runs, then hand-edit a row and run again
TOWN_MEMORY_PERSIST=1 nandatown run memory_mvp/memory_mvp_llm.yaml
```

| Variable | Default | Effect |
|---|---|---|
| `TOWN_MEMORY_DIR` | `memory_mvp/memory` | where the memory files live |
| `TOWN_MEMORY_PERSIST` | `0` | `1` carries memory across runs |
| `TOWN_MEMORY_SCHEMA` | `memory_mvp/memory_schema.json` | the write contract |
| `TOWN_LLM_MODEL` | `mock:v1` | `claude-*` for Anthropic, any other name for an OpenAI-compatible endpoint |
| `TOWN_MODEL_URL` | Ollama `localhost:11434/v1` | OpenAI-compatible endpoint |

## Credits

Built on [NANDA Town](https://github.com/projnanda/nandatown) by Project NANDA
(Apache-2.0), which provides the twelve-layer agent sandbox, the evaluator and
the evidence bundles. The memory layer, agents, schema and demo in this repo
are original work, MIT licensed.

The accusation quoted in the demo ("disputed the last two invoices and never
paid") is fabricated test data about a synthetic agent called `agent-a`. It
refers to no real person or company.
