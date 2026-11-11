"""Look inside the store from the terminal.

    python3 -m gitmem.inspect facts [agent] [--lane self|observed]
    python3 -m gitmem.inspect history <agent> <key>
    python3 -m gitmem.inspect log [n]
    python3 -m gitmem.inspect policy
    python3 -m gitmem.inspect check          # any fact edited outside the store?
    python3 -m gitmem.inspect try <agent> <key> <value> [--lane observed]
                                             # would the contract accept it?
    python3 -m gitmem.inspect correct <agent> <key> <value> --reason "why"
                      [--by NAME] [--key PATH] # change a fact on the record
    python3 -m gitmem.inspect restore        # put outside edits back
    python3 -m gitmem.inspect keygen <human:NAME|gitmem-store>
                                             # make a signing key and trust it
    python3 -m gitmem.inspect trust <human:NAME> <key.pub>
                                             # trust an existing SSH key
    python3 -m gitmem.inspect verify         # who signed every commit
    python3 -m gitmem.inspect events [run-id] # the memory trail of a run

Run it from the memory_mvp folder, or point TOWN_GITMEM_DIR somewhere else.
"""

from __future__ import annotations

import json
import os
import sys

from .contract import Contract, RejectedWrite
from .correct import correct as _correct
from .correct import outside_edits, restore as _restore
from .signing import Keyring, verify as _verify
from .policy import Policy
from .store import Store

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE_DIR = os.environ.get("TOWN_GITMEM_DIR") or os.path.join(
    HERE, "memory-git")


def _store() -> Store:
    store = Store(STORE_DIR)
    if not os.path.isdir(os.path.join(STORE_DIR, ".git")):
        print(f"no store at {STORE_DIR}; run a scenario first")
        raise SystemExit(1)
    return store


def facts(args: list[str]) -> None:
    store = _store()
    lane = None
    if "--lane" in args:
        lane = args[args.index("--lane") + 1]
        args = [a for i, a in enumerate(args)
                if i not in (args.index("--lane"), args.index("--lane") + 1)]
    holder = args[0] if args else None
    rows = store.active(holder, lane)
    if not rows:
        print("(nothing active)")
        return
    width = max(len(f.key) for f in rows)
    for fact in sorted(rows, key=lambda f: (f.holder, f.lane, f.key)):
        print(f"{fact.holder:<9} {fact.lane:<9} {fact.key:<{width}} "
              f"= {json.dumps(fact.value)}")
        print(f"{'':<9} {'':<9} {'':<{width}}   "
              f"by {fact.asserter} at t={fact.valid_from}"
              f" · {fact.id} · {fact.source}")
        if fact.evidence:
            print(f"{'':<9} {'':<9} {'':<{width}}   "
                  f"evidence: {fact.evidence[:100]}")


def history(args: list[str]) -> None:
    if len(args) < 2:
        print("usage: history <agent> <key>")
        raise SystemExit(2)
    store = _store()
    versions = store.history(args[0], args[1])
    if not versions:
        print("(no such key)")
        return
    for fact in versions:
        marker = {"active": "now", "superseded": "was",
                  "invalidated": "gone"}.get(fact.status, fact.status)
        print(f"{marker:<5} {fact.id}  lane={fact.lane:<9} "
              f"t={fact.valid_from:<6} by {fact.asserter:<9} "
              f"= {json.dumps(fact.value)}")
        if fact.superseded_by:
            print(f"      replaced by {fact.superseded_by} at t={fact.invalid_at}")
        if fact.evidence:
            print(f"      evidence: {fact.evidence[:110]}")


def log(args: list[str]) -> None:
    limit = int(args[0]) if args else 20
    for line in _store().log(limit):
        print(line)


def policy(_args: list[str]) -> None:
    loaded = Policy.load()
    for key, value in loaded.as_dict().items():
        print(f"{key:<36} {value}")


