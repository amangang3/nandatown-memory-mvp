"""gitmem: a governed, versioned memory layer for multi-agent NANDA Town runs.

Four capabilities, one store:

  1. Capture    two lanes. The agent writes what it chose to remember
                (`self`), and an external observation model reads the raw
                event trace and decides independently what changed
                (`observed`). The two are then compared.
  2. Writing    one contract in front of both lanes: typed keys, limits,
                evidence required of the observer, refusal with a reason.
  3. Versions   every accepted change is a git commit carrying who asserted
                it, when, from which event, and what it replaced.
  4. Access     every read is a policy decision, recorded. Toggles cover
                agent-to-agent reads, harness reads, observer reads, and
                whether a subject may see claims made about it.

The store is adapted from git-for-memory (akg745), which supplies the fact
model, supersede semantics and git commit discipline. The write contract,
access policy, two-lane capture and reconciliation are from the NANDA Town
memory MVP.
"""

from .contract import Contract, RejectedWrite          # noqa: F401
from .policy import Policy, Decision                   # noqa: F401
from .store import Fact, Change, Store                 # noqa: F401

__all__ = ["Contract", "RejectedWrite", "Policy", "Decision",
           "Fact", "Change", "Store"]
