# Demo guide

How to show the whole memory layer running in NANDA Town: two agents, their
individual memories, the shared store behind them, the write rules, the git
history, the access policy, and what happens when someone edits a memory by
hand.

Everything here runs in about a second and needs no model account. Commands
marked **(model)** are optional.

---

## Setup, once

```bash
git clone https://github.com/projnanda/nandatown && cd nandatown
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
cp -R /path/to/this/repo/memory_mvp .
```

Check it works, and start from a clean store:

```bash
TOWN_GITMEM_RESET=1 nandatown run memory_mvp/memory_combined.yaml
```

You should see `Verdict: PASSED` and a list of stages. Keep two terminals
open if you can: one at the repo root for `nandatown`, one in `memory_mvp`
for `gitmem.inspect`.

> Every `python3 -m gitmem.inspect …` command below is run **from inside
> `memory_mvp/`**.

---

## Act 1 — Run it (30 seconds)

```bash
nandatown run memory_mvp/memory_combined.yaml
```

Two agents meet: a buyer that needs widgets and a seller that has them.
The buyer asks a price, writes down what it learns, and in the second round
orders from memory instead of asking again.

Point at the stage list. Each line is a separate claim the run has to
survive, and they map onto the four capabilities:

| Stage | Capability |
|---|---|
| `lane_self_captured`, `lane_observed_captured`, `lanes_compared`, `observer_found_a_gap` | capture, in two lanes |
| `one_contract_for_both_lanes`, `refusals_explain_themselves` | writing rules |
| `every_change_committed`, `changes_carry_provenance`, `history_is_queryable`, `store_was_clean_at_open` | version tracking |
| `reads_are_policy_decisions`, `agents_kept_out_of_each_others_memory` | access control |

---

## Act 2 — Read one agent's own memory

This is what the buyer itself knows. Nothing else in the run can see it
unless the policy says so.

```bash
python3 -m gitmem.inspect facts agent-a --lane self
```

```
agent-a   self      last_order_total_cents = 3990
                                             by agent-a at t=0.70 · f-0015 · sim-78d2…
agent-a   self      preferred_seller       = "agent-b"
                                             by agent-a at t=0.70 · f-0003 · sim-170a…
agent-a   self      widget_unit_cents      = 1995
                                             by agent-a at t=0.70 · f-0004 · sim-170a…
```

Three facts, about 120 bytes. That is everything the buyer carries between
conversations — and in round two it spent exactly that instead of asking
the seller again.

Swap the name for the seller's side of the same encounter:

```bash
python3 -m gitmem.inspect facts agent-b --lane self
```

Each fact is a file you can open:

```bash
cat memory-git/facts/agent-a/self/f-0004.md
```

```
---
id: "f-0004"
holder: "agent-a"
lane: "self"
key: "widget_unit_cents"
subject: "agent-a"
asserter: "agent-a"
type: "integer"
scope: "private"
status: "active"
...
---
1995
```

**The line to say out loud:** this file *is* the memory. Not a cache of it,
not a log of it. Edit the last line and the agent believes you — which is
Act 7.

---

## Act 3 — The shared store behind both agents

One store holds every agent's memory, namespaced so they never share a
namespace. Drop the agent name to see all of it:

```bash
python3 -m gitmem.inspect facts
ls -d memory-git/facts/*/*
```

```
facts/agent-a/observed   facts/agent-a/self
facts/agent-b/observed   facts/agent-b/self
```

Two agents × two **lanes**. The `self` lane is what the agent chose to
write. The `observed` lane is what an external observer concluded by
reading the run's raw trace — the subject of Act 5.

---

## Act 4 — The writing rules

Both lanes go through one contract (`memory_schema.json`). You can try a
value against it without storing anything:

```bash
python3 -m gitmem.inspect try agent-a widget_unit_cents "1995 cents or so"
```

```
REFUSED by the write contract
  key 'widget_unit_cents' holds integers; got '1995 cents or so'. Write the
  bare number and put any commentary in a separate key.
  rule for this key: {"type": "integer", "min": 0}
```

```bash
python3 -m gitmem.inspect try agent-a widget_unit_cents 1995
```

```
ACCEPTED (nothing was stored; this was a dry run)
  stored as: 1995  type=integer  scope=private
```

Two more worth showing:

```bash
# a key nobody declared is still caught, by the shape of its name
python3 -m gitmem.inspect try agent-a widget_price_cents "about twenty bucks"

# a key typed `agent` only accepts a real participant in this run
python3 -m gitmem.inspect try agent-b preferred_seller nobody
```

