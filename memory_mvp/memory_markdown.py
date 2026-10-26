"""markdown.v1: a memory layer whose store is a markdown file per agent.

Swap-in replacement for the default `kv.v1` memory layer. Every
`api.remember(key, value)` call rewrites that agent's own
`memory/<agent>.md`, and every `api.recall(key)` reads the value back
*out of that file*. The file is the memory, not a log of it: edit a row
and the agent believes the new value; delete it and the agent forgets.

Each file carries two sections.

  ## Facts         the current state, one row per key, rewritten in place
  ## Observations  an append-only log of what the agent did, with
                   logical timestamps

Values are stored as JSON so they round-trip with their type. The trace
records that a write happened and to which file, never the value
itself, so an exported bundle cannot leak private state through the
memory layer (the same property `kv.v1` holds).

Two environment knobs:

  TOWN_MEMORY_DIR      where the files live (default: ./memory)
  TOWN_MEMORY_PERSIST  1 to carry memory across runs instead of
                       starting each run from empty files

Also defines the two roles and the validator for memory_mvp.yaml.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from typing import Any

from nandatown.layers import register
from nandatown.sim.agents import SimAgent, role
from nandatown.sim.validators import Trace, _check, validator

HERE = os.path.dirname(os.path.abspath(__file__))
MEMORY_DIR = os.environ.get("TOWN_MEMORY_DIR") or os.path.join(HERE, "memory")
SCHEMA_PATH = (os.environ.get("TOWN_MEMORY_SCHEMA")
               or os.path.join(HERE, "memory_schema.json"))
KEY_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,59}$")
PERSIST = os.environ.get("TOWN_MEMORY_PERSIST", "0").lower() not in (
    "", "0", "false", "no")

_FACT_ROW = re.compile(r"^\|\s*`(?P<key>[^`]+)`\s*\|\s*`(?P<value>.*)`"
                       r"\s*\|\s*t=(?P<at>[0-9.]+)\s*\|$")
_LOG_ROW = re.compile(r"^- t=[0-9.]+ · ")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-") or "agent"


def _encode(value: Any) -> str:
    """JSON on one line, with table-breaking pipes escaped."""
    return json.dumps(value, sort_keys=True, default=str).replace("|", "\\|")


def _decode(text: str) -> Any:
    try:
        return json.loads(text.replace("\\|", "|"))
    except json.JSONDecodeError:
        return None


class RejectedWrite(ValueError):
    """The value did not match what this key is allowed to hold."""


class MemoryContract:
    """Types per key, checked at the moment of writing.

    Rejection is not a failure of the run: the writer is told what was
    wrong and can write again. What must never happen is a malformed
    value entering the store quietly, because every later reader
    inherits it.
    """

    def __init__(self, path: str = SCHEMA_PATH):
        self.path = path
        try:
            with open(path) as f:
                spec = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            spec = {}
        self.keys: dict[str, dict] = spec.get("keys", {})
        self.patterns: dict[str, dict] = spec.get("patterns", {})
        self.default: dict = spec.get("default",
                                      {"type": "string", "max_length": 200})
        self.declared = bool(self.keys)

    def rule_for(self, key: str) -> dict:
        """Exact name first, then the shape of the name, then the
        default. Binding by shape is what stops a writer from escaping
        the contract by inventing a key nobody declared."""
        if key in self.keys:
            return self.keys[key]
        for pattern, rule in self.patterns.items():
            if fnmatch.fnmatchcase(key, pattern):
                return rule
        return self.default

    def check(self, key: str, value: Any, agents: set[str]) -> Any:
        if not KEY_SHAPE.match(key or ""):
            raise RejectedWrite(
                f"key {key!r} is not a usable key: letters, digits, _ . -"
                " only, 60 characters at most")
        rule = self.rule_for(key)
        kind = rule.get("type", "string")

        if kind in ("integer", "number"):
            number = _as_number(value, kind)
            if number is None:
                raise RejectedWrite(
                    f"key {key!r} holds {kind}s; got {value!r}. Write the"
                    f" bare number and put any commentary in a separate"
                    f" key.")
            if "min" in rule and number < rule["min"]:
                raise RejectedWrite(
                    f"key {key!r} must be at least {rule['min']}")
            if "max" in rule and number > rule["max"]:
                raise RejectedWrite(
                    f"key {key!r} must be at most {rule['max']}")
            return number

        if kind == "boolean":
            if isinstance(value, bool):
                return value
            if str(value).strip().lower() in ("true", "false"):
                return str(value).strip().lower() == "true"
            raise RejectedWrite(f"key {key!r} holds true or false")

        if kind == "agent":
            name = str(value).strip()
            if name not in agents:
                raise RejectedWrite(
                    f"key {key!r} holds the name of one agent in this run"
                    f" ({', '.join(sorted(agents))}); got {value!r}")
            return name

        text = value if isinstance(value, str) else json.dumps(
            value, sort_keys=True, default=str)
        limit = rule.get("max_length", 200)
        if len(text) > limit:
            raise RejectedWrite(
                f"key {key!r} holds at most {limit} characters; got"
                f" {len(text)}. Keep facts short and split them across"
                f" keys rather than writing prose.")
        return text


def _as_number(value: Any, kind: str):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) or (kind == "number"
                                  and isinstance(value, float)):
        return value
    text = str(value).strip().replace(",", "")
    try:
        return int(text) if kind == "integer" else float(text)
    except ValueError:
        return None


@register("memory", "markdown.v1")
class MarkdownMemory:
    """markdown.v1: per-agent memory persisted as a human-readable file."""

    # The layer reacts to the town's own lifecycle events.
    subscribes_to = ("run_created", "run_finished")

    def __init__(self, engine):
        self.engine = engine
        self.dir = MEMORY_DIR
        self.paths: dict[str, str] = {}
        self.log: dict[str, list[str]] = {}
        self.carried: set[str] = set()
        self.finished_at: float | None = None
        self.contract = MemoryContract()
        os.makedirs(self.dir, exist_ok=True)

    # -- the memory contract (same shape as kv.v1) ----------------------

    def remember(self, agent: str, key: str, value: Any) -> None:
        names = {a.name for a in self.engine.spec.agents}
        try:
            value = self.contract.check(key, value, names)
        except RejectedWrite as why:
            # Attributed, and visible in the bundle: a rejected write is
            # evidence about the writer, not a silent no-op.
            self.engine.emit(agent, "memory_write_rejected", agent,
                             {"key": key, "reason": str(why),
                              "expected": self.contract.rule_for(key)})
            self._append(agent, f"write to `{key}` refused: {why}")
            self._write(agent, self._read(agent)["facts"])
            raise
        facts = self._read(agent)["facts"]
        facts[key] = (_encode(value), self.engine.now)
        self._append(agent, f"remembered `{key}`")
        size = self._write(agent, facts)
        self.engine.emit(agent, "memory_written", agent,
                         {"key": key, "file": self._rel(agent),
                          "facts": len(facts), "bytes": size})

    def recall(self, agent: str, key: str) -> Any:
        facts = self._read(agent)["facts"]
        hit = key in facts
        self.engine.emit(agent, "memory_recalled", agent,
                         {"key": key, "hit": hit, "source": "file",
                          "file": self._rel(agent)})
        return _decode(facts[key][0]) if hit else None

    def keys(self, agent: str) -> list[str]:
        """Every key this agent has written. An agent that can list its
        own memory never has to guess a key name."""
        facts = self._read(agent)["facts"]
        self.engine.emit(agent, "memory_listed", agent,
                         {"keys": len(facts), "file": self._rel(agent)})
        return sorted(facts)

    def snapshot(self, agent: str) -> dict[str, Any]:
        facts = self._read(agent)["facts"]
        self.engine.emit(agent, "memory_listed", agent,
                         {"keys": len(facts), "file": self._rel(agent)})
        return {key: _decode(value) for key, (value, _) in facts.items()}

    # -- an extension beyond kv.v1: prose the agent wants to keep -------

    def note(self, agent: str, text: str) -> None:
        facts = self._read(agent)["facts"]
        self._append(agent, text)
        self._write(agent, facts)

    # -- lifecycle -------------------------------------------------------

    def on_event(self, event) -> None:
        if event.kind == "run_created":
            for spec in self.engine.spec.agents:
                prior = (self._read(spec.name) if PERSIST
                         else {"facts": {}, "log": []})
                self.log[spec.name] = list(prior["log"])
                if prior["facts"] or prior["log"]:
                    self.carried.add(spec.name)
                    self._append(spec.name,
                                 f"— new run `{self.engine.run_id}`;"
                                 f" memory carried over —")
                self._write(spec.name, prior["facts"])
        elif event.kind == "run_finished":
            self.finished_at = event.at
            for name in list(self.paths):
                self._write(name, self._read(name)["facts"])

    # -- file store ------------------------------------------------------

    def path_for(self, agent: str) -> str:
        path = self.paths.get(agent)
        if path is None:
            path = os.path.join(self.dir, f"{_slug(agent)}.md")
            self.paths[agent] = path
        return path

    def _rel(self, agent: str) -> str:
        return os.path.relpath(self.path_for(agent), HERE)

    def _append(self, agent: str, text: str) -> None:
        self.log.setdefault(agent, []).append(
            f"- t={self.engine.now:.2f} · {text}")

    def _read(self, agent: str) -> dict[str, Any]:
        """Parse the agent's file. The file is the source of truth."""
        facts: dict[str, tuple[str, float]] = {}
        log: list[str] = []
        try:
            with open(self.path_for(agent)) as f:
                text = f.read()
        except FileNotFoundError:
            return {"facts": facts, "log": log}
        for raw in text.splitlines():
            line = raw.strip()
            match = _FACT_ROW.match(line)
            if match:
                facts[match["key"]] = (match["value"], float(match["at"]))
            elif _LOG_ROW.match(line):
                log.append(line)
        return {"facts": facts, "log": log}

    def _write(self, agent: str, facts: dict[str, tuple[str, float]]) -> int:
        text = self._render(agent, facts)
        with open(self.path_for(agent), "w") as f:
            f.write(text)
        return len(text.encode())

    def _render(self, agent: str, facts: dict[str, tuple[str, float]]) -> str:
        out = [f"# memory: {agent}", ""]
        out.append(f"- run `{self.engine.run_id}` ·"
                   f" scenario `{self.engine.spec.name}`")
        out.append("- store: `markdown.v1` — this file *is* the memory;"
                   " editing it changes what the agent knows")
        if agent in self.carried:
            out.append("- carried over from an earlier run"
                       " (TOWN_MEMORY_PERSIST=1)")
        if self.finished_at is not None:
            out.append(f"- run finished at t={self.finished_at:.2f}")
        out += ["", "## Facts", ""]
        if facts:
            out += ["| key | value | updated |", "| --- | --- | --- |"]
            for key in sorted(facts):
                value, at = facts[key]
                out.append(f"| `{key}` | `{value}` | t={at:.2f} |")
        else:
            out.append("_nothing remembered yet._")
        out += ["", "## Observations", ""]
        out += self.log.get(agent) or ["_nothing observed yet._"]
        return "\n".join(out) + "\n"


