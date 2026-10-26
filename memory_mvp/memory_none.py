"""none.v1: the counterfactual. An agent with no memory at all.

Same interface as any memory plugin, but nothing is kept: every write
is discarded and every read misses. Swap it in to see what the rest of
the town does when agents cannot remember each other.

    nandatown run memory_mvp/memory_mvp.yaml \
        --plugin memory_mvp/memory_none.py --layer memory=none.v1
"""

from __future__ import annotations

from typing import Any

from nandatown.layers import register


@register("memory", "none.v1")
class NoMemory:
    """none.v1: writes go nowhere, reads always miss."""

    def __init__(self, engine):
        self.engine = engine

    def remember(self, agent: str, key: str, value: Any) -> None:
        self.engine.emit(agent, "memory_discarded", agent, {"key": key})

    def recall(self, agent: str, key: str) -> Any:
        self.engine.emit(agent, "memory_recalled", agent,
                         {"key": key, "hit": False, "source": "none"})
        return None

    def note(self, agent: str, text: str) -> None:
        pass
