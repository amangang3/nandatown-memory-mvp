"""Signed commits: catching an edit that was also committed.

    cd memory_mvp && python3 -m pytest -q test_signing.py

Needs git 2.34+ and ssh-keygen. The end-to-end test also needs `nandatown`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from gitmem.contract import Contract  # noqa: E402
from gitmem.correct import correct  # noqa: E402
from gitmem.policy import Policy  # noqa: E402
from gitmem.signing import (  # noqa: E402
    STORE_PRINCIPAL, Keyring, Signer, start, undo_untrusted, verify,
)
from gitmem.store import Store  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None,
                                reason="ssh-keygen is not installed")

KEY = "widget_unit_cents"
REQUIRE = Policy(signed_commits="require")


def _remember(store: Store, value, key: str = KEY) -> None:
    op = Contract().check({"op": "ADD", "lane": "self", "holder": "agent-a",
                           "asserter": "agent-a", "key": key, "value": value},
                          {"agent-a", "agent-b"})
    store.commit(store.apply([op], source="run", when="1.00"), source="run",
                 title=f"agent-a remembers {key}", when="1.00")


def _fact_file(store: Store) -> Path:
    fact = store.current("agent-a", KEY, "self")
    return store.root / "facts" / "agent-a" / "self" / f"{fact.id}.md"


def _plain_commit(store: Store, message: str = "fix price") -> None:
    """What someone with a shell and no key does: edit, then commit."""
    subprocess.run(["git", "-C", str(store.root), "-c", "user.name=x",
                    "-c", "user.email=x@x", "-c", "commit.gpgsign=false",
                    "commit", "-qam", message], check=True)


@pytest.fixture
def ring(tmp_path) -> Keyring:
    return Keyring(tmp_path / "keys")


@pytest.fixture
def store(tmp_path, ring) -> Store:
    s = Store(tmp_path / "memory", signer=ring.store_signer())
    s.init()
    start(s, ring)
    _remember(s, 1995)
    return s


def _statuses(store, ring) -> list[str]:
    return [c.status for c in verify(store, ring)["commits"]]


# -- the store signs, and verifies ---------------------------------------------

def test_a_signing_store_verifies_clean(store, ring):
    report = verify(store, ring)
    assert report["bad"] == [] and not report["rewritten"]
    assert set(_statuses(store, ring)) == {"trusted"}
    assert report["anchor"]


def test_an_edit_committed_without_a_key_is_caught(store, ring):
    path = _fact_file(store)
    path.write_text(path.read_text().replace("\n1995\n", "\n1000\n"))
    _plain_commit(store)
    # Git's working tree is clean: this is the gap signatures close.
    assert store._git("status", "--porcelain").strip() == ""
    bad = verify(store, ring)["bad"]
    assert [c.status for c in bad] == ["unsigned"]
    assert bad[0].subject == "fix price"


def test_a_key_nobody_trusts_is_caught(store, ring, tmp_path):
    stranger = tmp_path / "stranger"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f",
                    str(stranger)], check=True)
    _fact_file(store).write_text(
        _fact_file(store).read_text().replace("\n1995\n", "\n1000\n"))
    store._git("add", "-A")
    store._commit("memory: looks official", when="2",
                  signer=Signer(STORE_PRINCIPAL, stranger))
    assert [c.status for c in verify(store, ring)["bad"]] == ["untrusted_key"]


def test_a_correction_must_be_signed_by_the_person_it_names(store, ring):
    ring.keygen("human:alex")
    ring.keygen("human:sam")
    # sam signs a commit claiming to be alex's correction
    out = correct(store, "agent-a", KEY, "2195", reason="r", by="alex",
                  policy=REQUIRE, keyring=ring,
                  key_path=str(ring.root / "human-sam"))
    assert not out.accepted          # sam's key is not alex's principal
    # ...and the store key signing a "correction" is the wrong signer too
    op = Contract().check({"op": "UPDATE", "lane": "self",
                           "holder": "agent-a", "asserter": "human:alex",
                           "key": KEY, "value": 2195, "reason": "forged"},
                          {"agent-a"})
    store.commit(store.apply([op], source="x", when="2"), source="x",
                 title="correction by human:alex: agent-a widget_unit_cents",
                 when="2")
    bad = verify(store, ring)["bad"]
    assert [(c.status, c.signer, c.expected) for c in bad] == [
        ("wrong_signer", STORE_PRINCIPAL, "human:alex")]


def test_a_signed_correction_verifies_as_that_person(store, ring):
    ring.keygen("human:alex")
    out = correct(store, "agent-a", KEY, "2195", reason="price up",
                  by="alex", policy=REQUIRE, keyring=ring)
    assert out.accepted, out.reason
    last = verify(store, ring)["commits"][-1]
    assert (last.status, last.signer) == ("trusted", "human:alex")


def test_a_correction_needs_a_trusted_key(store, ring):
    out = correct(store, "agent-a", KEY, "2195", reason="r", by="alex",
                  policy=REQUIRE, keyring=ring)
    assert not out.accepted and out.rule == "signed_commits"


def test_no_correction_on_top_of_an_untrusted_commit(store, ring):
    ring.keygen("human:alex")
    _fact_file(store).write_text(
        _fact_file(store).read_text().replace("\n1995\n", "\n1000\n"))
    _plain_commit(store)
    out = correct(store, "agent-a", KEY, "2195", reason="r", by="alex",
                  policy=REQUIRE, keyring=ring)
    assert not out.accepted and out.rule == "untrusted_commits_pending"


# -- undoing untrusted commits ----------------------------------------------------

def test_undo_puts_back_changed_and_removes_planted_facts(store, ring):
    path = _fact_file(store)
    path.write_text(path.read_text().replace("\n1995\n", "\n1000\n"))
    planted = path.parent / "f-0999.md"
    planted.write_text('---\nid: "f-0999"\nholder: "agent-a"\n---\n1\n')
    store._git("add", "-A")
    _plain_commit(store)

    report = verify(store, ring)
    done = {d["path"].rsplit("/", 1)[1]: d["action"]
            for d in undo_untrusted(store, ring, report)}
    assert done == {path.name: "restored", "f-0999.md": "removed"}
    assert store.current("agent-a", KEY, "self").value == 1995
    after = verify(store, ring)
    assert after["bad"] == []
    assert "undone" in [c.status for c in after["commits"]]
    # A second pass has nothing pending and does not fail.
    assert undo_untrusted(store, ring, after) == []


# -- turning signing on, and history -------------------------------------------------

def test_turning_signing_on_for_an_existing_store(tmp_path, ring):
    old = Store(tmp_path / "old")
    old.init()
    _remember(old, 1995)                   # unsigned history
    signed = Store(old.root, signer=ring.store_signer())
    start(signed, ring)
    statuses = _statuses(signed, ring)
    assert statuses[:-1] == ["legacy"] * (len(statuses) - 1)
    assert statuses[-1] == "trusted"       # "signing turned on"
    assert verify(signed, ring)["bad"] == []


def test_a_rewritten_history_is_not_trusted(store, ring):
    first = store._git("rev-list", "--max-parents=0", "HEAD").strip()
    store._git("reset", "-q", "--hard", first)
    store._git("commit", "-q", "--amend", "-m", "memory: rewritten")
    report = verify(store, ring)
    assert report["rewritten"]


def test_principals_are_checked(ring):
    with pytest.raises(ValueError):
        ring.keygen("alex")
    with pytest.raises(ValueError):
        ring.keygen("human:not a name")


# -- end to end, through NANDA Town ---------------------------------------------------

TOWN = next((p for p in [Path(sys.executable).parent / "nandatown"]
             if p.exists()), None) or shutil.which("nandatown")


def _run(scenario: str, env: dict, out: Path) -> dict:
    proc = subprocess.run([str(TOWN), "run", str(HERE / scenario),
                           "--out", str(out)], env=env, capture_output=True,
                          text=True, cwd=HERE.parent)
    line = [l for l in proc.stdout.splitlines()
            if l.startswith("Evidence bundle: ")]
    assert line, proc.stdout[-1500:] + proc.stderr[-1500:]
    bundle = Path(line[0].split(": ", 1)[1].strip())
    kinds = [json.loads(l)["kind"] for l in
             (bundle / "events.jsonl").read_text().splitlines()]
    result = json.loads((bundle / "result.json").read_text())
    return {"verdict": result["verdict"],
            "stages": {s["name"]: s["status"] for s in result["stages"]},
            "confirmed": kinds.count("order_confirmed"),
            "refused": kinds.count("order_refused")}


@pytest.mark.skipif(TOWN is None, reason="nandatown is not installed")
def test_signed_store_through_town(tmp_path):
    policy = tmp_path / "require.json"
    policy.write_text(json.dumps({"preset": "default", "overrides": {
        "signed_commits": "require"}}))
    restore = tmp_path / "restore.json"
    restore.write_text(json.dumps({"preset": "default", "overrides": {
        "signed_commits": "require", "outside_edits": "restore"}}))
    keys = tmp_path / "keys"
    env = dict(os.environ, TOWN_GITMEM_DIR=str(tmp_path / "memory-git"),
               TOWN_GITMEM_KEYS=str(keys), TOWN_MEMORY_POLICY=str(policy),
               NANDATOWN_HOME=str(tmp_path / "home"))
    env.pop("TOWN_MEMORY_PRESET", None)
    out = tmp_path / "runs"
    ring = Keyring(keys)

    learned = _run("memory_combined.yaml", env, out)
    assert learned["stages"]["commits_signed"] == "passed"
    _run("memory_combined_price_change.yaml", env, out)

    ring.keygen("human:alex")
    store = Store(tmp_path / "memory-git", signer=ring.store_signer())
    assert correct(store, "agent-a", KEY, "2195", reason="price up",
                   by="alex", policy=REQUIRE, keyring=ring).accepted
    fixed = _run("memory_combined_price_change.yaml", env, out)
    assert fixed["confirmed"] >= 1 and fixed["verdict"] == "passed"
    assert fixed["stages"]["commits_signed"] == "passed"

    # Edit the corrected price back, and commit it without a key.
    path = _fact_file(store)
    path.write_text(path.read_text().replace("\n2195\n", "\n1995\n"))
    _plain_commit(store)

    reported = _run("memory_combined_price_change.yaml", env, out)
    assert reported["stages"]["commits_signed"] == "failed"
    assert reported["refused"] >= 1 and reported["verdict"] == "failed"

    undone = _run("memory_combined_price_change.yaml",
                  dict(env, TOWN_MEMORY_POLICY=str(restore)), out)
    assert undone["stages"]["untrusted_commits_undone"] == "passed"
    assert undone["confirmed"] >= 1 and undone["verdict"] == "passed"

    settled = _run("memory_combined_price_change.yaml", env, out)
    assert settled["stages"]["commits_signed"] == "passed"
    assert settled["verdict"] == "passed"
