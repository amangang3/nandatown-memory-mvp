"""The `git.v1` memory layer: the four capabilities wired into one plugin.

Drop-in for nandatown's `memory` layer. `remember` and `recall` keep the
shape every existing scenario expects; everything else is additive.

    remember(agent, key, value)   lane A, the agent's own write
    recall(agent, key)            the holder reading its own memory
    snapshot(agent) / keys(agent) the holder listing its own memory
    read_as(reader, holder, key)  anyone else, decided by policy
    history(holder, key)          every version, with who and when
    observe(events)               lane B, run by the harness at run end

Run lifecycle:
  run_created   open the store, report HEAD, and check for facts edited
                outside the store since the last run — the tamper signal
  run_finished  run the observation lane over the trace, reconcile it
                against what the agents wrote, and commit a run report
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Any

from nandatown.layers import register

from .brains import ObserverError
from .contract import Contract, RejectedWrite
from .observer import make_reader, reconcile, turns_from
from .policy import Policy
from .store import Store

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE_DIR = os.environ.get("TOWN_GITMEM_DIR") or os.path.join(
    HERE, "memory-git")
RESET = os.environ.get("TOWN_GITMEM_RESET", "0").lower() not in (
    "", "0", "false", "no")


@register("memory", "git.v1")
class GitMemory:
    """Two capture lanes, one write contract, git history, read policy."""

    subscribes_to = ("run_created", "run_finished")

    def __init__(self, engine):
        self.engine = engine
        self.policy = Policy.load()
        self.contract = Contract()
        self.store = Store(STORE_DIR)
        if RESET and os.path.isdir(STORE_DIR):
            shutil.rmtree(STORE_DIR)
        self.store.init()
        self.reader = None
        self.last_report: dict = {}

    # -- helpers -----------------------------------------------------------

    @property
    def agents(self) -> set:
        return {a.name for a in self.engine.spec.agents}

    def _when(self) -> str:
        return f"{self.engine.now:.2f}"

    def _emit(self, observer: str, kind: str, subject: str,
              detail: dict) -> None:
        self.engine.emit(observer, kind, subject, detail)

    def _write(self, op: dict, title: str) -> str | None:
        """Contract, then store, then commit. Returns the commit id."""
        lane = op.get("lane", "self")
        allowed = self.policy.may_capture(lane)
        if not allowed.allowed:
            self._emit(op.get("holder", "town"), "memory_write_blocked",
                       op.get("holder", "town"),
                       {"lane": lane, "key": op.get("key", ""),
                        "rule": allowed.rule, "reason": allowed.reason})
            raise RejectedWrite(allowed.reason)

        checked = self.contract.check(op, self.agents)
        # An observed fact is dated when the thing happened, not when the
        # observer got round to reading about it.
        when = str(op.get("when") or self._when())
        source = checked.get("source") or f"{self.engine.run_id}:t={when}"
        checked["source"] = source
        changes = self.store.apply([checked], source=source, when=when)
        if not changes:
            return None
        change = changes[0]
        if change.op == "NOOP":
            # The writer still asserted it; the store simply had nothing
            # to change. Record the act, not a new version.
            self._emit(checked["asserter"], "memory_write_noop",
                       checked["holder"],
                       {"key": checked["key"], "lane": lane,
                        "fact": change.fact.id,
                        "reason": "this value is already current"})
            return None
        commit = self.store.commit(changes, source=source, title=title,
                                   when=when)
        self._emit(
            checked["asserter"],
            "memory_written" if lane == "self" else "memory_observed",
            checked["holder"],
            {"key": checked["key"], "lane": lane, "op": change.op,
             "fact": change.fact.id, "supersedes": change.fact.supersedes,
             "commit": commit, "scope": checked.get("scope", "private"),
             "confidence": checked.get("confidence", 1.0),
             "asserter": checked["asserter"],
             "evidence_chars": len(str(checked.get("evidence", ""))),
             "source": source})
        return commit

    # -- lane A: the agent writes ------------------------------------------

    def remember(self, agent: str, key: str, value: Any,
                 subject: str = "", scope: str | None = None) -> None:
        op = {"op": "ADD", "lane": "self", "holder": agent, "asserter": agent,
              "key": key, "value": value, "subject": subject or agent,
              "scope": scope or "private", "confidence": 1.0,
              "reason": "the agent chose to record this"}
        try:
            self._write(op, title=f"{agent} remembers {key}")
        except RejectedWrite as why:
            self._emit(agent, "memory_write_rejected", agent,
                       {"key": key, "lane": "self", "reason": str(why),
                        "expected": self.contract.rule_for(key)})
            raise

    def recall(self, agent: str, key: str) -> Any:
        fact = self.store.current(agent, key, lane="self")
        self._emit(agent, "memory_recalled", agent,
                   {"key": key, "hit": fact is not None, "lane": "self",
                    "fact": fact.id if fact else None})
        return fact.value if fact else None

    def keys(self, agent: str) -> list:
        return sorted(f.key for f in self.store.active(agent, "self"))

    def snapshot(self, agent: str) -> dict:
        facts = self.store.active(agent, "self")
        self._emit(agent, "memory_listed", agent,
                   {"keys": len(facts), "lane": "self"})
        return {f.key: f.value for f in facts}

    # -- capability 4: everyone else's reads --------------------------------

    def read_as(self, reader: str, holder: str,
                key: str | None = None) -> dict | None:
        """A read by someone who is not the holder. Policy decides, and the
        decision is recorded either way."""
        facts = [f for f in self.store.active(holder)
                 if key is None or f.key == key]
        probe = facts[0] if facts else _Probe(holder)
        decision = self.policy.may_read(reader, probe)
        self._emit(reader,
                   "memory_access_allowed" if decision.allowed
                   else "memory_access_denied",
                   holder,
                   {"reader": reader, "holder": holder, "key": key or "*",
                    "rule": decision.rule, "reason": decision.reason,
                    "keys": len(facts) if decision.allowed else 0})
        if not decision.allowed:
            return None
        visible = {}
        for fact in facts:
            if self.policy.may_read(reader, fact).allowed:
                visible[fact.key] = fact.value
        return visible

    # -- capability 3: history ----------------------------------------------

    def history(self, holder: str, key: str) -> list:
        return [
            {"fact": f.id, "status": f.status, "value": f.value,
             "asserter": f.asserter, "lane": f.lane, "valid_from": f.valid_from,
             "invalid_at": f.invalid_at, "supersedes": f.supersedes,
             "evidence": f.evidence, "source": f.source}
            for f in self.store.history(holder, key)
        ]

    # -- lifecycle ------------------------------------------------------------

    def on_event(self, event) -> None:
        if event.kind == "run_created":
            self._open()
        elif event.kind == "run_finished":
            self._close()

    def _open(self) -> None:
        dirty = self.store.dirty()
        self._emit("town", "memory_store_opened", self.engine.run_id,
                   {"head": self.store.head(), "preset": self.policy.preset,
                    "lanes": list(self.policy.capture_lanes),
                    "facts": len(self.store.active()),
                    "store": os.path.relpath(STORE_DIR, HERE)})
        if dirty:
            self._emit("town", "memory_tampered", self.engine.run_id,
                       {"files": dirty[:8], "count": len(dirty),
                        "detail": "fact files were changed outside the store"
                                  " since the last commit"})

    def _close(self) -> None:
        # The harness exercises its own read right before anything else,
        # so a run always records what the policy allows the operator to
        # see — whether or not an agent happened to go looking.
        for agent in sorted(self.agents):
            self.read_as("harness", agent)
        if "observed" not in self.policy.capture_lanes:
            self._emit("town", "memory_observation_skipped", self.engine.run_id,
                       {"reason": "the observed lane is off in this policy"})
            return
        try:
            self.observe(self.engine.events)
        except ObserverError as why:
            self._emit("observer", "memory_observation_failed",
                       self.engine.run_id, {"reason": str(why)[:300]})

    # -- lane B: the observer -------------------------------------------------

    def observe(self, events) -> dict:
        if self.reader is None:
            self.reader = make_reader()
        known: dict = {}
        for fact in self.store.active(lane="observed"):
            known.setdefault(fact.holder, {})[fact.key] = fact.value

        accepted = rejected = unchanged = 0
        for turn in turns_from(events):
            ops = self.reader.propose(turn, known, self.agents)
            for op in ops:
                op = dict(op, lane="observed", asserter="observer",
                          when=turn.at)
                try:
                    commit = self._write(op, title=f"observed at t={turn.at}")
                except RejectedWrite as why:
                    rejected += 1
                    self._emit("observer", "memory_write_rejected",
                               op.get("holder", "town"),
                               {"key": op.get("key", ""), "lane": "observed",
                                "reason": str(why)})
                    continue
                if commit:
                    accepted += 1
                else:
                    unchanged += 1
                known.setdefault(op["holder"], {})[op["key"]] = op.get("value")

        self._emit("observer", "memory_observation_done", self.engine.run_id,
                   {"model": self.reader.name, "accepted": accepted,
                    "unchanged": unchanged, "rejected": rejected,
                    "turns": len(turns_from(events))})
        return self._reconcile()

    # -- the gap between the lanes --------------------------------------------

    def _reconcile(self) -> dict:
        can_compare = self.policy.may_read("observer", _Probe("any"))
        if not can_compare.allowed:
            self._emit("observer", "memory_reconcile_skipped",
                       self.engine.run_id,
                       {"rule": can_compare.rule, "reason": can_compare.reason})
            return {}

        report: dict = {"run": self.engine.run_id,
                        "model": self.reader.name if self.reader else "none",
                        "policy": self.policy.as_dict(), "agents": {}}
        for agent in sorted(self.agents):
            mine = {f.key: f.value
                    for f in self.store.active(agent, "self")}
            theirs = {f.key: f.value
                      for f in self.store.active(agent, "observed")}
            result = reconcile(mine, theirs)
            report["agents"][agent] = result
            self._emit("observer", "memory_reconciled", agent, {
                "self_keys": len(mine), "observed_keys": len(theirs),
                **result["counts"]})
            for finding in result["findings"]:
                if finding["kind"] == "agreed":
                    continue
                self._emit("observer", "memory_reconcile_finding", agent,
                           {"kind": finding["kind"], "key": finding["key"]})

        self.last_report = report
        self._write_report(report)
        return report

    def _write_report(self, report: dict) -> None:
        reports = self.store.root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        path = reports / f"{self.engine.run_id}.json"
        path.write_text(json.dumps(report, indent=2, sort_keys=True))
        try:
            self.store._git("add", "-A", "reports")
            self.store._commit(
                f"reconcile: {self.engine.run_id}\n\n"
                + "\n".join(
                    f"{agent}: " + ", ".join(
                        f"{k} {v}" for k, v in data["counts"].items())
                    for agent, data in sorted(report["agents"].items())),
                when=self._when())
        except RuntimeError:
            pass


class _Probe:
    """A stand-in fact used to ask the policy a question about a holder
    when no concrete fact is to hand."""

    def __init__(self, holder: str):
        self.holder = holder
        self.subject = ""
        self.scope = "private"
        self.lane = "self"
