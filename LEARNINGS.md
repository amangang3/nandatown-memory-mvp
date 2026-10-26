# Memory as a File — NANDA Town memory layer MVP

Field notes from building `markdown.v1`, a per-agent memory layer where the
markdown file **is** the store. What two agents wrote down, how much it cost,
and what happened when their memory was edited behind their backs.

| | |
|---|---|
| Date | 26 Oct 2026 |
| Harness | nandatown 0.2.0, evaluator lab-0.2.6, Python 3.13 |
| Brains | `claude-sonnet-5` (Anthropic Messages API), `mock:v1` (deterministic) |
| Runs | 6 evidence bundles under `runs/` |
| Code | `memory_mvp/` |
| Report | https://claude.ai/artifact/B2FmSjh3KRSp9kUqcACMkC |

---

## 1. What is under test

NANDA Town treats memory as one of twelve replaceable protocol layers. The
shipped default, `kv.v1`, is an in-process dictionary that dies with the run.
`markdown.v1` replaces it:

- every `remember(key, value)` rewrites that agent's own `memory/<agent>.md`
- every `recall(key)` parses the value back **out of that file**
- each file carries a `## Facts` table (current state, one row per key,
  rewritten in place) and an append-only `## Observations` log with logical
  timestamps

Two agents — a buyer and a seller — meet over two rounds. Round one the buyer
asks a price; round two it should already know. The encounter runs twice over:
once with scripted state machines (`memory_mvp.yaml`), once with Claude driving
both agents through tools (`memory_mvp_llm.yaml`). Ten stages grade each run
from the event trace alone.

Agent tool surface in the model-driven version:

```
list_memory()                         every key I already own
read_memory(key)                      one key
remember(key, value)                  write, subject to the contract
ask_price(seller, sku)                spend a round trip
place_order(seller, sku, qty, cents)  act on what I know
```

---

## 2. How much was actually stored

Very little, and that is the point.

| Agent / run | Keys | Facts | Log | File |
|---|---:|---:|---:|---:|
| agent-a · scripted | 3 | 135 B | 404 B | 799 B |
| agent-b · scripted | 4 | 163 B | 383 B | 806 B |
| agent-a · Claude | 2 | 122 B | 891 B | 1277 B |
| agent-b · Claude | 1 | 209 B | 512 B | 985 B |

"Facts" is the state a later round reads back; "Log" is the narrative around
it. The Claude files cover two runs with memory carried over.

**Durable state after a complete two-round encounter: 1–4 facts, 122–209 bytes,
roughly 30–50 tokens per agent.** Only the Facts block is injected into a later
prompt — the log is for humans and audits. Round two of the Claude run spent
**122 bytes** of context to skip a question it had already paid for. That is the
whole economic argument for a memory layer over replaying a transcript.

Activity per run (from the bundles):

| Run | Scenario | Events | Messages | Tool calls | Writes | Refused | Reads | Lists |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `sim-83aa9fbb5140` | scripted | 34 | 4 | — | 7 | 0 | 4 | — |
| `sim-539392d1ac82` | mock brain | 43 | 4 | 11 | 4 | 0 | 3 | 0 |
| `sim-af42b1e07978` | Claude, first | 66 | 6 | 20 | 5 | 0 | 9 | 0 |
| `sim-05655f5225ed` | Claude, key drift | 82 | 8 | 28 | 8 | 1 | 11 | 0 |
| `sim-fa437cacf26d` | Claude, all pass | 61 | 6 | 18 | 5 | 1 | 0 | 6 |
| `sim-815e5e8d6c10` | Claude, tampered | 30 | 2 | 6 | 1 | 1 | 0 | 2 |

### The two agent types stored information in opposite shapes

The scripted agents wrote **four small typed keys**. Claude's seller wrote
**one fat key** filed under the buyer's name:

```
| `agent-a` | "Returning buyer: previously quoted widget at 1995 cents.
              Has ordered 2 units at 1995 cents/unit twice without asking
              first - confirmed both orders. Reliable, price-matching buyer." |
```

Nobody asked it to build a counterparty record. It also read that key before
every reply. Useful behaviour — and unqueryable, unbounded prose.

---

## 3. Takeaways

### 3.1 A memory layer is a write boundary, not a store

The first Claude run stored `widget_unit_cents = "1995 (agent-b)"` — prose where
an integer belonged. It passed every stage, because that model was smart enough
to re-read its own annotation. Any weaker consumer breaks on it.

The fix is types declared per key and checked at the moment of writing
(`memory_schema.json`). The interesting part is the refusal: the reason goes
back to the writer, which corrects itself inside the same turn. Observed twice.
The refusal is also written into the agent's own file:

