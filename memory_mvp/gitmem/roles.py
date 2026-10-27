"""Agents for the combined layer.

Two scripted roles for a run that needs no model, and two model-driven
roles that extend the existing LLM agents with one extra tool: an attempt
to read the counterparty's memory. That attempt is the point — whether it
succeeds is a policy decision, and either way the decision is recorded.

Self-reported writes can now be refused (a bad value, or a deployment
where the self lane is off entirely), so every `remember` here is written
to survive a refusal rather than crash the run.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from nandatown.sim.agents import SimAgent, role

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .contract import RejectedWrite  # noqa: E402


class GitMemAgent(SimAgent):
    """Shared helpers: tolerant writes and a counterparty read attempt."""

    @property
    def memory(self):
        return self.api._engine.layers["memory"]

    def keep(self, key: str, value: Any, subject: str = "") -> bool:
        """Write, and carry on if the write is refused."""
        try:
            self.memory.remember(self.name, key, value, subject=subject)
            return True
        except RejectedWrite:
            return False
        except Exception:  # a store problem is not the agent's to handle
            return False

    def peek(self, other: str):
        """Try to read another agent's memory. The policy decides."""
        try:
            return self.memory.read_as(self.name, other)
        except Exception:
            return None


@role("git-asker")
class GitAsker(GitMemAgent):
    """Buys widgets. Asks once, then orders from memory."""

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
        seller = self.memory.recall(self.name, "preferred_seller")
        price = self.memory.recall(self.name, self.price_key)
        if seller is None or price is None:
            cards = self.api.lookup(f"sell.{self.sku}")
            if not cards:
                self.api.observe("asker_gave_up", self.name,
                                 {"reason": "no seller in the registry"})
                return
            self.api.send(cards[0]["name"], "price_question",
                          {"sku": self.sku})
            return
        self.api.observe("question_skipped", self.name,
                         {"round": self.round, "key": self.price_key})
        self.api.send(seller, "order",
                      {"sku": self.sku, "quantity": self.quantity,
                       "unit_cents": int(price), "round": self.round})

    def handle_price_answer(self, msg):
        body = msg["body"]
        self.keep("preferred_seller", msg["sender"], subject=msg["sender"])
        self.keep(self.price_key, body["unit_cents"], subject=msg["sender"])
        if self.round < self.rounds:
            self.api.later(1.0, self.start_round)

    def handle_order_confirmed(self, msg):
        body = msg["body"]
        self.keep("last_order_total_cents", body["total_cents"],
                  subject=msg["sender"])
        self.api.observe("order_confirmed", self.name,
                         {"round": body.get("round"),
                          "matched": body.get("matched")})
        # What does my counterparty believe about me? Usually: not allowed.
        self.peek(msg["sender"])

    def handle_order_declined(self, msg):
        body = msg["body"]
        self.api.observe("order_refused", self.name,
                         {"round": body.get("round"),
                          "reason": str(body.get("reason", ""))[:160]})
        self.peek(msg["sender"])


@role("git-answerer")
class GitAnswerer(GitMemAgent):
    """Sells widgets at a fixed price and keeps a record of its buyers."""

    def on_start(self):
        self.answered = 0
        self.filled = 0
        self.sku = self.config.get("sku", "widget")
        self.unit_cents = int(self.config.get("unit_cents", 1995))
        self.api.register([f"sell.{self.sku}"], {"sku": self.sku})

    def handle_price_question(self, msg):
        self.answered += 1
        self.keep("last_question_from", msg["sender"], subject=msg["sender"])
        self.keep("questions_answered", self.answered)
        self.api.reply(msg, "price_answer",
                       {"sku": self.sku, "unit_cents": self.unit_cents})

    def handle_order(self, msg):
        body = msg["body"]
        matched = body.get("unit_cents") == self.unit_cents
        quantity = int(body.get("quantity", 0))
        self.api.observe("order_priced_from_memory", msg["sender"],
                         {"matched": matched, "round": body.get("round")})
        # Check what we know about this buyer before answering.
        self.peek(msg["sender"])
        if not matched:
            self.api.reply(msg, "order_declined", {
                "sku": self.sku, "round": body.get("round"),
                "reason": f"The price you listed ({body.get('unit_cents')}"
                          f" cents/unit) doesn't match my actual price of"
                          f" {self.unit_cents} cents/unit."})
            self.api.observe("order_declined", msg["sender"],
                             {"round": body.get("round"),
                              "reason": "price mismatch"})
            return
        self.filled += 1
        self.keep("last_buyer", msg["sender"], subject=msg["sender"])
        self.keep("orders_filled", self.filled)
        self.api.reply(msg, "order_confirmed",
                       {"sku": self.sku, "quantity": quantity,
                        "total_cents": self.unit_cents * quantity,
                        "matched": matched, "round": body.get("round")})


# -- model-driven versions ------------------------------------------------

def _install_llm_roles() -> bool:
    """Extend the existing LLM agents with a counterparty-read tool.

    Imported lazily so a scripted run never needs the LLM module.
    """
    try:
        import llm_agents
    except ImportError:
        return False

    class PeekMixin:
        def tool_read_counterparty(self, args) -> str:
            other = str(args.get("agent", ""))
            memory = self.api._engine.layers["memory"]
            reader = getattr(memory, "read_as", None)
            if reader is None:
                return "error: this town's memory layer has no cross-agent read"
            visible = reader(self.name, other)
            if visible is None:
                return ("refused: you are not permitted to read"
                        f" {other}'s memory in this deployment")
            if not visible:
                return f"{other}'s memory holds nothing you may see"
            return "\n".join(f"{k} = {json.dumps(v)}"
                             for k, v in sorted(visible.items()))

        PEEK_SPEC = {
            "name": "read_counterparty_memory",
            "description": "Try to read what another agent has recorded,"
                           " including what it has recorded about you. May"
                           " be refused.",
            "schema": {"type": "object",
                       "properties": {"agent": {"type": "string"}},
                       "required": ["agent"]},
        }

    @role("git-llm-asker")
    class GitLlmAsker(PeekMixin, llm_agents.LlmAsker):
        def tools(self):
            table = super().tools()
            table["read_counterparty_memory"] = self.tool_read_counterparty
            return table

        def tool_specs(self):
            return super().tool_specs() + [self.PEEK_SPEC]

    @role("git-llm-answerer")
    class GitLlmAnswerer(PeekMixin, llm_agents.LlmAnswerer):
        def tools(self):
            table = super().tools()
            table["read_counterparty_memory"] = self.tool_read_counterparty
            return table

        def tool_specs(self):
            return super().tool_specs() + [self.PEEK_SPEC]

    return True


INSTALLED_LLM_ROLES = _install_llm_roles()