# -- the two agents ----------------------------------------------------


class MemoryAgent(SimAgent):
    """Shared helper: direct access to the memory layer for prose notes."""

    @property
    def memory(self):
        return self.api._engine.layers["memory"]

    def note(self, text: str) -> None:
        note = getattr(self.memory, "note", None)
        if callable(note):
            note(self.name, text)


@role("asker")
class Asker(MemoryAgent):
    """Round 1: asks, then remembers. Round 2: recalls instead of asking."""

    def on_start(self):
        self.round = 0
        self.sku = self.config.get("sku", "widget")
        self.rounds = int(self.config.get("rounds", 2))
        self.quantity = int(self.config.get("quantity", 2))
        self.api.register([f"buy.{self.sku}"])
        self.api.later(0.5, self.start_round)

    @property
    def price_key(self) -> str:
        return f"{self.sku}_unit_cents"

    def start_round(self):
        self.round += 1
        self.api.observe("round_started", self.name, {"round": self.round})
        seller = self.api.recall("preferred_seller")
        price = self.api.recall(self.price_key)
        if seller is None or price is None:
            cards = self.api.lookup(f"sell.{self.sku}")
            if not cards:
                self.api.observe("asker_gave_up", self.name,
                                 {"reason": "no seller in the registry"})
                return
            target = cards[0]["name"]
            self.note(f"round {self.round}: nothing remembered about"
                      f" {self.sku}; asking {target}")
            self.api.send(target, "price_question", {"sku": self.sku})
            return
        self.note(f"round {self.round}: recalled {self.sku} at {price} cents"
                  f" from {seller}; ordering straight from memory")
        self.api.observe("question_skipped", self.name,
                         {"round": self.round, "key": self.price_key})
        self.api.send(seller, "order",
                      {"sku": self.sku, "quantity": self.quantity,
                       "unit_cents": price, "round": self.round})
        if self.round < self.rounds:
            self.api.later(1.0, self.start_round)

    def handle_price_answer(self, msg):
        body = msg["body"]
        self.note(f"{msg['sender']} answered: {body['sku']} at"
                  f" {body['unit_cents']} cents each")
        self.api.remember("preferred_seller", msg["sender"])
        self.api.remember(self.price_key, body["unit_cents"])
        if self.round < self.rounds:
            self.api.later(1.0, self.start_round)

    def handle_order_confirmed(self, msg):
        body = msg["body"]
        self.note(f"{msg['sender']} confirmed the order:"
                  f" {body['total_cents']} cents total")
        self.api.remember("last_order_total_cents", body["total_cents"])
        self.api.observe("order_confirmed", self.name,
                         {"round": body.get("round"),
                          "matched": body.get("matched")})