def check(_args: list[str]) -> None:
    store = _store()
    edits = outside_edits(store)
    unsigned = _signature_problems(store)
    if unsigned:
        print(f"UNTRUSTED · {len(unsigned)} commit(s) without a trusted"
              " signature; see `verify`")
    if not edits:
        print(f"clean · HEAD {store.head()} · "
              f"{len(store.active())} active fact(s)")
        if unsigned:
            raise SystemExit(1)
        return
    print(f"TAMPERED · {len(edits)} fact file(s) changed outside the store:")
    for edit in edits:
        print(f"  {edit['change']:<9} {edit['path']}")
    print()
    print(store.diff())
    print("Put them back with `restore`, or make the change on the record"
          " with `correct` after restoring.")
    raise SystemExit(1)


def _signature_problems(store) -> list:
    if not Policy.load().requires_signatures():
        return []
    report = _verify(store, Keyring())
    return report["bad"] + (["rewritten"] if report["rewritten"] else [])


def _option(args: list[str], name: str) -> tuple[str | None, list[str]]:
    if name not in args:
        return None, args
    at = args.index(name)
    if at + 1 >= len(args):
        print(f"{name} needs a value")
        raise SystemExit(2)
    return args[at + 1], args[:at] + args[at + 2:]


def correct(args: list[str]) -> None:
    """A person changing one fact in an agent's memory, on the record."""
    reason, args = _option(args, "--reason")
    by, args = _option(args, "--by")
    key_path, args = _option(args, "--key")
    if len(args) < 3:
        print('usage: correct <agent> <key> <value> --reason "why"'
              " [--by NAME]")
        raise SystemExit(2)
    holder, key, value = args[0], args[1], " ".join(args[2:])
    store = _store()
    outcome = _correct(store, holder, key, value, reason or "",
                       by=by or "operator", key_path=key_path)
    if not outcome.accepted:
        print(f"REFUSED [{outcome.rule}]")
        print(f"  {outcome.reason}")
        if "rule_for_key" in outcome.details:
            print(f"  rule for this key:"
                  f" {json.dumps(outcome.details['rule_for_key'])}")
        raise SystemExit(1)
    fact = store.get(outcome.fact)
    print(f"CORRECTED · commit {outcome.commit}")
    print(f"  {holder} {key}: {json.dumps(outcome.old_value)}"
          f" -> {json.dumps(fact.value)}")
    print(f"  {outcome.fact}"
          + (f" replaces {outcome.replaced}" if outcome.replaced else "")
          + f" · by {fact.asserter} · why: {outcome.reason}")
    print(f"  {holder} will act on this the next time it reads {key}.")


def keygen(args: list[str]) -> None:
    if len(args) != 1:
        print("usage: keygen <human:NAME|gitmem-store>")
        raise SystemExit(2)
    ring = Keyring()
    try:
        signer = ring.keygen(args[0])
    except ValueError as why:
        print(f"REFUSED · {why}")
        raise SystemExit(2)
    print(f"trusted {signer.principal} · key {signer.key}")
    print(f"  allowed signers: {ring.allowed}")


def trust(args: list[str]) -> None:
    if len(args) != 2:
        print("usage: trust <human:NAME> <public-key-file>")
        raise SystemExit(2)
    try:
        Keyring().trust(args[0], args[1])
    except (ValueError, FileNotFoundError) as why:
        print(f"REFUSED · {why}")
        raise SystemExit(2)
    print(f"trusted {args[0]} · {args[1]}")


def verify(_args: list[str]) -> None:
    store = _store()
    ring = Keyring()
    report = _verify(store, ring)
    print(f"anchor {(report['anchor'] or 'none')[:12]}"
          f" · trusted signers: {', '.join(sorted(ring.principals())) or 'none'}"
          f" · keys: {ring.root}")
    if report["rewritten"]:
        print("HISTORY REWRITTEN · the trust anchor is no longer in it;"
              " nothing here is trusted")
    for check in report["commits"]:
        mark = {"trusted": "ok", "legacy": "--", "undone": "~~"}.get(
            check.status, "!!")
        who = check.signer or "-"
        extra = (f"  (expected {check.expected})"
                 if check.status == "wrong_signer" else "")
        print(f"{mark} {check.commit[:7]} {check.status:<13} {who:<14}"
              f" {check.subject[:70]}{extra}")
    if report["bad"] or report["rewritten"]:
        raise SystemExit(1)