```
- t=0.50 · write to `widget_seller` refused: key 'widget_seller' holds the name
  of one agent in this run (agent-a, agent-b); got 'agent-b is the registered
  seller for sku widget. Asked price in round 1, awaiting response.'
- t=0.50 · remembered `widget_seller`
```

A rejected write is evidence about the writer, not a silent no-op — it emits
`memory_write_rejected` into the bundle.

### 3.2 Naming is the failure mode, not storage

The single most instructive failure. One run wrote the price under
`widget_unit_cents_agent-b`, then next round looked for `widget_unit_cents`,
then guessed `widget_agent-b_unit_cents`, missed both, gave up, and asked the
seller again:

```
ev-27  round 1  remember     widget_unit_cents_agent-b   ← written here
ev-44  round 2  read_memory  widget_unit_cents           miss
ev-48  round 2  read_memory  widget_agent-b_unit_cents   miss
ev-50  round 2  ask_price                                ← paid twice
```

The fact was in the file the whole time. **Convention stated in a system prompt
did not survive one round trip.** The fix is discovery, not more convention: a
`list_memory` tool returning every key the agent already owns. With it, key
drift went to zero and the redundant question disappeared. A `no_key_drift`
stage now fails any run that reads a key it never wrote while holding the same
fact under another name.

### 3.3 A contract only binds the names it can predict

Typed keys were trivially routed around — the model picked an undeclared name
and the permissive default applied. Pattern rules narrowed the gap:

```json
"keys":     {"widget_unit_cents": {"type": "integer", "min": 0},
             "preferred_seller":  {"type": "agent"}},
"patterns": {"*_cents": {"type": "integer", "min": 0},
             "*_seller": {"type": "agent"}},
"default":  {"type": "string", "max_length": 200}
```

Even then, `widget_price_agent_b` slipped through holding
`"1995 cents per unit as of last quote"`. A closed vocabulary is the only
complete fix, and it costs you the seller's counterparty record — the single
most useful thing either model invented. **That trade-off is a product
decision, not a technical one.**

### 3.4 Keys belong in the evidence, values belong in the file

Memory events carry the key, the file path and a byte count — never the value:

```
ev-20  agent-a  memory_written   {"key": "widget_unit_cents",
                                  "file": "memory/agent-a.md",
                                  "facts": 2, "bytes": 536}
```

An exported bundle therefore proves what an agent learned and when, without
leaking what it was. The `values_stayed_out_of_the_trace` stage enforces it on
every run.

### 3.5 Measure against a no-memory baseline

Swapping the layer is the cheapest experiment in the whole project:

| Layer | Round 2 asks again | Purchase completed | Survives the run | Auditable | Stages passed |
|---|---|---|---|---|---:|
| `none.v1` (amnesiac) | yes | **no** | no | no | 1 of 6 |
| `kv.v1` (default dict) | no | yes | no | no | 4 of 6 |
| `markdown.v1` | no | yes | **yes** | **yes** | 6 of 6 |

With no memory at all the pair never completes a purchase — they ask and answer
the same question twice and the run ends. `kv.v1` already buys *not repeating
yourself inside one conversation*; that is the cheap part. The file store buys
durability, inspectability and auditability **across** conversations, which is
the part other agents and people have to trust.

---

## 4. The retroactive edit experiment

After a clean passing run, one line of `agent-a.md` was changed by hand and the
same scenario re-run with memory carried over (`TOWN_MEMORY_PERSIST=1`).

```diff
  memory-llm/agent-a.md · ## Facts
- | `widget_price_agent_b` | "1995 cents per unit as of last quote" |
+ | `widget_price_agent_b` | "999 cents per unit as of last quote"  |
```

That is the entire intervention — no code changed, no prompt changed, no
restart. Result (`sim-815e5e8d6c10`):

```
t=0.50  agent-a  list_memory                2 keys
t=0.50  agent-a  place_order                unit_cents: 999      ← believed it
t=0.50  agent-a  question_skipped           round 1
t=0.60  agent-b  order_priced_from_memory   matched: false
t=0.60  agent-b  confirm_order              total_cents: 3990    ← billed 1995/unit
```

**The agent believed it instantly and completely.** It listed its memory, saw
999, and ordered in the first round with zero verification. Its own note: *"using
the previously recorded price."* Cost of the lie: zero round trips.

**The two sides then silently disagreed.** The buyer booked 999 per unit; the
seller billed its real 1995 and confirmed anyway, recording `matched: false` on
its side only. Nothing in the protocol stopped the trade or told the buyer it
had been overcharged.

**Nothing detected the edit.** No hash, no signature, no version, no prior
value. Worse, the file's own Observations log still carried the original true
line — *"Order placed … at 1995 cents each"* — directly above a Facts row saying
999. The history contradicted the index, and no reader existed to notice.