@role("answerer")
class Answerer(MemoryAgent):
    """Answers the price question and keeps its own side of the story."""

    def on_start(self):
        self.answered = 0
        self.filled = 0
        self.sku = self.config.get("sku", "widget")
        self.unit_cents = int(self.config.get("unit_cents", 1995))
        self.api.register([f"sell.{self.sku}"], {"sku": self.sku})
        self.note(f"open for business: {self.sku} at"
                  f" {self.unit_cents} cents each")

    def handle_price_question(self, msg):
        self.answered += 1
        self.note(f"{msg['sender']} asked what {self.sku} costs; quoted"
                  f" {self.unit_cents} cents")
        self.api.remember("last_question_from", msg["sender"])
        self.api.remember("questions_answered", self.answered)
        self.api.reply(msg, "price_answer",
                       {"sku": self.sku, "unit_cents": self.unit_cents})

    def handle_order(self, msg):
        body = msg["body"]
        matched = body.get("unit_cents") == self.unit_cents
        quantity = int(body.get("quantity", 0))
        self.filled += 1
        self.note(f"{msg['sender']} ordered {quantity} x {self.sku} without"
                  f" asking the price again; their remembered price"
                  f" {'matched' if matched else 'did not match'} mine")
        self.api.remember("last_buyer", msg["sender"])
        self.api.remember("orders_filled", self.filled)
        self.api.observe("order_priced_from_memory", msg["sender"],
                         {"matched": matched, "round": body.get("round")})
        self.api.reply(msg, "order_confirmed",
                       {"sku": self.sku, "quantity": quantity,
                        "total_cents": self.unit_cents * quantity,
                        "matched": matched, "round": body.get("round")})