def restore(_args: list[str]) -> None:
    store = _store()
    edits = outside_edits(store)
    if not edits:
        print("clean · nothing to restore")
        return
    for done in _restore(store, edits):
        where = f" -> {done['moved_to']}" if "moved_to" in done else ""
        print(f"{done['action']:<11} {done['path']}{where}")


def try_write(args: list[str]) -> None:
    """Put a value through the contract without storing anything."""
    lane = "self"
    if "--lane" in args:
        at = args.index("--lane")
        lane = args[at + 1]
        args = [a for i, a in enumerate(args) if i not in (at, at + 1)]
    if len(args) < 3:
        print("usage: try <agent> <key> <value> [--lane observed]")
        raise SystemExit(2)
    holder, key, value = args[0], args[1], " ".join(args[2:])
    store = _store()
    agents = {f.holder for f in store.all()} or {holder}
    contract = Contract()
    policy_now = Policy.load()

    capture = policy_now.may_capture(lane)
    if not capture.allowed:
        print(f"BLOCKED by policy [{capture.rule}]")
        print(f"  {capture.reason}")
        return

    op = {"op": "ADD", "lane": lane, "holder": holder, "key": key,
          "value": value,
          "asserter": holder if lane == "self" else "observer",
          "evidence": "(demo)" if lane == "observed" else "",
          "source": "(demo)" if lane == "observed" else ""}
    try:
        checked = contract.check(op, agents)
    except RejectedWrite as why:
        print(f"REFUSED by the write contract")
        print(f"  {why}")
        print(f"  rule for this key: {json.dumps(contract.rule_for(key))}")
        return
    print("ACCEPTED (nothing was stored; this was a dry run)")
    print(f"  stored as: {checked['statement']}  type={checked['type']}"
          f"  scope={checked['scope']}")


FIELDS_SHOWN = ("key", "lane", "op", "commit", "hit", "asserter",
                "corrected", "fact", "count", "actions", "rule", "reader",
                "holder", "kind", "accepted", "unchanged", "rejected",
                "agreed", "omission", "contradiction", "unsupported",
                "preset", "lanes", "files", "model", "reason")


def events(args: list[str]) -> None:
    """The memory events of one run, from its evidence bundle."""
    runs = os.path.join(os.path.dirname(HERE), "runs")
    if not os.path.isdir(runs):
        runs = os.path.join(HERE, "..", "runs")
    run = args[0] if args else None
    if run is None:
        bundles = sorted(
            (os.path.join(runs, d) for d in os.listdir(runs)
             if d.startswith("sim-")),
            key=os.path.getmtime)
        if not bundles:
            print(f"no bundles under {runs}")
            raise SystemExit(1)
        path = bundles[-1]
    else:
        path = run if os.path.isdir(run) else os.path.join(runs, run)
    print(f"# {os.path.basename(path)}")
    with open(os.path.join(path, "events.jsonl")) as handle:
        for line in handle:
            event = json.loads(line)
            if not event["kind"].startswith("memory_"):
                continue
            detail = event["detail"] if isinstance(event["detail"], dict) else {}
            shown = {k: detail[k] for k in FIELDS_SHOWN if k in detail}
            reason = str(shown.pop("reason", ""))
            print(f"{event['event_id']:>6}  t={event['at']:<5.2f} "
                  f"{event['observer']:<9} {event['kind']:<24} "
                  f"{event['subject']:<9} "
                  + " ".join(f"{k}={json.dumps(v)}" for k, v in shown.items()))
            if reason:
                print(f"{'':>6}  {'':<7} {'':<9} {'':<24} {'':<9} "
                      f"reason: {reason[:110]}")


COMMANDS = {"facts": facts, "history": history, "log": log,
            "policy": policy, "check": check, "try": try_write,
            "events": events, "correct": correct, "restore": restore,
            "keygen": keygen, "trust": trust, "verify": verify}


def main(argv: list[str]) -> None:
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        raise SystemExit(2)
    COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    main(sys.argv[1:])
