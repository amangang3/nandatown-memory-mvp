"""Capability 1, lane B — the external observer, and the gap it exposes.

The agent writes what it chose to remember. This reads the raw event
trace of the same run and decides independently what each agent *should*
now know, with a quote from the trace behind every claim. The two lanes
are then compared, and the disagreement is the output worth having:

  agreed        both lanes hold the same value for a key
  omission      the trace supports a fact the agent never wrote down
  contradiction the lanes hold different values for the same key
  unsupported   the agent asserted something the trace does not back

The observer never reads agent memory unless the policy allows it. In the
cross-enterprise preset it cannot, which is the point: traces are all you
get when the counterparty will not let you define how its agents record
anything.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .brains import make_brain

SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]{12,}|gh[pousr]_[A-Za-z0-9]{10,}"
                    r"|Bearer\s+[A-Za-z0-9._\-]{10,})")

# Events that say what happened between agents. Internal memory events are
# deliberately excluded: the observer works from the conversation, not from
# the thing it is auditing.
WIRE_KINDS = ("message_sent", "message_delivered", "message_dropped",
              "order_priced_from_memory", "order_declined", "order_confirmed",
              "order_refused", "round_started", "question_skipped",
              "card_registered", "run_created", "run_finished")

SYSTEM = """You keep an independent record of what each agent in a marketplace
run should now know, by reading the raw event trace of that run.

You are not the agents. You never see inside their memory. You see only what
crossed the wire: messages, their bodies, and the town's own observations.

Write down a fact only when the trace shows it plainly and a later round would
be worse off without it: a price that was quoted, who sells what, what an order
settled at, whether an order was refused and on what stated grounds.

Do not record: the step-by-step of this round, anything you inferred rather
than read, anything true only for a moment, or anything an agent merely
claimed about itself without the trace bearing it out.

Operations:
- ADD        a fact this holder's record does not have yet.
- UPDATE     the trace shows a value you already recorded has changed. Use
             UPDATE, never a second contradicting ADD.
- INVALIDATE a fact you recorded is no longer true and nothing replaces it.

Rules:
- holder is the agent whose record this belongs to, and must be an agent in
  the run.
- key is short, reusable and specific: `widget_unit_cents`, `preferred_seller`,
  `last_order_total_cents`. Reuse a key you already used for the same thing.
- value is the bare value. Prices are integers in cents, never prose.
- evidence quotes the trace, under 200 characters. A claim with no quote is
  not a claim you may make.
- source names the event id the quote came from, e.g. "ev-21".
- confidence 0.9+ when the trace states it outright, 0.6-0.8 when it follows
  from what was said, below 0.6 do not record it.
- At most 6 operations per turn. Most turns need none; an empty list is the
  usual right answer."""

SCHEMA = {
    "type": "object",
    "properties": {
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string",
                           "enum": ["ADD", "UPDATE", "INVALIDATE"]},
                    "holder": {"type": "string"},
                    "key": {"type": "string"},
                    "value": {"type": ["string", "number", "boolean"]},
                    "subject": {"type": "string"},
                    "evidence": {"type": "string"},
                    "source": {"type": "string"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["op", "holder", "key", "evidence", "source",
                             "confidence", "reason"],
            },
        }
    },
    "required": ["operations"],
}


def redact(text: str) -> str:
    return SECRET.sub("[redacted]", text)


@dataclass
class Turn:
    at: str
    lines: list = field(default_factory=list)

    def render(self) -> str:
        return "\n".join(self.lines)


def turns_from(events) -> list[Turn]:
    """One turn per tick of the run's logical clock."""
    out: list[Turn] = []
    current: Turn | None = None
    for event in events:
        if event.kind not in WIRE_KINDS:
            continue
        at = f"{event.at:.2f}"
        if current is None or current.at != at:
            current = Turn(at=at)
            out.append(current)
        detail = event.detail if isinstance(event.detail, dict) else {}
        body = detail.get("body")
        rendered = f"{event.event_id} t={at} {event.observer} {event.kind}"
        if detail.get("kind"):
            rendered += f" kind={detail['kind']}"
        if detail.get("to"):
            rendered += f" to={detail['to']}"
        if body:
            rendered += f" body={json.dumps(body, sort_keys=True)}"
        extra = {k: v for k, v in detail.items()
                 if k not in ("body", "kind", "to")}
        if extra:
            rendered += f" {json.dumps(extra, sort_keys=True)}"
        current.lines.append(redact(rendered))
    return [t for t in out if t.lines]


# -- the deterministic reader -------------------------------------------


