"""Capability 2 — the writing rules.

One door for both lanes. An agent writing its own memory and an external
observer deriving a fact from the trace are checked by the same contract,
with two differences that follow from where the fact came from:

  * a `self` write must be made by the holder itself — an agent cannot
    write into another agent's memory;
  * an `observed` write must carry evidence (a quote from the trace) and
    name the event it came from, because an observation nobody can check
    is just an opinion with better handwriting.

A refused write is never silent. The reason goes back to the writer, and
the refusal is recorded as an event so the bundle shows what was attempted.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SCHEMA = os.path.join(os.path.dirname(HERE), "memory_schema.json")
KEY_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,59}$")
LANES = ("self", "observed")


class RejectedWrite(ValueError):
    """The write did not meet the contract. Carries the reason verbatim."""


class Contract:
    def __init__(self, path: str | None = None,
                 require_evidence: bool = True):
        self.path = path or os.environ.get("TOWN_MEMORY_SCHEMA") \
            or DEFAULT_SCHEMA
        self.require_evidence = require_evidence
        try:
            with open(self.path) as handle:
                spec = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError):
            spec = {}
        self.keys: dict[str, dict] = spec.get("keys", {})
        self.patterns: dict[str, dict] = spec.get("patterns", {})
        self.default: dict = spec.get("default",
                                      {"type": "string", "max_length": 200})

    # -- rules -------------------------------------------------------------

    def rule_for(self, key: str) -> dict:
        """Exact name first, then the shape of the name, then the default.
        Binding by shape is what stops a writer escaping the contract by
        inventing a key nobody declared."""
        if key in self.keys:
            return self.keys[key]
        for pattern, rule in self.patterns.items():
            if fnmatch.fnmatchcase(key, pattern):
                return rule
        return self.default

    def check(self, op: dict, agents: set[str]) -> dict:
        """Validate one operation and return it normalised.

        Raises RejectedWrite with a reason a writer can act on.
        """
        lane = op.get("lane", "self")
        if lane not in LANES:
            raise RejectedWrite(f"unknown lane {lane!r}; expected one of "
                                f"{', '.join(LANES)}")

        holder = str(op.get("holder", ""))
        if holder not in agents:
            raise RejectedWrite(
                f"holder {holder!r} is not an agent in this run "
                f"({', '.join(sorted(agents))})")

        asserter = str(op.get("asserter") or holder)
        if lane == "self" and asserter != holder:
            raise RejectedWrite(
                f"{asserter!r} cannot write into {holder!r}'s own memory;"
                f" a self-reported fact is written by its holder or not at all")
        if lane == "observed":
            if asserter in agents:
                raise RejectedWrite(
                    "an observed fact is asserted by the observer, not by an"
                    " agent in the run")
            if self.require_evidence and not str(op.get("evidence", "")).strip():
                raise RejectedWrite(
                    "an observed fact must quote the trace it came from;"
                    " write the supporting line into `evidence`")
            if not str(op.get("source", "")).strip():
                raise RejectedWrite(
                    "an observed fact must name the event it came from in"
                    " `source`")

        kind = (op.get("op") or "ADD").upper()
        if kind not in ("ADD", "UPDATE", "INVALIDATE"):
            raise RejectedWrite(f"unknown operation {kind!r}")

        key = str(op.get("key", ""))
        if not KEY_SHAPE.match(key):
            raise RejectedWrite(
                f"key {key!r} is not a usable key: letters, digits, _ . -"
                f" only, 60 characters at most")

        out = dict(op)
        out["lane"], out["holder"], out["asserter"], out["op"] = \
            lane, holder, asserter, kind
        out["key"] = key

        if kind == "INVALIDATE":
            return out

        rule = self.rule_for(key)
        value = self.coerce(key, op.get("value"), rule, agents)
        out["value"] = value
        out["statement"] = json.dumps(value, ensure_ascii=False, default=str)
        out["type"] = rule.get("type", "string")

        confidence = op.get("confidence", 1.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            raise RejectedWrite("confidence must be a number between 0 and 1")
        if not 0.0 <= confidence <= 1.0:
            raise RejectedWrite("confidence must be between 0 and 1")
        out["confidence"] = confidence

        scope = op.get("scope") or ("private" if lane == "self" else "project")
        if scope not in ("private", "project", "shared"):
            raise RejectedWrite(
                f"scope {scope!r} must be private, project or shared")
        out["scope"] = scope
        return out

    def coerce(self, key: str, value: Any, rule: dict,
               agents: set[str]) -> Any:
        kind = rule.get("type", "string")

        if kind in ("integer", "number"):
            number = _as_number(value, kind)
            if number is None:
                raise RejectedWrite(
                    f"key {key!r} holds {kind}s; got {value!r}. Write the"
                    f" bare number and put any commentary in a separate key.")
            if "min" in rule and number < rule["min"]:
                raise RejectedWrite(f"key {key!r} must be at least {rule['min']}")
            if "max" in rule and number > rule["max"]:
                raise RejectedWrite(f"key {key!r} must be at most {rule['max']}")
            return number

        if kind == "boolean":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in ("true", "false"):
                return text == "true"
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
                f" {len(text)}. Keep facts short and split them across keys"
                f" rather than writing prose.")
        return text


def _as_number(value: Any, kind: str):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) or (kind == "number" and isinstance(value, float)):
        return value
    text = str(value).strip().replace(",", "")
    try:
        return int(text) if kind == "integer" else float(text)
    except ValueError:
        return None
