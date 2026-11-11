"""Corrections: a person changing an agent's memory on the record, and the
agent acting on it. Contrasted with the same change made around the store.

    cd memory_mvp && python3 -m pytest -q test_corrections.py

The unit tests need only git. The end-to-end test drives real NANDA Town
runs (`nandatown` must be installed) and is skipped without it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from gitmem.contract import Contract, RejectedWrite  # noqa: E402
from gitmem.correct import (  # noqa: E402
    correct, corrections_in_effect, outside_edits, restore,
)
from gitmem.policy import Policy  # noqa: E402
from gitmem.store import Store  # noqa: E402

KEY = "widget_unit_cents"


# -- a store with one remembered price ---------------------------------------

@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "memory")
    s.init()
    op = Contract().check({"op": "ADD", "lane": "self", "holder": "agent-a",
                           "asserter": "agent-a", "key": KEY,
                           "value": 1995}, {"agent-a", "agent-b"})
    s.commit(s.apply([op], source="run-1", when="1.00"), source="run-1",
             title="agent-a remembers price", when="1.00")
    return s


def _file(store: Store, key: str = KEY) -> Path:
    fact = store.current("agent-a", key, "self")
    return store.root / "facts" / "agent-a" / "self" / f"{fact.id}.md"


# -- corrections ---------------------------------------------------------------

def test_correction_is_committed_with_person_and_reason(store):
    out = correct(store, "agent-a", KEY, "2195",
                  reason="seller raised its price", by="alex")
    assert out.accepted, out.reason
    fact = store.current("agent-a", KEY, "self")
    assert fact.value == 2195                      # coerced by the contract
    assert fact.asserter == "human:alex"
    assert "seller raised its price" in fact.evidence
    assert fact.supersedes == out.replaced and out.old_value == 1995
    last = store._git("log", "-1", "--format=%B")
    assert "correction by human:alex" in last
    assert "why: seller raised its price" in last
    assert outside_edits(store) == []
    assert [f.id for f in corrections_in_effect(store)] == [fact.id]


def test_correction_needs_a_reason(store):
    out = correct(store, "agent-a", KEY, "2195", reason="  ")
    assert not out.accepted and out.rule == "write_contract"
    assert "say why" in out.reason
    assert store.current("agent-a", KEY, "self").value == 1995


def test_correction_must_meet_the_write_contract(store):
    out = correct(store, "agent-a", KEY, "cheap", reason="looks wrong")
    assert not out.accepted and out.rule == "write_contract"
    assert "integers" in out.reason


def test_correction_refused_where_policy_forbids_it(store):
    out = correct(store, "agent-a", KEY, "2195", reason="r",
                  policy=Policy.load(preset="cross_enterprise"))
    assert not out.accepted and out.rule == "human_corrections"


def test_correction_refused_for_unknown_holder(store):
    out = correct(store, "agent-z", KEY, "2195", reason="r")
    assert not out.accepted and out.rule == "unknown_holder"


def test_correction_refused_while_an_outside_edit_is_pending(store):
    path = _file(store)
    path.write_text(path.read_text().replace("\n1995\n", "\n1000\n"))
    out = correct(store, "agent-a", KEY, "2195", reason="r")
    assert not out.accepted and out.rule == "outside_edits_pending"
    # Nothing was committed over the edit.
    assert "correction" not in store._git("log", "-1", "--format=%s")


def test_unchanged_value_is_not_a_correction(store):
    out = correct(store, "agent-a", KEY, "1995", reason="r")
    assert not out.accepted and out.rule == "unchanged"


def test_an_agent_still_cannot_write_into_another_agents_memory():
    with pytest.raises(RejectedWrite):
        Contract().check({"lane": "self", "holder": "agent-a",
                          "asserter": "agent-b", "key": KEY, "value": 1,
                          "reason": "I say so"}, {"agent-a", "agent-b"})


# -- outside edits ---------------------------------------------------------------

def test_restore_puts_back_modified_and_deleted_and_quarantines_added(store):
    assert correct(store, "agent-a", "orders_filled", "3",
                   reason="seed").accepted
    price, buyer = _file(store), _file(store, "orders_filled")
    price.write_text(price.read_text().replace("\n1995\n", "\n1\n"))
    buyer.unlink()
    extra = price.parent / "f-9999.md"
    extra.write_text("---\nid: \"f-9999\"\n---\n\"planted\"\n")

    edits = {e["path"]: e["change"] for e in outside_edits(store)}
    assert sorted(edits.values()) == ["added", "deleted", "modified"]

    done = {d["path"]: d["action"] for d in restore(store, list(
        {"path": p, "change": c} for p, c in edits.items()))}
    assert sorted(done.values()) == ["quarantined", "restored", "restored"]
    assert outside_edits(store) == []
    assert store.current("agent-a", KEY, "self").value == 1995
    assert buyer.exists() and not extra.exists()
    assert (store.root / "quarantine" / "facts" / "agent-a" / "self"
            / "f-9999.md").exists()


def test_a_staged_store_write_is_not_an_outside_edit(store):
    # What a write cut off between `git add` and `git commit` leaves.
    path = _file(store)
    path.write_text(path.read_text().replace("\n1995\n", "\n2000\n"))
    store._git("add", "--", str(path.relative_to(store.root)))
    assert outside_edits(store) == []


# -- end to end, through NANDA Town ------------------------------------------------

def _nandatown() -> list[str] | None:
    beside = Path(sys.executable).parent / "nandatown"
    if beside.exists():
        return [str(beside)]
    found = shutil.which("nandatown")
    return [found] if found else None


TOWN = _nandatown()


def _run(scenario: str, env: dict, out: Path) -> dict:
    proc = subprocess.run(
        [*TOWN, "run", str(HERE / scenario), "--out", str(out)],
        env=env, capture_output=True, text=True, cwd=HERE.parent)
    match = re.search(r"Evidence bundle: (.+)", proc.stdout)
    assert match, proc.stdout[-1500:] + proc.stderr[-1500:]
    bundle = Path(match.group(1).strip())
    events = [json.loads(line) for line in
              (bundle / "events.jsonl").read_text().splitlines()]
    result = json.loads((bundle / "result.json").read_text())
    stages = {s["name"]: s["status"] for s in result["stages"]}
    kinds = [e["kind"] for e in events]
    recalled = {e["detail"]["key"]: e["detail"] for e in events
                if e["kind"] == "memory_recalled"
                and e["detail"].get("hit")}
    return {"verdict": result["verdict"], "stages": stages,
            "confirmed": kinds.count("order_confirmed"),
            "refused": kinds.count("order_refused"),
            "recalled": recalled, "kinds": kinds, "bundle": bundle}


@pytest.mark.skipif(TOWN is None, reason="nandatown is not installed")
def test_a_correction_changes_what_the_agent_does(tmp_path):
    store_dir = tmp_path / "memory-git"
    out = tmp_path / "runs"
    env = dict(os.environ, TOWN_GITMEM_DIR=str(store_dir),
               NANDATOWN_HOME=str(tmp_path / "home"))
    env.pop("TOWN_MEMORY_PRESET", None)
    env.pop("TOWN_MEMORY_POLICY", None)
    store = Store(store_dir)

    # 1. The buyer learns the price (1995) and orders at it.
    learned = _run("memory_combined.yaml", env, out)
    assert learned["confirmed"] >= 1 and learned["refused"] == 0

    # 2. The seller raises its price to 2195. The buyer orders from memory
    #    at 1995 and is refused: its memory is stale.
    stale = _run("memory_combined_price_change.yaml", env, out)
    assert stale["refused"] >= 1 and stale["confirmed"] == 0
    assert stale["recalled"][KEY]["asserter"] == "agent-a"

    # 3. A person corrects the price, on the record.
    fixed = correct(store, "agent-a", KEY, "2195",
                    reason="agent-b raised its price", by="alex")
    assert fixed.accepted, fixed.reason

    # 4. Next run: the buyer acts on the corrected price, and the trace
    #    says the value it acted on was set by a person.
    after = _run("memory_combined_price_change.yaml", env, out)
    assert after["confirmed"] >= 1 and after["refused"] == 0
    assert after["recalled"][KEY]["asserter"] == "human:alex"
    assert after["recalled"][KEY]["corrected"] is True
    assert after["stages"]["corrections_on_record"] == "passed"
    assert after["stages"]["agent_acted_on_correction"] == "passed"
    assert "question_skipped" in after["kinds"]
    assert after["verdict"] == "passed"

    # 5. The same fact edited around the store, back to the stale price.
    #    Under the default policy ("report") the agent believes the file,
    #    is refused again, and the run fails on the unexplained edit.
    def hand_edit():
        path = store_dir / "facts" / "agent-a" / "self" / \
            f"{store.current('agent-a', KEY, 'self').id}.md"
        path.write_text(path.read_text().replace("\n2195\n", "\n1995\n"))

    hand_edit()
    reported = _run("memory_combined_price_change.yaml", env, out)
    assert "memory_tampered" in reported["kinds"]
    assert reported["refused"] >= 1 and reported["confirmed"] == 0
    assert reported["stages"]["store_was_clean_at_open"] == "failed"
    assert reported["verdict"] == "failed"

    # 6. A correction cannot be layered over that edit.
    blocked = correct(store, "agent-a", KEY, "2195", reason="r", by="alex")
    assert not blocked.accepted and blocked.rule == "outside_edits_pending"

    # 7. With outside_edits = "restore", the run puts the file back before
    #    the agent reads it, and the corrected price stands.
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"preset": "default",
                                  "overrides": {"outside_edits": "restore"}}))
    restored = _run("memory_combined_price_change.yaml",
                    dict(env, TOWN_MEMORY_POLICY=str(policy)), out)
    assert "memory_restored" in restored["kinds"]
    assert restored["stages"]["outside_edits_restored"] == "passed"
    assert restored["confirmed"] >= 1 and restored["refused"] == 0
    assert restored["recalled"][KEY]["asserter"] == "human:alex"
    assert restored["verdict"] == "passed"
    assert outside_edits(store) == []