```
REFUSED by the write contract
  key 'preferred_seller' holds the name of one agent in this run
  (agent-a, agent-b); got 'nobody'
```

**The point:** the refusal goes back to the writer with the reason, so a
model corrects itself in the same turn instead of quietly rotting the
store. Running the model-driven scenario shows that happening live.

---

## Act 5 — Two lanes, and the gap between them

The observer never sees inside agent memory. It reads the same event trace
the town recorded and decides independently what each agent should know,
quoting the trace for every claim:

```bash
python3 -m gitmem.inspect facts agent-a --lane observed
```

```
agent-a   observed  widget_unit_cents      = 1995
                                             by observer at t=0.60 · f-0009 · ev-18
                                             evidence: ev-18 t=0.60 agent-b message_sent
                                             kind=price_answer to=agent-a body={"sku":
                                             "widget", "unit_cents": 1995}
```

Then the two lanes are compared. The run prints the counts, and the full
report is committed into the store:

```bash
cat memory-git/reports/*.json | head -30
```

| Finding | Means |
|---|---|
| `agreed` | both lanes hold the same value |
| `omission` | the trace supports a fact the agent never wrote down |
| `contradiction` | the lanes hold different values for the same key |
| `unsupported` | the agent asserted something the trace does not back |

In the standard run you will see `unsupported` on the seller's internal
counters (`questions_answered`, `orders_filled`) — correct, because nothing
on the wire proves them. **(model)** With real agents the observer typically
finds several `omission`s: facts the agents simply never bothered to record.

---

## Act 6 — Version tracking

Every accepted change is a git commit, and nothing is ever overwritten.
The clearest way to show it is to change something in the world and watch
the record catch up. The seller has put its price up since the last run:

```bash
nandatown run memory_mvp/memory_combined.yaml              # price is 1995
nandatown run memory_mvp/memory_combined_price_change.yaml # price is now 2195
```

The buyer still believes 1995, orders at it, and is refused. It does not
fix its own memory — but the observer, reading the refusal, does:

```bash
python3 -m gitmem.inspect history agent-a widget_unit_cents
```

```
now   f-0004  lane=self      t=0.70   by agent-a   = 1995
was   f-0009  lane=observed  t=0.60   by observer  = 1995
      replaced by f-0014 at t=0.60
      evidence: ev-18 … kind=price_answer … {"unit_cents": 1995}
now   f-0014  lane=observed  t=0.60   by observer  = 2195
      evidence: ev-19 … kind=order_declined … "doesn't match my actual price
      of 2195 cents/unit"
```

Three things in one screen: the old value is **kept** and marked `was`, it
links forward to what replaced it, and the new value carries the sentence
that justified it. The buyer's own lane still says 1995 — which is why the
run also reports a `contradiction`.

The commit behind it:

```bash
python3 -m gitmem.inspect log 8
git -C memory-git log -1 --format="%h%n%n%B" --grep=widget_unit_cents -- facts
```

```
memory: +0 added, ~1 updated, -0 invalidated (observed at t=0.60)

~ f-0009 -> f-0014 [agent-a/observed] widget_unit_cents
    was: 1995
    now: 2195
    why: the refusal stated the seller's real price, which contradicts what
         the buyer acted on
    evidence: ev-19 t=0.60 agent-b message_sent kind=order_declined …
    asserted by: observer

Source: ev-19
Logical time: t=0.60
```

Who asserted it, when, from which event, what it replaced, and the quote
behind it. `git log`, `git diff` and `git blame` all work, because it is a
real repository. Re-asserting a value that is already current is recorded
as an act (`memory_write_noop`) but creates no new version, so the history
only holds changes that mean something.

---

## Act 7 — Someone edits a memory by hand

The payoff. Open a fact file in any editor and change the value:

```bash
# the buyer's remembered price: 1995 -> 999
sed -i '' 's/^1995$/999/' memory-git/facts/agent-a/self/f-0004.md
python3 -m gitmem.inspect check
```

```
TAMPERED · 1 fact file(s) changed outside the store:
  facts/agent-a/self/f-0004.md

-1995
+999
```

Now run the scenario again:

```bash
nandatown run memory_mvp/memory_combined.yaml
```

It fails, and it fails **twice over**, in two independent ways:

1. `store_was_clean_at_open` — **Failed**, naming the edited file. The store
   commits only files it wrote, so an outside edit is never quietly absorbed.
   It stays flagged on every run until someone deals with it.
2. `observer_found_a_gap` — a **contradiction** on `widget_unit_cents`. The
   observer, reading only the wire, concluded 1995; the agent's own memory
   says 999.

