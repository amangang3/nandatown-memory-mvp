"""Offline checks for the two real-model brains.

No account and no network: a fake transport asserts the request each
brain builds and hands back a provider-shaped tool call, so the wire
formats are exercised even when the run itself uses the mock brain.

    ../.venv/bin/python -m pytest memory_mvp/test_brains.py -q
"""

from __future__ import annotations

import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_brains import AnthropicBrain, OpenAICompatibleBrain  # noqa: E402

SYSTEM = "You are a purchasing agent."
TOOLS = [{"name": "read_memory", "description": "look it up",
          "schema": {"type": "object",
                     "properties": {"key": {"type": "string"}},
                     "required": ["key"]}}]
CONVERSATION = [
    {"type": "user", "text": "Round 2 begins."},
    {"type": "tool_calls", "text": "checking",
     "calls": [{"id": "c1", "name": "read_memory",
                "input": {"key": "widget_unit_cents"}}]},
    {"type": "tool_results",
     "results": [{"id": "c1", "name": "read_memory", "output": "1995"}]},
]


def test_anthropic_request_and_parse():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [
            {"type": "text", "text": "ordering from memory"},
            {"type": "tool_use", "id": "t1", "name": "place_order",
             "input": {"unit_cents": 1995}}]})

    brain = AnthropicBrain(
        "claude-sonnet-5", api_key="test-key",
        http=httpx.Client(transport=httpx.MockTransport(handler)))
    reply = brain.act(SYSTEM, CONVERSATION, TOOLS)

    body = seen["body"]
    assert body["system"] == SYSTEM
    assert body["tools"][0]["input_schema"]["required"] == ["key"]
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert body["messages"][1]["content"][-1]["type"] == "tool_use"
    assert body["messages"][2]["content"][0]["tool_use_id"] == "c1"
    assert reply["text"] == "ordering from memory"
    assert reply["tool_calls"] == [
        {"id": "t1", "name": "place_order", "input": {"unit_cents": 1995}}]


def test_openai_compatible_request_and_parse():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "t2", "type": "function", "function": {
                "name": "place_order",
                "arguments": '{"unit_cents": 1995}'}}]}}]})

    brain = OpenAICompatibleBrain(
        "llama3.1", base_url="http://model.invalid/v1",
        http=httpx.Client(base_url="http://model.invalid/v1",
                          transport=httpx.MockTransport(handler)))
    reply = brain.act(SYSTEM, CONVERSATION, TOOLS)

    body = seen["body"]
    assert body["messages"][0] == {"role": "system", "content": SYSTEM}
    assert body["tools"][0]["function"]["parameters"]["required"] == ["key"]
    assert body["messages"][2]["tool_calls"][0]["function"]["name"] \
        == "read_memory"
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c1",
                                   "content": "1995"}
    assert reply["tool_calls"] == [
        {"id": "t2", "name": "place_order", "input": {"unit_cents": 1995}}]
