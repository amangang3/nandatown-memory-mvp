# The combined memory layer (`git.v1`)

Two capture lanes, one write contract, a git-versioned store, and a read
policy — running inside NANDA Town as a drop-in `memory` layer.

```
      LANE A  the agent writes                LANE B  the observer reads
      what it chose to remember               the raw trace and decides
              │                                       │
              └──────────┐             ┌──────────────┘
                         ▼             ▼
                   ┌──────────────────────────┐
                   │   the write contract      │  typed keys, limits,
                   │   (memory_schema.json)    │  evidence required of
                   └────────────┬──────────────┘  the observer; refusal
                                ▼                  comes back with a reason
                   ┌──────────────────────────┐
                   │   git store (memory-git)  │  one commit per change,
                   │   facts/<agent>/<lane>/   │  supersede links, who
                   └────────────┬──────────────┘  asserted it and from where
                                ▼
                   ┌──────────────────────────┐
                   │   read policy             │  every read by anyone but
                   │   (memory_policy.json)    │  the holder is a decision,
                   └──────────────────────────┘  and is recorded
                                │
                                ▼
                     the two lanes compared:
            agreed · omission · contradiction · unsupported
```

The store is adapted from **git-for-memory** (akg745) — its fact model,
supersede semantics and commit discipline. The two-lane capture, the write
contract, the access policy and the reconciliation are from this MVP.

## Run it

```bash
# scripted agents, deterministic observer: about a second, no account
nandatown run memory_mvp/memory_combined.yaml

# model-driven agents (mock brains by default)
nandatown run memory_mvp/memory_combined_llm.yaml

# ...with real agents
ANTHROPIC_API_KEY=sk-ant-... TOWN_LLM_MODEL=claude-sonnet-5 \
  nandatown run memory_mvp/memory_combined_llm.yaml

# ...with a real observer, through your Claude Code login rather than credits
TOWN_OBSERVER_MODEL=claude-cli:claude-haiku-4-5-20251001 \
  nandatown run memory_mvp/memory_combined.yaml

# a deployment where we do not get to define how agents record anything
TOWN_MEMORY_PRESET=cross_enterprise \
  nandatown run memory_mvp/memory_combined.yaml
```

Run it twice and the second run supersedes the first run's facts rather
than overwriting them — that is the point of the store.

## Capability 1 — capture, in two lanes

**Lane A, self-report.** `api.remember(key, value)` writes into
`facts/<agent>/self/`, asserted by the agent itself. An agent cannot write
into another agent's memory; the contract refuses it.

**Lane B, observation.** At the end of the run the layer replays the
event trace one logical tick at a time and asks an observer what each
agent should now know. Every observed fact must carry a quote from the
trace and the event id it came from. Observed facts land in
`facts/<agent>/observed/`, asserted by `observer`, never by an agent.

| `TOWN_OBSERVER_MODEL` | What runs |
|---|---|
| `rules:v1` *(default)* | fixed rules over the trace — no account, no variance, reproducible |
| `claude-cli:<model>` | the local Claude Code CLI headless, on your login rather than API credits |
| `claude-sonnet-5`, … | the Anthropic Messages API, needs `ANTHROPIC_API_KEY` |

**The gap between them** is the output worth having. For each agent:

| Finding | Means |
|---|---|
| `agreed` | both lanes hold the same value for a key |
| `omission` | the trace supports a fact the agent never wrote down |
| `contradiction` | the lanes hold different values for the same key |
| `unsupported` | the agent asserted something the trace does not back |

Measured: with Sonnet driving both agents, the observer recorded **5 facts
the agents never wrote down**. With a tampered store, it reported a
**contradiction** on exactly the key that had been edited by hand.

## Capability 2 — writing rules, one door for both lanes

`memory_schema.json` binds types by exact key, then by key pattern, then a
default; `*_cents` must be an integer, `*_seller` must name a real agent,
anything undeclared is a string of at most 200 characters. On top of that:

* a `self` write must be made by its holder;
* an `observed` write must carry `evidence` and a `source` event;
* a refusal emits `memory_write_rejected` **with the reason**, and the
  writer gets that reason back so it can try again.

## Capability 3 — version tracking

Every accepted change is a git commit naming the holder, the lane, the
key, what it replaced, who asserted it, the evidence and the logical time.
Commit timestamps come from the run's logical clock, so a replay produces
the same history rather than wall-clock noise.

```bash
cd memory_mvp
python3 -m gitmem.inspect facts agent-a          # what it holds now, both lanes
python3 -m gitmem.inspect history agent-a widget_unit_cents
python3 -m gitmem.inspect log 20                 # the commits
python3 -m gitmem.inspect check                  # edited outside the store?
git -C memory-git log --stat                     # or just use git
```

