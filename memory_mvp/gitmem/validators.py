"""Stages: one or more per capability, computed from the trace alone.

Capability 1  both lanes captured, and compared
Capability 2  one contract in front of both, refusals explained
Capability 3  every accepted change committed, history queryable
Capability 4  every read decided by policy, nothing leaked against it
"""

from __future__ import annotations

from nandatown.records import StageResult
from nandatown.sim.validators import Trace, _check, validator


def _not_tested(name: str, note: str) -> StageResult:
    return StageResult(name=name, status="not_tested", note=note)


def _policy(trace: Trace) -> dict:
    opened = trace.find("memory_store_opened")
    return opened[-1].detail if opened else {}


@validator("memory_combined")
def memory_combined(spec, trace: Trace):
    names = {a.name for a in spec.agents}
    opened = _policy(trace)
    lanes = set(opened.get("lanes", ["self", "observed"]))
    stages = []

    # -- capability 1: two lanes -------------------------------------------

    self_writes = trace.find("memory_written")
    observed = trace.find("memory_observed")
    blocked = trace.find("memory_write_blocked")

    if "self" in lanes and not self_writes and not blocked:
        # A warm store is allowed to be quiet: an agent that already knows
        # everything this round has nothing new to write.
        stages.append(_not_tested(
            "lane_self_captured",
            "no agent had anything new to record in this run"))
    elif "self" in lanes:
        stages.append(_check(
            "lane_self_captured",
            bool(self_writes) and all(e.observer == e.subject
                                      for e in self_writes),
            [e.event_id for e in self_writes],
            "a self-reported fact must be written by the agent that holds"
            " it, and at least one must have been accepted"))
    else:
        stages.append(_check(
            "lane_self_blocked_by_policy",
            bool(blocked) and all(e.detail.get("lane") == "self"
                                  for e in blocked),
            [e.event_id for e in blocked],
            "with the self lane off, an agent's own write must be refused"
            " and the refusal recorded"))

    ran = trace.find("memory_observation_done")
    if not observed and ran:
        # The observer ran and found nothing new: a settled store, not a
        # broken lane. Say which, rather than reporting no evidence.
        stages.append(_not_tested(
            "lane_observed_captured",
            "the observer ran and had nothing new to record"
            f" ({ran[-1].detail.get('unchanged', 0)} value(s) already"
            f" current, {ran[-1].detail.get('rejected', 0)} refused)"))
        stages.append(_not_tested(
            "observed_facts_carry_evidence",
            "no observed fact was written in this run"))
    else:
        stages.append(_check(
            "lane_observed_captured",
            bool(observed) and all(e.observer == "observer" for e in observed),
            [e.event_id for e in observed],
            "the observation lane must record facts of its own, asserted by"
            " the observer rather than by an agent"))
        stages.append(_check(
            "observed_facts_carry_evidence",
            bool(observed) and all(e.detail.get("evidence_chars", 0) > 0
                                   for e in observed),
            [e.event_id for e in observed],
            "every observed fact must quote the trace it came from"))

    reconciled = trace.find("memory_reconciled")
    skipped = trace.find("memory_reconcile_skipped")
    if skipped:
        # A policy that forbids the comparison is the policy working, not
        # the system failing. Say so, and say which rule did it.
        stages.append(_not_tested(
            "lanes_compared",
            "not compared by policy ("
            + str(skipped[0].detail.get("rule", "")) + "): "
            + str(skipped[0].detail.get("reason", ""))))
    else:
        stages.append(_check(
            "lanes_compared", {e.subject for e in reconciled} == names,
            [e.event_id for e in reconciled],
            "every agent's own memory must be compared against what the"
            " observer independently recorded"))

    findings = trace.find("memory_reconcile_finding")
    gaps = [e for e in findings
            if e.detail.get("kind") in ("omission", "contradiction",
                                        "unsupported")]
    if not reconciled:
        stages.append(_not_tested(
            "observer_found_a_gap", "the lanes were never compared"))
    elif not gaps:
        stages.append(_not_tested(
            "observer_found_a_gap",
            "the two lanes agreed on everything in this run"))
    else:
        kinds = {}
        for event in gaps:
            kind = event.detail.get("kind", "?")
            kinds[kind] = kinds.get(kind, 0) + 1
        stages.append(_check(
            "observer_found_a_gap", True, [e.event_id for e in gaps],
            "", "the lanes disagreed: "
                + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))))

    # -- capability 2: the write contract ----------------------------------

    rejected = trace.find("memory_write_rejected")
    all_writes = self_writes + observed
    noop = trace.find("memory_write_noop")
    if not all_writes:
        quiet = ("nothing was written in this run"
                 + (f"; {len(noop)} value(s) were re-asserted unchanged"
                    if noop else ""))
        stages.append(_not_tested("one_contract_for_both_lanes", quiet))
        stages.append(_not_tested("every_change_committed", quiet))
        stages.append(_not_tested("changes_carry_provenance", quiet))
        stages.append(_not_tested("history_is_queryable", quiet))
        return _finish(stages, trace, names, opened, rejected)

    stages.append(_check(
        "one_contract_for_both_lanes",
        bool(all_writes) and all(e.detail.get("lane") in lanes
                                 and e.detail.get("op") and e.detail.get("key")
                                 for e in all_writes),
        [e.event_id for e in all_writes[:8]],
        "every accepted write, from either lane, must have come through the"
        " contract in a lane this policy captures"))

    # -- capability 3: version tracking -------------------------------------

    writes = all_writes
    stages.append(_check(
        "every_change_committed",
        bool(writes) and all(e.detail.get("commit") for e in writes),
        [e.event_id for e in writes[:8]],
        "every accepted change must land in a git commit"))

    stages.append(_check(
        "changes_carry_provenance",
        bool(writes) and all(e.detail.get("asserter") and e.detail.get("source")
                             for e in writes),
        [e.event_id for e in writes[:8]],
        "every change must name who asserted it and what it came from"))

    chained = [e for e in writes if e.detail.get("supersedes")]
    if not chained:
        stages.append(_not_tested(
            "history_is_queryable",
            "no fact was rewritten in this run, so nothing superseded"))
    else:
        stages.append(_check(
            "history_is_queryable",
            all(e.detail.get("op") == "UPDATE" for e in chained),
            [e.event_id for e in chained],
            "a rewritten fact must link to the version it replaced"))

    return _finish(stages, trace, names, opened, rejected, writes=all_writes)


