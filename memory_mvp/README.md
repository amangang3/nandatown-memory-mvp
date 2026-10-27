# memory_mvp — a markdown memory layer for NANDA Town

Two agents meet, learn one fact from each other, and each writes what
it learned to **its own `memory/<agent>.md`**. On the next round the
asker reads the fact back out of its file instead of asking again.

The point of the MVP: in nandatown, *memory is one of the twelve
replaceable protocol layers*. The default plugin (`kv.v1`) keeps an
in-process dict that dies with the run. This swaps in `markdown.v1`,
where the file on disk **is** the store — not a log of it.

## Run it

```
source ../.venv/bin/activate
nandatown run memory_mvp/memory_mvp.yaml     # from the repo root
cat memory/agent-a.md memory/agent-b.md
```

Cold start: agent-a asks once, both agents write their files, round two
is served from memory. ~1 second, deterministic, no model calls.

## Files

| file | what it is |
|---|---|
| `memory_mvp.yaml` | the scenario: 2 agents, `layers: {memory: markdown.v1}` |
| `memory_markdown.py` | the `markdown.v1` plugin, the two roles, and the validator |
| `memory/<agent>.md` | the memory itself, one file per agent (rewritten each run) |

## What each file holds

- `## Facts` — current state, one row per key, rewritten in place.
  Values are JSON so they round-trip with their type.
- `## Observations` — append-only log with logical timestamps.

## The three things it proves

```
nandatown run memory_mvp/memory_mvp.yaml
```
**Cold start.** `recalled_without_asking` passes: one question in the
whole run, round two ordered from the file.

```
TOWN_MEMORY_PERSIST=1 nandatown run memory_mvp/memory_mvp.yaml
```
**Memory outlives the run.** Zero questions asked — agent-a starts
round one already knowing the price from the previous run's file.

```
# hand-edit memory/agent-a.md: change widget_unit_cents to 999
TOWN_MEMORY_PERSIST=1 nandatown run memory_mvp/memory_mvp.yaml
```
**The file is the store.** The run FAILS at
`remembered_price_was_right`, because the agent now believes what you
typed. Nothing else changed.

## The contract a memory plugin has to meet

```python
@register("memory", "yourstore.v1")
class YourStore:
    def __init__(self, engine): ...
    def remember(self, agent: str, key: str, value) -> None: ...
    def recall(self, agent: str, key: str): ...
```

`markdown.v1` adds one optional extension, `note(agent, text)`, for
prose observations; agents that only call `remember`/`recall` work
unchanged. Writes emit a `memory_written` event carrying the key, the
file path and the byte count — **never the value** — so an exported
evidence bundle cannot leak private state through the memory layer.
The `values_stayed_out_of_the_trace` stage checks exactly that.

## Knobs

| env var | default | effect |
|---|---|---|
| `TOWN_MEMORY_DIR` | `./memory` | where the files live |
| `TOWN_MEMORY_PERSIST` | `0` | `1` carries memory across runs |

Scenario config: `rounds` and `quantity` on agent-a, `unit_cents` on
agent-b.

## Model-driven agents

`memory_mvp_llm.yaml` runs the same encounter with nothing hardcoded: a
model decides whether to consult memory, whether to ask, what is worth
writing down, and when to order. Its tool surface is the memory layer's
own contract plus the two town actions.

```
read_memory(key)                     what do I already know?
remember(key, value)                 keep this for next time
ask_price(seller, sku)               spend a round trip
place_order(seller, sku, qty, cents) act on what I know
```

```
TOWN_MEMORY_DIR=$PWD/memory_mvp/memory-llm \
  nandatown run memory_mvp/memory_mvp_llm.yaml
```

Default brain is `mock:v1`: a deterministic policy, no account, no
network, so the scenario is reproducible in CI. Point it at a real
model to find out whether one actually behaves this way.