# -- what the run has to prove ------------------------------------------


@validator("memory_mvp")
def memory_mvp(spec, trace: Trace):
    names = {a.name for a in spec.agents}
    stages = []

    writes = trace.find("memory_written")
    write_ids = [e.event_id for e in writes]
    stages.append(_check(
        "both_agents_wrote_memory", names <= {e.observer for e in writes},
        write_ids, "each agent must write at least one memory of its own"))

    files = {e.observer: e.detail.get("file") for e in writes
             if e.detail.get("bytes", 0) > 0}
    stages.append(_check(
        "one_file_per_agent",
        set(files) == names and len(set(files.values())) == len(names),
        write_ids,
        "each agent's memory must land in its own non-empty file"))

    hits = [e for e in trace.find("memory_recalled") if e.detail.get("hit")]
    stages.append(_check(
        "recalled_from_file",
        bool(hits) and all(e.detail.get("source") == "file" for e in hits),
        [e.event_id for e in hits],
        "a later round must read its facts back out of the markdown file"))

    rounds = trace.find("round_started")
    skipped = trace.find("question_skipped")
    questions = trace.find("message_sent", kind="price_question")
    # Cold start asks once and then never again; a run that inherited a
    # memory file (TOWN_MEMORY_PERSIST=1) must not ask at all.
    stages.append(_check(
        "recalled_without_asking",
        bool(rounds) and len(questions) <= 1
        and len(skipped) == len(rounds) - len(questions),
        [e.event_id for e in rounds + skipped + questions],
        "the asker must ask at most once and order from memory thereafter"))

    priced = trace.find("order_priced_from_memory")
    stages.append(_check(
        "remembered_price_was_right",
        bool(priced) and all(e.detail.get("matched") for e in priced),
        [e.event_id for e in priced],
        "the price recalled from the file must match the seller's real price"))

    memory_events = [e for e in trace.events if e.kind.startswith("memory_")]
    leaks = [e for e in memory_events
             if isinstance(e.detail, dict) and "value" in e.detail]
    stages.append(_check(
        "values_stayed_out_of_the_trace", not leaks,
        [e.event_id for e in memory_events],
        "memory events may carry keys and file paths, never the values"))
    return stages
