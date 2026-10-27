"""Look inside the store from the terminal.

    python3 -m gitmem.inspect facts [agent] [--lane self|observed]
    python3 -m gitmem.inspect history <agent> <key>
    python3 -m gitmem.inspect log [n]
    python3 -m gitmem.inspect policy
    python3 -m gitmem.inspect check          # any fact edited outside the store?
    python3 -m gitmem.inspect try <agent> <key> <value> [--lane observed]
                                             # would the contract accept it?
    python3 -m gitmem.inspect events [run-id] # the memory trail of a run

Run it from the memory_mvp folder, or point TOWN_GITMEM_DIR somewhere else.
"""

from __future__ import annotations

import json
import os
import sys

from .contract import Contract, RejectedWrite
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
    dirty = store.dirty()
    if not dirty:
        print(f"clean · HEAD {store.head()} · "
              f"{len(store.active())} active fact(s)")
        return
    print(f"TAMPERED · {len(dirty)} fact file(s) changed outside the store:")
    for path in dirty:
        print(f"  {path}")
    print()
    print(store.diff())
    raise SystemExit(1)


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


FIELDS_SHOWN = ("key", "lane", "op", "commit", "hit", "rule", "reader",
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
            "events": events}


def main(argv: list[str]) -> None:
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        raise SystemExit(2)
    COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    main(sys.argv[1:])
