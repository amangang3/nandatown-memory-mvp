"""JSON-returning model backends for the observation lane.

Three, chosen with TOWN_OBSERVER_MODEL:

  rules:v1          a deterministic reader of the trace. No model, no
                    account, no cost — the default, so a run is always
                    reproducible. It is a stand-in, not a claim that a
                    model would behave this way.
  claude-cli:MODEL  the locally installed Claude Code CLI in headless
                    mode, using your existing login rather than an API
                    key. Adapted from git-for-memory's gfm/llm.py.
  claude-<name>     the Anthropic Messages API, needs ANTHROPIC_API_KEY.

A backend returns a parsed JSON object or raises. Nothing here knows what
a memory operation is; see observer.py for that.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class ObserverError(RuntimeError):
    pass


def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class ClaudeCliBrain:
    """Headless `claude -p`, so the observer costs plan usage, not credits."""

    def __init__(self, model: str = "claude-haiku-4-5-20251001"):
        self.name = f"claude-cli:{model}"
        self.model = model
        self.binary = (shutil.which("claude")
                       or str(Path.home() / ".local" / "bin" / "claude"))

    @staticmethod
    def _clean_env() -> dict:
        # A CLI launched from inside another Claude Code session inherits
        # that session's auth variables, which the child cannot use.
        keep = ("HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG")
        env = {k: os.environ[k] for k in keep if k in os.environ}
        env["GFM_INTERNAL"] = "1"
        return env

    def complete(self, system: str, user: str, schema: dict,
                 timeout: int = 180) -> dict:
        system = (system + "\n\nReply with only a JSON object matching this"
                  " JSON Schema, and no other text:\n"
                  + json.dumps(schema, separators=(",", ":")))
        cmd = [
            self.binary, "-p",
            "--model", self.model,
            "--tools", "",
            "--setting-sources", "",
            "--no-session-persistence",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disable-slash-commands",
            "--system-prompt", system,
            "--output-format", "json",
        ]
        last = ""
        for _ in range(2):
            proc = subprocess.run(cmd, input=user, capture_output=True,
                                  text=True, timeout=timeout,
                                  env=self._clean_env())
            try:
                envelope = json.loads(proc.stdout)
            except json.JSONDecodeError:
                last = (proc.stdout or proc.stderr)[:400]
                continue
            if envelope.get("is_error"):
                last = str(envelope.get("result", ""))[:400]
                continue
            parsed = _extract_json(envelope.get("result", ""))
            if parsed is None:
                last = "reply was not JSON: " + str(
                    envelope.get("result", ""))[:300]
                continue
            return parsed
        raise ObserverError(f"{self.name}: {last}")


class AnthropicBrain:
    """The Messages API, for when there is a key and no CLI."""

    def __init__(self, model: str, api_key: str | None = None, http=None):
        import httpx

        self.name = model
        self.model = model
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ObserverError(
                f"TOWN_OBSERVER_MODEL={model} needs ANTHROPIC_API_KEY")
        self.http = http or httpx.Client(
            headers={"x-api-key": key,
                     "anthropic-version": ANTHROPIC_VERSION,
                     "content-type": "application/json"},
            timeout=120.0)

    def complete(self, system: str, user: str, schema: dict) -> dict:
        system = (system + "\n\nReply with only a JSON object matching this"
                  " JSON Schema, and no other text:\n"
                  + json.dumps(schema, separators=(",", ":")))
        response = self.http.post(ANTHROPIC_URL, json={
            "model": self.model,
            "max_tokens": 2048,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        })
        if response.status_code >= 400:
            raise ObserverError(f"anthropic/{self.model} returned HTTP "
                                f"{response.status_code}: {response.text[:400]}")
        blocks = response.json().get("content", [])
        text = " ".join(b.get("text", "") for b in blocks
                        if b.get("type") == "text")
        parsed = _extract_json(text)
        if parsed is None:
            raise ObserverError(f"anthropic/{self.model}: reply was not JSON")
        return parsed


def make_brain(model: str | None = None):
    """None means the deterministic reader; see observer.RuleReader."""
    model = (model or os.environ.get("TOWN_OBSERVER_MODEL")
             or "rules:v1").strip()
    if model.startswith("rules"):
        return None
    if model.startswith("claude-cli"):
        _, _, name = model.partition(":")
        return ClaudeCliBrain(name or "claude-haiku-4-5-20251001")
    if model.startswith("claude"):
        return AnthropicBrain(model)
    raise ObserverError(
        f"unknown observer model {model!r}; use rules:v1, claude-cli:<model>"
        f" or an Anthropic model name")