| env | effect |
|---|---|
| `TOWN_LLM_MODEL=mock:v1` | deterministic stand-in (default) |
| `TOWN_LLM_MODEL=claude-sonnet-5` | Anthropic Messages API, needs `ANTHROPIC_API_KEY` |
| `TOWN_LLM_MODEL=llama3.1` | any OpenAI-compatible `/chat/completions`; `TOWN_MODEL_URL` defaults to Ollama |

The extra stages a model-driven run has to pass:

| stage | the claim |
|---|---|
| `model_drove_both_agents` | both agents report which brain drove them |
| `model_chose_to_write_memory` | each model decided, unprompted by code, to write something |
| `memory_consulted_before_acting` | every round opens with `read_memory`, not with an action |
| `model_skipped_the_redundant_question` | having learned the price once, it never asks again |

Note what stays with the harness, not the model: the seller's real
price, and the `matched` verdict comparing it to what the buyer
remembered. A model is never asked to grade itself.

Files: `llm_agents.py` (roles, tools, validator), `llm_brains.py` (the
three backends; no vendor message format leaks into the agents),
`test_brains.py` (offline checks of both real-model wire formats —
`python -m pytest memory_mvp/test_brains.py -q`).

## The write boundary

`memory_schema.json` declares what each key may hold. Writes are checked
at the moment of writing; a refused write emits `memory_write_rejected`
with the reason, the reason goes back to the writer, and the writer gets
to try again.

```json
"keys":     {"widget_unit_cents": {"type": "integer", "min": 0},
             "preferred_seller":  {"type": "agent"}},
"patterns": {"*_cents": {"type": "integer", "min": 0},
             "*_seller": {"type": "agent"}},
"default":  {"type": "string", "max_length": 200}
```

Exact names win, then patterns (which bind the names a model invents),
then the default. Types: `integer`, `number`, `boolean`, `string`
(`max_length`), `agent` (must name a participant in this run).
Point `TOWN_MEMORY_SCHEMA` elsewhere to use a different contract.

Agents also get `list_memory`, which returns every key they have
written. Without it a model invents a key name on the way in and a
different one on the way out, and pays again for a fact it already
owns — see `no_key_drift`.

## The combined layer: `git.v1`

`markdown.v1` above is the single-lane MVP. `git.v1` is the full thing —
two capture lanes, one write contract, a git-versioned store adapted from
[git-for-memory](https://github.com/akg745/git-for-memory), and an access
policy on every read.

```bash
nandatown run memory_mvp/memory_combined.yaml              # scripted, ~1s
nandatown run memory_mvp/memory_combined_llm.yaml          # model-driven
TOWN_MEMORY_PRESET=cross_enterprise \
  nandatown run memory_mvp/memory_combined.yaml            # observation only
cd memory_mvp && python3 -m gitmem.inspect history agent-a widget_unit_cents
```

| Capability | Where it lives |
|---|---|
| Capture, two lanes | `gitmem/layer.py` (self), `gitmem/observer.py` (observed) |
| Writing rules | `gitmem/contract.py` + `memory_schema.json` |
| Version tracking | `gitmem/store.py` + `gitmem/inspect.py` |
| Access control | `gitmem/policy.py` + `memory_policy.json` |

See **[COMBINED.md](COMBINED.md)** for the full picture: the presets, the
observer backends, the event vocabulary, the stages and the limits.

## Where to take it next

- More agents: add rows under `agents:`; each gets its own file.
- Swap back to the default to see the difference:
  `nandatown run memory_mvp/memory_mvp.yaml --layer memory=kv.v1`
  (the recall stages still pass — it just leaves nothing on disk).
- Forgetting: add TTLs or a size cap to `_write` and a
  `memory_forgotten` event, then a stage that proves what was dropped.
- Shared vs private memory: today each agent reads only its own file.
  A second plugin could expose a shared section and a stage proving
  one agent cannot read another's private facts.
- Real processes: the Track mode runs agents as separate HTTP
  processes. Same file format, memory written agent-side.