def _finish(stages, trace, names, opened, rejected, writes=None):
    if rejected:
        stages.append(_check(
            "refusals_explain_themselves",
            all(str(e.detail.get("reason", "")).strip() for e in rejected),
            [e.event_id for e in rejected],
            "a refused write must tell the writer why"))
    elif not any(s.name == "refusals_explain_themselves" for s in stages):
        stages.append(_not_tested(
            "refusals_explain_themselves",
            "the contract refused nothing in this run"))

    tampered = trace.find("memory_tampered")
    if tampered:
        stages.append(_check(
            "store_was_clean_at_open", False,
            [e.event_id for e in tampered],
            "fact files were changed outside the store since the last"
            " commit: " + str(tampered[0].detail.get("files", ""))))
    else:
        stages.append(_check(
            "store_was_clean_at_open", True,
            [e.event_id for e in trace.find("memory_store_opened")],
            "the store must be able to say whether it was edited"))

    # -- capability 4: access control ----------------------------------------

    allowed = trace.find("memory_access_allowed")
    denied = trace.find("memory_access_denied")
    decisions = allowed + denied
    stages.append(_check(
        "reads_are_policy_decisions", bool(decisions)
        and all(e.detail.get("rule") for e in decisions),
        [e.event_id for e in decisions],
        "a read by anyone other than the holder must be decided by the"
        " policy, and the rule recorded"))

    cross = [e for e in allowed
             if e.detail.get("reader") in names
             and e.detail.get("holder") in names
             and e.detail.get("reader") != e.detail.get("holder")]
    attempted = [e for e in decisions
                 if e.detail.get("reader") in names
                 and e.detail.get("holder") in names
                 and e.detail.get("reader") != e.detail.get("holder")]
    if not attempted:
        stages.append(_not_tested(
            "agents_kept_out_of_each_others_memory",
            "no agent tried to read another agent's memory in this run"))
    elif opened.get("preset") == "open_lab":
        stages.append(_not_tested(
            "agents_kept_out_of_each_others_memory",
            "this run uses the open_lab preset, where cross-agent reads are"
            " deliberately allowed"))
    else:
        stages.append(_check(
            "agents_kept_out_of_each_others_memory", not cross,
            [e.event_id for e in attempted],
            "no agent may read another agent's memory under this policy"))

    # -- the standing property ------------------------------------------------

    watched = [e for e in trace.events
               if e.kind.startswith("memory_") or e.kind == "model_tool_call"]
    leaks = [e for e in watched
             if isinstance(e.detail, dict) and "value" in e.detail]
    stages.append(_check(
        "values_stayed_out_of_the_trace", not leaks,
        [e.event_id for e in watched[:8]],
        "memory events may carry keys, commits and reasons, never values"))
    return stages


@validator("memory_combined_llm")
def memory_combined_llm(spec, trace: Trace):
    stages = memory_combined(spec, trace)
    configured = trace.find("model_configured")
    stages.insert(0, _check(
        "model_drove_both_agents",
        {e.observer for e in configured} == {a.name for a in spec.agents},
        [e.event_id for e in configured],
        "both agents must be driven by a brain, and say which"))
    return stages
