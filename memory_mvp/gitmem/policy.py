"""Capability 4 — who can read what, as toggles.

Every read of a fact is a decision, and every decision is recorded. The
toggles exist because the right answer changes with the deployment:

  open_lab          everything readable; useful while debugging a scenario
  default           agents keep their own memory private; the harness and
                    the observer may read it; a subject cannot see claims
                    made about it by someone else
  cross_enterprise  we do not get to say how the other side records its
                    own memory, and we do not get to read it. Only the
                    observed lane is captured, from traces we can see.

A preset is a starting point: any individual toggle can be overridden in
the policy file or by the scenario.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_POLICY = os.path.join(os.path.dirname(HERE), "memory_policy.json")

PRESETS: dict[str, dict] = {
    "open_lab": {
        "capture_lanes": ["self", "observed"],
        "agents_read_each_other": True,
        "subject_can_read_claims_about_self": True,
        "harness_can_read_agent_memory": True,
        "observer_can_read_agent_memory": True,
    },
    "default": {
        "capture_lanes": ["self", "observed"],
        "agents_read_each_other": False,
        "subject_can_read_claims_about_self": False,
        "harness_can_read_agent_memory": True,
        "observer_can_read_agent_memory": True,
    },
    "cross_enterprise": {
        # The counterparty runs its own agent its own way. We cannot
        # define how it records memory, so self-reporting is not available
        # and nobody on our side may read inside it. Observation of the
        # traffic we can legitimately see is the only capture lane left.
        "capture_lanes": ["observed"],
        "agents_read_each_other": False,
        "subject_can_read_claims_about_self": False,
        "harness_can_read_agent_memory": False,
        "observer_can_read_agent_memory": False,
    },
}


@dataclass
class Decision:
    allowed: bool
    rule: str
    reason: str = ""


@dataclass
class Policy:
    preset: str = "default"
    capture_lanes: list = field(default_factory=lambda: ["self", "observed"])
    agents_read_each_other: bool = False
    subject_can_read_claims_about_self: bool = False
    harness_can_read_agent_memory: bool = True
    observer_can_read_agent_memory: bool = True

    # -- loading ------------------------------------------------------------

    @classmethod
    def load(cls, path: str | None = None, preset: str | None = None
             ) -> "Policy":
        """Three layers, innermost wins:

            1. the preset named by the argument, else TOWN_MEMORY_PRESET,
               else the policy file's `preset`, else "default"
            2. the file's `overrides` block, applied on top of it
            3. nothing else — a toggle not named in `overrides` always
               comes from the preset, so switching preset actually
               switches behaviour
        """
        path = path or os.environ.get("TOWN_MEMORY_POLICY") or DEFAULT_POLICY
        spec: dict = {}
        try:
            with open(path) as handle:
                spec = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError):
            spec = {}
        name = (preset or os.environ.get("TOWN_MEMORY_PRESET")
                or spec.get("preset") or "default")
        settings = dict(PRESETS.get(name, PRESETS["default"]))
        overrides = spec.get("overrides") or {}
        for key, value in overrides.items():
            if key in settings:
                settings[key] = value
        settings["preset"] = name
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in settings.items() if k in known})

    def as_dict(self) -> dict:
        return asdict(self)

    # -- decisions -----------------------------------------------------------

    def may_capture(self, lane: str) -> Decision:
        if lane in self.capture_lanes:
            return Decision(True, "capture_lanes")
        return Decision(
            False, "capture_lanes",
            f"this deployment does not capture the {lane!r} lane"
            + (": we do not get to define how these agents record their own"
               " memory, so only observed facts are accepted"
               if lane == "self" else ""))

    def may_read(self, reader: str, fact) -> Decision:
        """`reader` is an agent name, "harness" or "observer"."""
        holder = getattr(fact, "holder", "")
        subject = getattr(fact, "subject", "") or ""
        scope = getattr(fact, "scope", "private")

        if reader == holder:
            return Decision(True, "own_memory")

        if reader == "harness":
            if self.harness_can_read_agent_memory:
                return Decision(True, "harness_can_read_agent_memory")
            return Decision(False, "harness_can_read_agent_memory",
                            "the harness may not read inside agent memory in"
                            " this deployment")

        if reader == "observer":
            if self.observer_can_read_agent_memory:
                return Decision(True, "observer_can_read_agent_memory")
            return Decision(False, "observer_can_read_agent_memory",
                            "the observer works from traces only and may not"
                            " read inside agent memory")

        if scope == "shared":
            return Decision(True, "scope_shared")

        if subject == reader and scope != "private":
            if self.subject_can_read_claims_about_self:
                return Decision(True, "subject_can_read_claims_about_self")
            return Decision(
                False, "subject_can_read_claims_about_self",
                f"{reader!r} is the subject of this claim but may not read"
                f" what {holder!r} records about it")

        if self.agents_read_each_other:
            return Decision(True, "agents_read_each_other")
        return Decision(False, "agents_read_each_other",
                        f"{reader!r} may not read {holder!r}'s memory")
