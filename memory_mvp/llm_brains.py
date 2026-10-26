"""Three interchangeable brains for the model-driven agents.

A brain turns a conversation plus a tool list into either text or tool
calls. The conversation is kept in a provider-neutral form and each
brain converts it to its own wire format, so the agent code never
contains a vendor's message shape.

  TOWN_LLM_MODEL=mock:v1          deterministic stand-in, no network
  TOWN_LLM_MODEL=claude-sonnet-5  Anthropic Messages API (ANTHROPIC_API_KEY)
  TOWN_LLM_MODEL=llama3.1         any OpenAI-compatible /chat/completions
                                  endpoint (TOWN_MODEL_URL, default Ollama)
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OLLAMA_URL = "http://localhost:11434/v1"


def _raise_for_status(response, who: str) -> None:
    """Surface the provider's own explanation, not just the code."""
    if response.status_code < 400:
        return
    raise RuntimeError(f"{who} returned HTTP {response.status_code}:"
                       f" {response.text[:600]}")


class Brain:
    """Returns {"text": str, "tool_calls": [{"id", "name", "input"}]}."""

    name = "brain"

    def act(self, system: str, conversation: list[dict[str, Any]],
            tools: list[dict[str, Any]]) -> dict[str, Any]:
        raise NotImplementedError


# -- the stand-in -------------------------------------------------------


class MockBrain(Brain):
    """A deterministic agent-shaped policy: no inference, no account.

    It reads the same situation text a model would and follows the
    obvious strategy — consult memory first, ask only when memory is
    empty, write down whatever it learns. Enough to exercise the whole
    tool loop in CI; swap in a real model to find out whether one
    actually behaves this way.
    """

    name = "mock:v1"

    def __init__(self, agent_role: str):
        self.agent_role = agent_role

    def act(self, system, conversation, tools):
        situation = next((m["text"] for m in conversation
                          if m["type"] == "user"), "")
        said = situation.lower()
        results = [r for m in conversation if m["type"] == "tool_results"
                   for r in m["results"]]
        seen = {r["name"] for r in results}
        last = {r["name"]: r["output"] for r in results}

        def call(name, **args):
            return {"text": "", "tool_calls": [
                {"id": f"mock-{len(results) + 1}", "name": name,
                 "input": args}]}

        def done(text):
            return {"text": text, "tool_calls": []}

        seller = _first(r"seller called `([^`]+)`", situation)
        buyer = _first(r"`([^`]+)` sent", situation)
        sku = _first(r"sku `([^`]+)`", situation) or "widget"
        quantity = int(_first(r"(\d+) units", situation) or 2)
        quoted = _first(r"(\d+) cents", situation)

        if self.agent_role == "asker":
            if "read_memory" not in seen:
                return call("read_memory", key=f"{sku}_unit_cents")
            remembered = _first(r"^(\d+)$", last.get("read_memory", ""))
            if "a price answer arrived" in said:
                if "remember" not in seen:
                    return call("remember", key=f"{sku}_unit_cents",
                                value=quoted)
                if sum(r["name"] == "remember" for r in results) == 1:
                    return call("remember", key="preferred_seller",
                                value=seller)
                return done("Noted the price and who quoted it.")
            if remembered is None:
                if "ask_price" not in seen:
                    return call("ask_price", seller=seller, sku=sku)
                return done("Asked; waiting for the answer.")
            if "place_order" not in seen:
                return call("place_order", seller=seller, sku=sku,
                            quantity=quantity, unit_cents=int(remembered))
            return done("Ordered at the remembered price, no question asked.")

        if "a question about" in said:
            if "remember" not in seen:
                return call("remember", key="last_question_from", value=buyer)
            if "quote_price" not in seen:
                return call("quote_price", buyer=buyer, sku=sku)
            return done("Quoted my price.")
        if "an order arrived" in said:
            if "remember" not in seen:
                return call("remember", key="last_buyer", value=buyer)
            if "confirm_order" not in seen:
                return call("confirm_order", buyer=buyer, sku=sku,
                            quantity=quantity)
            return done("Order confirmed.")
        return done("Nothing to do.")


def _first(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.M)
    return match.group(1) if match else None


# -- real models --------------------------------------------------------