**Tamper detection.** Edit a fact file by hand and the next run opens with
`memory_tampered`, names the file, and fails `store_was_clean_at_open`.
The store commits only the files it wrote, so an out-of-band edit is never
quietly absorbed into its own history — it stays dirty until someone deals
with it.

## Capability 4 — access control, as toggles

`memory_policy.json` picks a preset; `overrides` changes individual
toggles on top of it. A toggle not named in `overrides` follows the
preset, so switching preset really switches behaviour.

| Toggle | What it governs |
|---|---|
| `capture_lanes` | which lanes are captured at all |
| `agents_read_each_other` | can agent A read agent B's memory |
| `subject_can_read_claims_about_self` | can the agent a claim is *about* read it |
| `harness_can_read_agent_memory` | can the operator look inside |
| `observer_can_read_agent_memory` | can the observer compare its record against the agent's |

| Preset | Shape |
|---|---|
| `open_lab` | everything readable; for debugging a scenario |
| `default` | agent memory is private; harness and observer may read it; a subject cannot see claims made about it |
| `cross_enterprise` | **we do not define how the other side records memory and cannot read inside it** — the observed lane is the only capture, and the harness is refused too |

Cross-enterprise is the case worth watching. Agents still call
`remember`; every call is refused with *"this deployment does not capture
the 'self' lane: we do not get to define how these agents record their own
memory, so only observed facts are accepted"* — and the run still produces
a full record, built entirely from traffic anyone could legitimately see.

Every read by anyone other than the holder emits
`memory_access_allowed` or `memory_access_denied` with the rule that
decided it. The harness exercises its own read right at the end of every
run, so a bundle always shows what the operator was allowed to see.

## What lands in the evidence bundle

Keys, commits, rules and reasons — never values. A stage
(`values_stayed_out_of_the_trace`) enforces it on every run.

```
memory_store_opened      head, preset, lanes, fact count
memory_tampered          fact files changed outside the store
memory_written           a self-reported fact: key, op, commit, supersedes
memory_observed          an observed fact: + evidence length, confidence
memory_write_rejected    the contract refused a write, and why
memory_write_blocked     the policy does not capture that lane, and why
memory_recalled          a holder read its own memory, hit or miss
memory_access_allowed    a read by someone else, and the rule that allowed it
memory_access_denied     ...and the rule that refused it
memory_observation_done  which observer ran, accepted/rejected counts
memory_reconciled        per agent: agreed / omission / contradiction / unsupported
memory_reconcile_finding one line per disagreement
```

## Stages

`memory_combined` (and `memory_combined_llm`, which adds
`model_drove_both_agents`) grade a run from the trace alone:

| Capability | Stages |
|---|---|
| 1 | `lane_self_captured` · `lane_observed_captured` · `observed_facts_carry_evidence` · `lanes_compared` · `observer_found_a_gap` |
| 2 | `one_contract_for_both_lanes` · `refusals_explain_themselves` |
| 3 | `every_change_committed` · `changes_carry_provenance` · `history_is_queryable` · `store_was_clean_at_open` |
| 4 | `reads_are_policy_decisions` · `agents_kept_out_of_each_others_memory` |
| — | `values_stayed_out_of_the_trace` |

Stages report `not_tested` rather than failing when a run had no occasion
to exercise them — a warm store with nothing new to write, or a policy
that forbids comparing the lanes.

## Environment

| Variable | Default | Effect |
|---|---|---|
| `TOWN_GITMEM_DIR` | `memory_mvp/memory-git` | where the store lives |
| `TOWN_GITMEM_RESET` | `0` | `1` wipes the store before the run |
| `TOWN_MEMORY_SCHEMA` | `memory_mvp/memory_schema.json` | the write contract |
| `TOWN_MEMORY_POLICY` | `memory_mvp/memory_policy.json` | the policy file |
| `TOWN_MEMORY_PRESET` | from the file | override the preset for one run |
| `TOWN_OBSERVER_MODEL` | `rules:v1` | which observer runs |
| `TOWN_LLM_MODEL` | `mock:v1` | which brain drives the agents |

## Limits

* The observation lane runs once, at the end of the run. Per-turn
  observation is the same code on a different trigger, but it is not
  wired up.
* Reconciliation compares by key. Two lanes that record the same fact
  under different key names read as one omission plus one unsupported —
  the key-drift problem, now visible instead of silent.
* Writes are not signed. Provenance says who *claims* to have asserted a
  fact; until each agent signs with its identity key, that claim is only
  as good as the process that wrote it.
* A model observer costs a call per turn. The CLI backend is free of API
  credits but slow under load — minutes per run, not seconds.
