"""Changing an agent's memory from outside the agent, in the open.

There are two ways a person can change what an agent remembers:

  * a correction goes through the store: it passes the write contract,
    names the person (`human:<name>`), says why, and is committed. The
    agent reads it as its own memory, and the history says who changed it
    and why.

  * an outside edit goes around the store: someone opens the fact file in
    an editor and saves it. Git sees a file that no longer matches its last
    commit, and nobody is on record for the change. Depending on the
    policy's `outside_edits` toggle, a run either reports it and lets the
    agent read the edited file ("report"), or reports it and puts the file
    back ("restore").

Same value, same file, different paths: one is accountable and one is not.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .contract import HUMAN_PREFIX, Contract, RejectedWrite
from .policy import Policy
from .store import Store


@dataclass
class Outcome:
    accepted: bool
    reason: str = ""
    rule: str = ""
    commit: str | None = None
    fact: str | None = None
    replaced: str | None = None
    old_value: Any = None
    details: dict = field(default_factory=dict)


def outside_edits(store: Store) -> list[dict]:
    """Fact files that differ from the last commit and that the store did
    not write.

    The store stages every file it writes before committing, so a change
    that is staged and otherwise clean is the store's own (interrupted)
    write. Only the working-tree column of `git status` is somebody else.
    """
    out = store._git("status", "--porcelain", "--untracked-files=all",
                     "--", "facts")
    found = []
    for line in out.splitlines():
        if len(line) < 4 or line[1] == " ":
            continue
        path = line[3:].strip()
        change = {"M": "modified", "D": "deleted", "?": "added"}.get(
            line[1], "changed")
        found.append({"path": path, "change": change})
    return found


def restore(store: Store, edits: list[dict]) -> list[dict]:
    """Put outside edits back to the last commit.

    A modified or deleted file is checked out again. A file someone added
    by hand has no committed version, so it is moved to `quarantine/`
    rather than deleted: nothing a person wrote is thrown away, it just
    stops being memory.
    """
    done = []
    tracked = [e["path"] for e in edits if e["change"] != "added"]
    if tracked:
        store._git("checkout", "HEAD", "--", *tracked)
        done += [dict(e, action="restored") for e in edits
                 if e["change"] != "added"]
    for edit in edits:
        if edit["change"] != "added":
            continue
        source = store.root / edit["path"]
        target = store.root / "quarantine" / edit["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        done.append(dict(edit, action="quarantined",
                         moved_to=str(target.relative_to(store.root))))
    return done


def correct(store: Store, holder: str, key: str, value: Any, reason: str,
            by: str = "operator", policy: Policy | None = None,
            contract: Contract | None = None) -> Outcome:
    """Correct one fact in `holder`'s own memory, on the record.

    Refused, with the reason, when the policy does not allow corrections,
    when the store holds unexplained outside edits (correcting on top of
    one would commit the edited file and hide it), when the holder is
    unknown, or when the value breaks the write contract.
    """
    policy = policy or Policy.load()
    contract = contract or Contract()

    allowed = policy.may_correct()
    if not allowed.allowed:
        return Outcome(False, allowed.reason, allowed.rule)

    pending = outside_edits(store)
    if pending:
        return Outcome(
            False,
            f"the store has {len(pending)} fact file(s) edited outside it"
            " (" + ", ".join(f"{e['change']} {e['path']}" for e in pending)
            + "); resolve those first, or this correction would commit them"
            " as if they were on the record",
            "outside_edits_pending", details={"edits": pending})

    holders = {f.holder for f in store.all()}
    if holder not in holders:
        known = ", ".join(sorted(holders)) or "none yet"
        return Outcome(False, f"{holder!r} has no memory in this store"
                              f" (holders: {known})", "unknown_holder")

    asserter = f"{HUMAN_PREFIX}{by}"
    before = store.current(holder, key, "self")
    op = {"op": "UPDATE" if before else "ADD", "lane": "self",
          "holder": holder, "asserter": asserter, "key": key,
          "value": value, "subject": holder, "scope": "private",
          "reason": reason, "evidence": f"correction: {reason}"}
    try:
        checked = contract.check(op, holders)
    except RejectedWrite as why:
        return Outcome(False, str(why), "write_contract",
                       details={"rule_for_key": contract.rule_for(key)})

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    source = f"correction by {asserter} at {stamp}"
    changes = store.apply([checked], source=source, when="between-runs")
    if changes and changes[0].op == "NOOP":
        return Outcome(False, f"{key!r} already holds that value; nothing"
                              " to correct", "unchanged",
                       fact=changes[0].fact.id)
    commit = store.commit(changes, source=source,
                          title=f"correction by {asserter}: {holder} {key}",
                          when="0")
    change = changes[0]
    return Outcome(True, reason, "human_corrections", commit=commit,
                   fact=change.fact.id,
                   replaced=change.old.id if change.old else None,
                   old_value=change.old.value if change.old else None)


def corrections_in_effect(store: Store) -> list:
    """Active self facts last set by a person rather than the agent."""
    return [f for f in store.active(lane="self")
            if f.asserter.startswith(HUMAN_PREFIX)]