class RuleReader:
    """A stand-in observer: fixed rules over the same trace a model reads.

    It exists so the two-lane design can be exercised with no account and
    no variance. Every rule below is something the trace states outright.
    """

    name = "rules:v1"

    PRICE_IN_REFUSAL = re.compile(
        r"actual price of (\d+) cents", re.I)

    def propose(self, turn: Turn, known: dict, agents: set) -> list[dict]:
        ops: list[dict] = []
        for line in turn.lines:
            # Only what an agent actually sent. The town's delivery echo
            # carries the same `kind=` and would otherwise be read as a
            # second, sender-less copy of the same message.
            if " message_sent " not in line:
                continue
            event_id = line.split(" ", 1)[0]
            body = _body(line)
            if "kind=price_answer" in line:
                buyer = _field(line, "to=")
                seller = line.split()[2]
                sku = body.get("sku", "widget")
                ops.append(_op("ADD", buyer, f"{sku}_unit_cents",
                               body.get("unit_cents"), seller, line, event_id,
                               0.95, "the seller stated this price on the wire"))
                ops.append(_op("ADD", buyer, "preferred_seller", seller,
                               seller, line, event_id, 0.9,
                               "this agent answered a price question"))
            elif "kind=price_question" in line:
                seller = _field(line, "to=")
                buyer = line.split()[2]
                ops.append(_op("ADD", seller, "last_question_from", buyer,
                               buyer, line, event_id, 0.95,
                               "this buyer asked for a price"))
            elif "kind=order_confirmed" in line:
                buyer = _field(line, "to=")
                seller = line.split()[2]
                ops.append(_op("ADD", buyer, "last_order_total_cents",
                               body.get("total_cents"), seller, line,
                               event_id, 0.95, "the seller confirmed this total"))
                ops.append(_op("ADD", seller, "last_buyer", buyer, buyer,
                               line, event_id, 0.9, "this buyer's order settled"))
            elif "kind=order_declined" in line:
                buyer = _field(line, "to=")
                seller = line.split()[2]
                reason = str(body.get("reason", ""))[:180]
                ops.append(_op("ADD", buyer, "last_refusal_reason", reason,
                               seller, line, event_id, 0.95,
                               "the seller refused and gave this reason"))
                price = self.PRICE_IN_REFUSAL.search(reason)
                if price:
                    sku = body.get("sku", "widget")
                    ops.append(_op(
                        "UPDATE", buyer, f"{sku}_unit_cents",
                        int(price.group(1)), seller, line, event_id, 0.95,
                        "the refusal stated the seller's real price, which"
                        " contradicts what the buyer acted on"))
        return [op for op in ops
                if op["holder"] in agents and op.get("value") is not None]


def _op(kind, holder, key, value, subject, line, source, confidence, reason):
    return {
        "op": kind, "holder": holder, "key": key, "value": value,
        "subject": subject, "evidence": line[:200], "source": source,
        "confidence": confidence, "reason": reason,
    }


def _field(line: str, prefix: str) -> str:
    for token in line.split():
        if token.startswith(prefix):
            return token[len(prefix):]
    return ""


def _body(line: str) -> dict:
    marker = " body="
    if marker not in line:
        return {}
    tail = line.split(marker, 1)[1]
    depth, end = 0, len(tail)
    for i, ch in enumerate(tail):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    try:
        return json.loads(tail[:end])
    except json.JSONDecodeError:
        return {}


# -- the model-driven reader --------------------------------------------


class ModelReader:
    def __init__(self, brain):
        self.brain = brain
        self.name = brain.name

    def propose(self, turn: Turn, known: dict, agents: set) -> list[dict]:
        lines = "\n".join(
            f"- {holder}/{key} = {json.dumps(value)}"
            for holder, facts in sorted(known.items())
            for key, value in sorted(facts.items())) or "(nothing yet)"
        prompt = (f"AGENTS IN THIS RUN: {', '.join(sorted(agents))}\n\n"
                  f"WHAT YOU HAVE ALREADY RECORDED:\n{lines}\n\n"
                  f"=== TRACE, t={turn.at} ===\n{turn.render()}\n=== END ===\n\n"
                  "Return the memory operations this turn justifies.")
        data = self.brain.complete(SYSTEM, prompt, SCHEMA)
        ops = []
        for op in data.get("operations", []):
            try:
                if float(op.get("confidence", 0)) < 0.6:
                    continue
            except (TypeError, ValueError):
                continue
            if op.get("holder") in agents:
                ops.append(op)
        return ops[:6]


def make_reader(model: str | None = None):
    brain = make_brain(model)
    return RuleReader() if brain is None else ModelReader(brain)


# -- comparing the two lanes ----------------------------------------------


def reconcile(self_facts: dict, observed_facts: dict) -> dict:
    """Compare one agent's self-reported memory against the observed record.

    Returns counts plus the findings themselves, so a validator can assert
    on them and a reader can see what the disagreement actually was.
    """
    findings: list[dict] = []
    for key in sorted(set(self_facts) | set(observed_facts)):
        mine = self_facts.get(key, _MISSING)
        theirs = observed_facts.get(key, _MISSING)
        if mine is _MISSING:
            findings.append({"kind": "omission", "key": key})
        elif theirs is _MISSING:
            findings.append({"kind": "unsupported", "key": key})
        elif _same(mine, theirs):
            findings.append({"kind": "agreed", "key": key})
        else:
            findings.append({"kind": "contradiction", "key": key})
    counts = {kind: sum(f["kind"] == kind for f in findings)
              for kind in ("agreed", "omission", "contradiction",
                           "unsupported")}
    return {"counts": counts, "findings": findings}


class _Missing:
    def __repr__(self):
        return "<missing>"


_MISSING = _Missing()


def _same(a, b) -> bool:
    if a == b:
        return True
    # "1995 cents per unit as of last quote" vs 1995: the agent wrote prose
    # where the observer recorded a number. Same fact, different discipline.
    if isinstance(b, (int, float)) and isinstance(a, str):
        found = re.findall(r"-?\d+", a)
        return bool(found) and int(found[0]) == int(b)
    if isinstance(a, (int, float)) and isinstance(b, str):
        return _same(b, a)
    return str(a).strip().lower() == str(b).strip().lower()
