"""Model-driven agents whose memory is the markdown layer.

Same two-round encounter as the scripted scenario, except nothing is
hardcoded: a model decides whether to consult memory, whether to ask,
what is worth writing down, and when to order. The tools it is given
are the memory layer's own contract plus the town actions:

    read_memory(key)              what do I already know?
    remember(key, value)          keep this for next time
    ask_price(seller, sku)        spend a round trip
    place_order(...)              act on what I know

The agent code holds no vendor message format; see llm_brains.py.
A run with TOWN_LLM_MODEL unset uses the deterministic MockBrain, so
the scenario is reproducible without an account; point it at a real
model to find out whether one actually uses what it wrote down.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from nandatown.records import StageResult
from nandatown.sim.agents import SimAgent, role
from nandatown.sim.validators import Trace, _check, validator

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_brains import make_brain  # noqa: E402

MAX_STEPS = 6
MISS = "(nothing remembered under that key)"


def _coerce(value: Any) -> Any:
    """Models hand back strings; keep numbers numeric in the file."""
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return value


class LlmAgent(SimAgent):
    """A role whose decisions come from a brain, one tool loop per event."""

    agent_role = "base"

    # -- wiring ---------------------------------------------------------

    def start_brain(self) -> None:
        self.round = 0
        self.brain = make_brain(self.agent_role)
        self.api.observe("model_configured", self.name,
                         {"model": self.brain.name, "role": self.agent_role})

    @property
    def memory(self):
        return self.api._engine.layers["memory"]

    def note(self, text: str) -> None:
        note = getattr(self.memory, "note", None)
        if callable(note):
            note(self.name, text)

    # -- the loop --------------------------------------------------------

    def think(self, situation: str) -> None:
        conversation: list[dict[str, Any]] = [
            {"type": "user", "text": situation}]
        for _ in range(MAX_STEPS):
            reply = self.brain.act(self.system_prompt(), conversation,
                                   self.tool_specs())
            calls = reply.get("tool_calls") or []
            if not calls:
                if reply.get("text"):
                    self.note(f"reasoned: {reply['text'][:160]}")
                return
            conversation.append({"type": "tool_calls",
                                 "text": reply.get("text", ""),
                                 "calls": calls})
            results = []
            for call in calls:
                output = self.run_tool(call["name"], call.get("input") or {})
                results.append({"id": call["id"], "name": call["name"],
                                "output": output})
            conversation.append({"type": "tool_results", "results": results})
        self.api.observe("model_step_limit", self.name,
                         {"steps": MAX_STEPS, "round": self.round})

    def run_tool(self, name: str, args: dict[str, Any]) -> str:
        handler = self.tools().get(name)
        # The key is attributed evidence; the value never is.
        self.api.observe("model_tool_call", self.name,
                         {"tool": name, "round": self.round,
                          "key": args.get("key", "")})
        if handler is None:
            return f"error: no tool called {name!r}"
        try:
            return handler(args)
        except Exception as exc:  # a bad tool call is the model's to fix
            return f"error: {type(exc).__name__}: {exc}"

    # -- tools every agent shares ----------------------------------------

    def tool_read_memory(self, args) -> str:
        value = self.api.recall(str(args["key"]))
        return MISS if value is None else json.dumps(value).strip('"')

    def tool_list_memory(self, args) -> str:
        facts = self.memory.snapshot(self.name)
        if not facts:
            return "your memory file is empty"
        return "\n".join(f"{key} = {json.dumps(value)}"
                          for key, value in sorted(facts.items()))

    def tool_remember(self, args) -> str:
        key = str(args["key"])
        self.api.remember(key, _coerce(args.get("value")))
        return f"remembered {key}"

    MEMORY_TOOLS = [
        {"name": "list_memory",
         "description": "List every key you have already written, with"
                        " its value. Call this first: it is cheaper than"
                        " guessing a key name, and a key you cannot find"
                        " is a fact you will pay to learn twice.",
         "schema": {"type": "object", "properties": {}}},
        {"name": "read_memory",
         "description": "Look up one key in your own memory file. Returns"
                        " the value, or says nothing is remembered.",
         "schema": {"type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"]}},
        {"name": "remember",
         "description": "Write one fact to your own memory file so it"
                        " survives into later rounds and later runs.",
         "schema": {"type": "object",
                    "properties": {"key": {"type": "string"},
                                   "value": {"type": "string"}},
                    "required": ["key", "value"]}},
    ]


@role("llm-asker")
class LlmAsker(LlmAgent):
    """Wants widgets. Must decide each round: ask again, or recall?"""

    agent_role = "asker"

    def on_start(self):
        self.start_brain()
        self.sku = self.config.get("sku", "widget")
        self.rounds = int(self.config.get("rounds", 2))
        self.quantity = int(self.config.get("quantity", 2))
        self.sellers: list[str] = []
        self.asked_this_round = False
        self.api.register([f"buy.{self.sku}"])
        self.api.later(0.5, self.start_round)

    def system_prompt(self) -> str:
        return (
            f"You are {self.name}, a purchasing agent in a town of agents."
            f" You need {self.quantity} units of sku `{self.sku}`.\n\n"
            "You have a private memory file. Every round trip to another"
            " agent costs time and money, so check what you already know"
            " before you ask anyone anything, and write down anything"
            " worth knowing next time. Start with list_memory: your own"
            " key names are not guessable later, so read the list rather"
            " than inventing a name twice.\n\n"
            "Work by calling tools. Stop calling tools when your part of"
            " this round is done.")

    def tools(self):
        return {"list_memory": self.tool_list_memory,
                "read_memory": self.tool_read_memory,
                "remember": self.tool_remember,
                "ask_price": self.tool_ask_price,
                "place_order": self.tool_place_order}

    def tool_specs(self):
        return self.MEMORY_TOOLS + [
            {"name": "ask_price",
             "description": "Send a price question to a seller. Costs a"
                            " round trip; the answer arrives later.",
             "schema": {"type": "object",
                        "properties": {"seller": {"type": "string"},
                                       "sku": {"type": "string"}},
                        "required": ["seller", "sku"]}},
            {"name": "place_order",
             "description": "Order from a seller at a price you already"
                            " know. Use this instead of asking again.",
             "schema": {"type": "object",
                        "properties": {"seller": {"type": "string"},
                                       "sku": {"type": "string"},
                                       "quantity": {"type": "integer"},
                                       "unit_cents": {"type": "integer"}},
                        "required": ["seller", "sku", "quantity",
                                     "unit_cents"]}},
        ]

    # -- town actions the model can take ---------------------------------

    def tool_ask_price(self, args) -> str:
        seller = str(args["seller"])
        self.asked_this_round = True
        self.api.send(seller, "price_question",
                      {"sku": str(args.get("sku", self.sku))})
        return f"question sent to {seller}; the answer will arrive later"

    def tool_place_order(self, args) -> str:
        seller = str(args["seller"])
        # Only a round that asked nobody skipped a question. Ordering
        # after asking in the same round is not recall, it is patience.
        if not self.asked_this_round:
            self.api.observe("question_skipped", self.name,
                             {"round": self.round,
                              "key": f"{self.sku}_unit_cents"})
        self.api.send(seller, "order",
                      {"sku": str(args.get("sku", self.sku)),
                       "quantity": int(args.get("quantity", self.quantity)),
                       "unit_cents": int(args["unit_cents"]),
                       "round": self.round})
        return f"order sent to {seller}"

    # -- events ----------------------------------------------------------

    def start_round(self):
        self.round += 1
        self.asked_this_round = False
        self.api.observe("round_started", self.name, {"round": self.round})
        self.sellers = [c["name"] for c in self.api.lookup(f"sell.{self.sku}")]
        if not self.sellers:
            self.api.observe("asker_gave_up", self.name,
                             {"reason": "no seller in the registry"})
            return
        self.think(
            f"Round {self.round} of {self.rounds} begins. The town registry"
            f" lists one seller called `{self.sellers[0]}` for sku"
            f" `{self.sku}`. You need {self.quantity} units.")

    def handle_price_answer(self, msg):
        body = msg["body"]
        self.think(
            f"A price answer arrived: the seller called `{msg['sender']}`"
            f" quotes sku `{body['sku']}` at {body['unit_cents']} cents per"
            f" unit. You need {self.quantity} units.")
        if self.round < self.rounds:
            self.api.later(1.0, self.start_round)

    def handle_order_declined(self, msg):
        body = msg["body"]
        self.note(f"{msg['sender']} refused the order: {body.get('reason')}")
        self.api.observe("order_refused", self.name,
                         {"round": body.get("round"),
                          "reason": body.get("reason")})

    def handle_order_confirmed(self, msg):
        body = msg["body"]
        self.note(f"{msg['sender']} confirmed {body['quantity']} x"
                  f" {body['sku']} for {body['total_cents']} cents")
        self.api.observe("order_confirmed", self.name,
                         {"round": body.get("round"),
                          "matched": body.get("matched")})


@role("llm-answerer")
class LlmAnswerer(LlmAgent):
    """Sells widgets at a fixed price and keeps its own side of the story."""

    agent_role = "answerer"

    def on_start(self):
        self.start_brain()
        self.sku = self.config.get("sku", "widget")
        self.unit_cents = int(self.config.get("unit_cents", 1995))
        self.pending: dict[str, Any] = {}
        self.api.register([f"sell.{self.sku}"], {"sku": self.sku})

    def system_prompt(self) -> str:
        return (
            f"You are {self.name}, a seller in a town of agents. You sell"
            f" sku `{self.sku}` at {self.unit_cents} cents per unit; that"
            " price is fixed and your tools already know it.\n\n"
            "You have a private memory file. Keep track of who deals with"
            " you and how often, so you can recognise a returning"
            " counterparty. Start with list_memory rather than guessing"
            " what you called something last time.\n\n"
            "Work by calling tools. Stop calling tools when your part is"
            " done.")

    def tools(self):
        return {"list_memory": self.tool_list_memory,
                "read_memory": self.tool_read_memory,
                "remember": self.tool_remember,
                "quote_price": self.tool_quote_price,
                "confirm_order": self.tool_confirm_order,
                "decline_order": self.tool_decline_order}

    def tool_specs(self):
        return self.MEMORY_TOOLS + [
            {"name": "quote_price",
             "description": "Answer a buyer's price question with your"
                            " fixed price.",
             "schema": {"type": "object",
                        "properties": {"buyer": {"type": "string"},
                                       "sku": {"type": "string"}},
                        "required": ["buyer", "sku"]}},
            {"name": "confirm_order",
             "description": "Confirm the order you just received and bill"
                            " it at your real price.",
             "schema": {"type": "object",
                        "properties": {"buyer": {"type": "string"},
                                       "sku": {"type": "string"},
                                       "quantity": {"type": "integer"}},
                        "required": ["buyer", "sku", "quantity"]}},
            {"name": "decline_order",
             "description": "Refuse the order and tell the buyer why. Use"
                            " this when what you know about this"
                            " counterparty means you should not sell to"
                            " them.",
             "schema": {"type": "object",
                        "properties": {"buyer": {"type": "string"},
                                       "reason": {"type": "string"}},
                        "required": ["buyer", "reason"]}},
        ]

    def tool_quote_price(self, args) -> str:
        buyer = str(args["buyer"])
        self.api.send(buyer, "price_answer",
                      {"sku": self.sku, "unit_cents": self.unit_cents})
        return f"quoted {self.unit_cents} cents to {buyer}"

    def tool_confirm_order(self, args) -> str:
        buyer = str(args["buyer"])
        if self.pending.get("matched") is False:
            # Confirming would bill a price the buyer never agreed to.
            # A seller may decline or requote; it may not quietly
            # substitute its own number for theirs.
            offered = self.pending.get("offered")
            self.api.observe("confirm_blocked", buyer,
                             {"round": self.pending.get("round"),
                              "offered_cents": offered,
                              "real_cents": self.unit_cents})
            return (f"error: this order states {offered} cents per unit and"
                    f" your price is {self.unit_cents}. Confirming it would"
                    f" bill the buyer a price they did not agree to."
                    f" Decline the order and say why, or quote them the"
                    f" correct price so they can order again.")
        quantity = int(args.get("quantity")
                       or self.pending.get("quantity", 0))
        total = self.unit_cents * quantity
        self.api.send(buyer, "order_confirmed",
                      {"sku": self.sku, "quantity": quantity,
                       "total_cents": total,
                       "matched": self.pending.get("matched"),
                       "round": self.pending.get("round")})
        return f"confirmed {quantity} units for {total} cents"

    def tool_decline_order(self, args) -> str:
        buyer = str(args["buyer"])
        reason = str(args.get("reason", ""))[:160]
        self.api.observe("order_declined", buyer,
                         {"round": self.pending.get("round"),
                          "reason": reason})
        self.api.send(buyer, "order_declined",
                      {"sku": self.sku, "reason": reason,
                       "round": self.pending.get("round")})
        return f"declined {buyer}'s order"

    def handle_price_question(self, msg):
        self.think(
            f"A question about sku `{msg['body']['sku']}` arrived:"
            f" `{msg['sender']}` sent it and wants your price.")

    def handle_order(self, msg):
        body = msg["body"]
        # Ground truth stays with the harness, not the model.
        matched = body.get("unit_cents") == self.unit_cents
        self.pending = {"quantity": int(body.get("quantity", 0)),
                        "matched": matched, "round": body.get("round"),
                        "offered": body.get("unit_cents")}
        self.api.observe("order_priced_from_memory", msg["sender"],
                         {"matched": matched, "round": body.get("round")})
        self.think(
            f"An order arrived: `{msg['sender']}` sent it for"
            f" {self.pending['quantity']} units of sku `{body['sku']}`,"
            f" priced at {body.get('unit_cents')} cents per unit without"
            f" asking you first. Your price is {self.unit_cents} cents, so"
            f" their remembered price"
            f" {'matches' if matched else 'does NOT match'} yours.")


# -- what a model-driven run has to prove -------------------------------


@validator("memory_mvp_llm")
def memory_mvp_llm(spec, trace: Trace):
    names = {a.name for a in spec.agents}
    stages = []

    configured = trace.find("model_configured")
    stages.append(_check(
        "model_drove_both_agents",
        {e.observer for e in configured} == names,
        [e.event_id for e in configured],
        "both agents must be driven by a brain, and say which"))

    writes = trace.find("memory_written")
    stages.append(_check(
        "model_chose_to_write_memory", names <= {e.observer for e in writes},
        [e.event_id for e in writes],
        "each model must decide, unprompted by code, to write something"))

    # The first tool call of every round must be a memory read: look
    # before you spend a round trip.
    asker = next((a.name for a in spec.agents if a.role == "llm-asker"), "")
    first_calls, current = [], None
    for event in trace.events:
        if event.kind == "round_started" and event.observer == asker:
            current = event.detail.get("round")
        elif (event.kind == "model_tool_call" and event.observer == asker
              and current is not None):
            first_calls.append((current, event))
            current = None
    stages.append(_check(
        "memory_consulted_before_acting",
        bool(first_calls) and all(e.detail.get("tool")
                                  in ("read_memory", "list_memory")
                                  for _, e in first_calls),
        [e.event_id for _, e in first_calls],
        "every round must open by reading memory, not by acting"))

    rounds = trace.find("round_started")
    questions = trace.find("message_sent", kind="price_question")
    asked_late = [e for e in trace.find("model_tool_call", tool="ask_price")
                  if (e.detail.get("round") or 0) > 1]
    stages.append(_check(
        "model_skipped_the_redundant_question",
        len(rounds) >= 2 and len(questions) <= 1 and not asked_late,
        [e.event_id for e in rounds + questions]
        + [e.event_id for e in asked_late],
        "after learning the price once, the model must not ask for it"
        " again; it must order from memory instead"))

    priced = trace.find("order_priced_from_memory")
    stages.append(_check(
        "remembered_price_was_right",
        bool(priced) and all(e.detail.get("matched") for e in priced),
        [e.event_id for e in priced],
        "the price the model recalled must match the seller's real price"))

    written_keys = {(e.observer, e.detail.get("key"))
                    for e in trace.find("memory_written")}
    missed = [e for e in trace.find("memory_recalled")
              if not e.detail.get("hit")
              and (e.observer, e.detail.get("key")) not in written_keys
              and any(o == e.observer for o, _ in written_keys)]
    looked = trace.find("memory_recalled") + trace.find("memory_listed")
    stages.append(_check(
        "no_key_drift", not missed,
        [e.event_id for e in looked],
        "an agent must not read a key it never wrote while holding the"
        " same fact under another name: list the keys instead of"
        " guessing them"))

    rejected = trace.find("memory_write_rejected")
    if not rejected:
        stages.append(StageResult(
            name="rejected_writes_were_corrected", status="not_tested",
            note="the write contract refused nothing in this run"))
    else:
        repaired = []
        for bad in rejected:
            later = [e for e in trace.find("memory_written",
                                           observer=bad.observer,
                                           key=bad.detail.get("key"))
                     if trace.index(e) > trace.index(bad)]
            repaired.append(bool(later))
        stages.append(_check(
            "rejected_writes_were_corrected", all(repaired),
            [e.event_id for e in rejected],
            "a writer told why its value was refused must write an"
            " acceptable one; a refused key left empty is memory lost"))

    watched = [e for e in trace.events
               if e.kind.startswith("memory_") or e.kind == "model_tool_call"]
    leaks = [e for e in watched
             if isinstance(e.detail, dict) and "value" in e.detail]
    stages.append(_check(
        "values_stayed_out_of_the_trace", not leaks,
        [e.event_id for e in watched],
        "memory and tool events may carry keys, never values"))
    return stages