```bash
python3 -m gitmem.inspect events | grep -E "tampered|reconcile_finding"
```

Put it back when you are done:

```bash
git -C memory-git checkout -- facts
```

---

## Act 8 — Access control

Who may read what, as toggles.

```bash
python3 -m gitmem.inspect policy
```

```
preset                               default
capture_lanes                        ['self', 'observed']
agents_read_each_other               False
subject_can_read_claims_about_self   False
harness_can_read_agent_memory        True
observer_can_read_agent_memory       True
```

Every read by anyone other than the holder is a recorded decision:

```bash
python3 -m gitmem.inspect events | grep memory_access
```

```
 ev-18  agent-b   memory_access_denied   agent-a  rule="subject_can_read_claims_about_self"
        reason: 'agent-b' is the subject of this claim but may not read what
        'agent-a' records about it
 ev-27  harness   memory_access_allowed  agent-a  rule="harness_can_read_agent_memory"
```

The seller tried to look at the buyer's memory and was refused, with the
rule that refused it. The harness was allowed, because this deployment
says operators may look.

### The cross-enterprise case

Sometimes you do not get to define how the other side records anything,
and you certainly do not get to read inside it. Only observation is left:

```bash
TOWN_MEMORY_PRESET=cross_enterprise nandatown run memory_mvp/memory_combined.yaml
python3 -m gitmem.inspect events | grep -E "write_blocked|access_denied" | head -4
```

```
 ev-16  agent-b  memory_write_blocked  agent-b  key="last_question_from" rule="capture_lanes"
        reason: this deployment does not capture the 'self' lane: we do not get to
        define how these agents record their own memory, so only observed facts
        are accepted
 ev-42  harness  memory_access_denied  agent-a  rule="harness_can_read_agent_memory"
```

The agents still call `remember`; every call is refused and the refusal is
recorded. The harness is refused too. **And the run still produces a
complete record** — built entirely from traffic anyone could legitimately
see. That is the deployment where only the observation lane is possible.

Switch the other way to show the opposite end:

```bash
TOWN_MEMORY_PRESET=open_lab nandatown run memory_mvp/memory_combined.yaml
```

Now `agents_kept_out_of_each_others_memory` reports **Not tested**, because
this preset deliberately lets agents read each other.

---

## Act 9 — With real models **(model)**

Agents that decide for themselves what to remember:

```bash
ANTHROPIC_API_KEY=sk-ant-... TOWN_LLM_MODEL=claude-sonnet-5 \
  nandatown run memory_mvp/memory_combined_llm.yaml
```

An observer that is a real model rather than fixed rules — this one runs
through your Claude Code login, so it costs plan usage rather than API
credits:

```bash
TOWN_OBSERVER_MODEL=claude-cli:claude-haiku-4-5-20251001 \
  nandatown run memory_mvp/memory_combined.yaml
```

It is slow (a call per turn; minutes, not seconds), so run it before the
demo and show the bundle rather than waiting on stage:

```bash
python3 -m gitmem.inspect events <run-id> | grep memory_observed
```

---

## A five-minute version

If you only have one slot, run these in order and talk over them:

1. `nandatown run memory_mvp/memory_combined.yaml` — *two agents trade; the
   second round is served from memory*
2. `python3 -m gitmem.inspect facts agent-a --lane self` — *this is
   everything the buyer knows, about 120 bytes*
3. `python3 -m gitmem.inspect try agent-a widget_unit_cents "1995 cents or so"`
   — *the rules refuse bad values and say why*
4. `nandatown run memory_mvp/memory_combined_price_change.yaml` then
   `python3 -m gitmem.inspect history agent-a widget_unit_cents` — *the price
   changed; nothing was overwritten, and the observer repaired the record
   from the refusal*
5. edit a fact file, then `nandatown run …` again — *caught twice: the store
   knows it was edited, and the observer contradicts it from the trace alone*
6. `TOWN_MEMORY_PRESET=cross_enterprise nandatown run …` — *and when you do
   not control the other side's agents at all, observation still works*

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `no store at …` | run a scenario first, or set `TOWN_GITMEM_DIR` |
| Want a clean slate | `TOWN_GITMEM_RESET=1 nandatown run …` |
| `check` still says TAMPERED | `git -C memory-git checkout -- facts` |
| Stages say *Not tested* | the store is settled — nothing changed this run. Use `TOWN_GITMEM_RESET=1` for a fresh one |
| `ModuleNotFoundError: gitmem` | run `gitmem.inspect` from inside `memory_mvp/` |
| Observer failed | check `python3 -m gitmem.inspect events \| grep observation` for the reason; `rules:v1` always works |