class AnthropicBrain(Brain):
    """Claude via the Messages API."""

    def __init__(self, model: str, api_key: str | None = None,
                 http: Any = None):
        import httpx

        self.name = model
        self.model = model
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                f"TOWN_LLM_MODEL={model} needs ANTHROPIC_API_KEY in the"
                " environment")
        self.http = http or httpx.Client(
            headers={"x-api-key": key,
                     "anthropic-version": ANTHROPIC_VERSION,
                     "content-type": "application/json"},
            timeout=120.0)

    def act(self, system, conversation, tools):
        body = {
            "model": self.model,
            "max_tokens": 1024,
            "system": system,
            "tools": [{"name": t["name"], "description": t["description"],
                       "input_schema": t["schema"]} for t in tools],
            "messages": self._messages(conversation),
        }
        response = self.http.post(ANTHROPIC_URL, json=body)
        _raise_for_status(response, f"anthropic/{self.model}")
        blocks = response.json().get("content", [])
        return {
            "text": " ".join(b.get("text", "") for b in blocks
                             if b.get("type") == "text").strip(),
            "tool_calls": [{"id": b["id"], "name": b["name"],
                            "input": b.get("input", {})}
                           for b in blocks if b.get("type") == "tool_use"],
        }

    @staticmethod
    def _messages(conversation):
        out = []
        for turn in conversation:
            if turn["type"] == "user":
                out.append({"role": "user", "content": turn["text"]})
            elif turn["type"] == "tool_calls":
                content = []
                if turn.get("text"):
                    content.append({"type": "text", "text": turn["text"]})
                content += [{"type": "tool_use", "id": c["id"],
                             "name": c["name"], "input": c["input"]}
                            for c in turn["calls"]]
                out.append({"role": "assistant", "content": content})
            elif turn["type"] == "tool_results":
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["id"],
                     "content": r["output"]} for r in turn["results"]]})
        return out


class OpenAICompatibleBrain(Brain):
    """Any /chat/completions endpoint with tool calling: Ollama, vLLM,
    LM Studio, a gateway."""

    def __init__(self, model: str, base_url: str | None = None,
                 http: Any = None):
        import httpx

        self.name = model
        self.model = model
        self.base_url = (base_url or os.environ.get("TOWN_MODEL_URL")
                         or OLLAMA_URL)
        headers = {}
        key = os.environ.get("TOWN_MODEL_KEY")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        self.http = http or httpx.Client(base_url=self.base_url,
                                         headers=headers, timeout=120.0)

    def act(self, system, conversation, tools):
        body = {
            "model": self.model,
            "messages": ([{"role": "system", "content": system}]
                         + self._messages(conversation)),
            "tools": [{"type": "function",
                       "function": {"name": t["name"],
                                    "description": t["description"],
                                    "parameters": t["schema"]}}
                      for t in tools],
            "tool_choice": "auto",
        }
        response = self.http.post("/chat/completions", json=body)
        _raise_for_status(response, f"{self.base_url} ({self.model})")
        message = response.json()["choices"][0]["message"]
        return {
            "text": message.get("content") or "",
            "tool_calls": [{"id": c["id"], "name": c["function"]["name"],
                            "input": _loads(c["function"]["arguments"])}
                           for c in (message.get("tool_calls") or [])],
        }

    @staticmethod
    def _messages(conversation):
        out = []
        for turn in conversation:
            if turn["type"] == "user":
                out.append({"role": "user", "content": turn["text"]})
            elif turn["type"] == "tool_calls":
                out.append({
                    "role": "assistant",
                    "content": turn.get("text") or None,
                    "tool_calls": [
                        {"id": c["id"], "type": "function",
                         "function": {"name": c["name"],
                                      "arguments": json.dumps(c["input"])}}
                        for c in turn["calls"]]})
            elif turn["type"] == "tool_results":
                out += [{"role": "tool", "tool_call_id": r["id"],
                         "content": r["output"]} for r in turn["results"]]
        return out


def _loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def make_brain(agent_role: str) -> Brain:
    """Pick a brain from TOWN_LLM_MODEL. Mock by default: a run that
    needs no account is a run anyone can reproduce."""
    model = os.environ.get("TOWN_LLM_MODEL", "mock:v1").strip()
    if model.startswith("mock"):
        return MockBrain(agent_role)
    if model.startswith("claude") and not os.environ.get("TOWN_MODEL_URL"):
        return AnthropicBrain(model)
    return OpenAICompatibleBrain(model)