> A retroactive edit is indistinguishable from a memory. The layer cannot tell
> tampering from learning, which means it cannot tell either one from a mistake.

The same property is what makes the file useful — a human can correct an agent
by editing a line — so the answer is not to prevent edits but to make them
**attributable**.

---

## 5. What "git-like" would actually require

The MVP has git's *surface* — plain text, human-readable, diffable in any
editor — and almost none of its *guarantees*.

| Property | Today | What it needs |
|---|---|---|
| Integrity | byte count in the event | hash each fact; store the digest beside the row so an edit is detectable |
| History | append-only prose log | a commit per write: key, value, parent, run, agent |
| Blame | `updated t=0.70` | provenance per fact — which run, which message, which counterparty taught it |
| Authenticity | none | sign writes with the agent's identity key (the town's identity and auth layers already exist) |
| Diff & revert | manual text edit | `memory diff run-a run-b`; restore a fact to its prior commit |
| Concurrency | last write wins | compare-and-set against the fact's current digest |
| Branch & merge | one file per agent | per-run branches; a declared conflict when two runs set the same key |
| Forgetting | unbounded growth | TTL and compaction, with a `memory_forgotten` record so loss is attributable |

The first two rows are also the fix for the tamper experiment, and they are
cheap: a digest column and a commit record would have let agent-a notice that
its file no longer matched what it wrote. Everything below them is what turns a
per-agent notepad into infrastructure several agents can share.

---

## 6. Limits

- **Six runs, one model, one scenario shape.** Not a benchmark.
- **Lab, not Track.** Everything runs in an in-process discrete-event
  simulation, not as separate HTTP processes. The Track's built-in task is a
  single quote exchange with fixed buyer/seller roles and no second round, so
  the recall experiment has nowhere to happen there without forking
  `profiles.py`, `runner.py` and `evaluator.py`.
- **A harness quirk is visible in the tamper trace:** round two is scheduled off
  the price-answer path, so an agent that never asks never gets a round two.
  That shortened the tampered run to one round. It is a property of the
  scenario, not of memory.
- **Prose still gets through** under key names the contract cannot predict.
- The town prints the right caveat on every run: *one run is one scoped
  observation, not a certificate.*

---

## 7. Reproduce

```bash
source .venv/bin/activate

# scripted pair
nandatown run memory_mvp/memory_mvp.yaml

# no memory at all, and the stock in-process dict, for contrast
nandatown run memory_mvp/memory_mvp.yaml --plugin memory_mvp/memory_none.py --layer memory=none.v1
nandatown run memory_mvp/memory_mvp.yaml --layer memory=kv.v1

# model-driven pair (mock brain by default — no account needed)
TOWN_MEMORY_DIR=$PWD/memory_mvp/memory-llm nandatown run memory_mvp/memory_mvp_llm.yaml

# model-driven pair on a real model
ANTHROPIC_API_KEY=sk-ant-... TOWN_LLM_MODEL=claude-sonnet-5 \
  TOWN_MEMORY_DIR=$PWD/memory_mvp/memory-llm nandatown run memory_mvp/memory_mvp_llm.yaml

# carry memory across runs, then hand-edit a Facts row and rerun
TOWN_MEMORY_PERSIST=1 nandatown run memory_mvp/memory_mvp_llm.yaml

# offline tests of both real-model wire formats
python -m pytest memory_mvp/test_brains.py -q
```

### Files

| Path | What it is |
|---|---|
| `memory_mvp/memory_markdown.py` | the `markdown.v1` layer, the write contract, the scripted roles, the validator |
| `memory_mvp/memory_schema.json` | the write boundary: types by exact key, by pattern, and a default |
| `memory_mvp/memory_none.py` | `none.v1`, the amnesiac counterfactual |
| `memory_mvp/llm_agents.py` | model-driven roles, their tool surfaces, the LLM validator |
| `memory_mvp/llm_brains.py` | mock / Anthropic / OpenAI-compatible backends |
| `memory_mvp/test_brains.py` | offline wire-format tests |
| `memory_mvp/memory/`, `memory-llm/` | the memories themselves, one file per agent |

### Environment knobs

| Variable | Default | Effect |
|---|---|---|
| `TOWN_MEMORY_DIR` | `memory_mvp/memory` | where the files live |
| `TOWN_MEMORY_PERSIST` | `0` | `1` carries memory across runs |
| `TOWN_MEMORY_SCHEMA` | `memory_mvp/memory_schema.json` | the write contract |
| `TOWN_LLM_MODEL` | `mock:v1` | `claude-*` for Anthropic, any other name for an OpenAI-compatible endpoint |
| `TOWN_MODEL_URL` | Ollama `localhost:11434/v1` | OpenAI-compatible endpoint |
