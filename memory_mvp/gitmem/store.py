"""Facts as files in a local git repo, one commit per batch of changes.

Adapted from git-for-memory (akg745) `gfm/store.py`. What changed for the
multi-agent case:

  * facts live under `facts/<holder>/<lane>/`, so one store holds several
    agents' memories without them sharing a namespace;
  * a fact records its `holder` (whose memory this is), its `lane`
    (`self` = the agent wrote it, `observed` = an external model derived it
    from the trace), its addressable `key`, and the `asserter` that claimed
    it, which is not always the holder;
  * commit timestamps come from the run's logical clock, so a replayed
    scenario produces the same history rather than wall-clock noise;
  * `dirty()` reports facts edited outside the store, which is how
    out-of-band tampering becomes visible.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

FIELDS = [
    "id", "holder", "lane", "key", "subject", "asserter", "type", "scope",
    "status", "confidence", "supersedes", "superseded_by", "valid_from",
    "invalid_at", "source", "evidence",
]

EPOCH = 1767225600  # 2026-01-01T00:00:00Z, the store's fixed clock origin


@dataclass
class Fact:
    id: str
    holder: str                      # whose memory this is
    key: str                         # how the holder addresses it
    statement: str                   # the value, as written
    subject: str = ""                # who or what the fact is about
    lane: str = "self"               # self | observed
    asserter: str = ""               # who claimed it (agent, or "observer")
    type: str = "string"
    scope: str = "private"           # private | project | shared
    status: str = "active"           # active | superseded | invalidated
    confidence: float = 1.0
    supersedes: str | None = None
    superseded_by: str | None = None
    valid_from: str = ""             # logical time of the run, as t=NN.NN
    invalid_at: str | None = None
    source: str = ""                 # run id + event id it came from
    evidence: str = ""               # verbatim quote backing an observed fact

    def to_file(self) -> str:
        d = asdict(self)
        head = "\n".join(f"{k}: {json.dumps(d[k], ensure_ascii=False)}"
                         for k in FIELDS)
        return f"---\n{head}\n---\n{self.statement}\n"

    @classmethod
    def from_file(cls, text: str) -> "Fact":
        match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
        if not match:
            raise ValueError("bad fact file")
        data: dict = {}
        for line in match.group(1).splitlines():
            key, _, value = line.partition(": ")
            try:
                data[key] = json.loads(value)
            except json.JSONDecodeError:
                data[key] = value
        data["statement"] = match.group(2).strip()
        allowed = [*FIELDS, "statement"]
        return cls(**{k: data[k] for k in allowed if k in data})

    @property
    def value(self):
        """The statement decoded back to its written type where possible."""
        try:
            return json.loads(self.statement)
        except json.JSONDecodeError:
            return self.statement


@dataclass
class Change:
    op: str                          # ADD | UPDATE | INVALIDATE
    fact: Fact
    old: Fact | None = None
    reason: str = ""


class Store:
    """A git repository of facts. Nothing is ever deleted."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser()
        self.facts_dir = self.root / "facts"

    # -- setup -----------------------------------------------------------

    def init(self) -> None:
        self.facts_dir.mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").exists():
            self._git("init", "-q", "-b", "main")
            (self.root / "README.md").write_text(
                "# agent memory\n\nOne file per fact. History is the point:\n"
                "`git log`, `git diff` and `git blame` answer what changed,\n"
                "when, who asserted it and on what evidence.\n")
            self._git("add", "README.md")
            self._commit("memory: empty store", when="0.00")

    def _git(self, *args: str, when: str | None = None) -> str:
        out = subprocess.run(
            ["git", "-C", str(self.root),
             "-c", "user.name=gitmem", "-c", "user.email=gitmem@localhost",
             "-c", "commit.gpgsign=false", *args],
            capture_output=True, text=True,
            env=_env(_stamp(when) if when is not None else None),
        )
        if out.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: "
                               f"{out.stderr.strip()}")
        return out.stdout

    def _commit(self, message: str, when: str) -> str:
        self._git("commit", "-q", "-m", message, when=when)
        return self._git("rev-parse", "--short", "HEAD").strip()

    # -- reading ----------------------------------------------------------

    def all(self) -> list[Fact]:
        if not self.facts_dir.exists():
            return []
        return [Fact.from_file(p.read_text())
                for p in sorted(self.facts_dir.rglob("*.md"))]

    def active(self, holder: str | None = None,
               lane: str | None = None) -> list[Fact]:
        return [f for f in self.all()
                if f.status == "active"
                and (holder is None or f.holder == holder)
                and (lane is None or f.lane == lane)]

    def current(self, holder: str, key: str, lane: str = "self") -> Fact | None:
        for fact in self.active(holder, lane):
            if fact.key == key:
                return fact
        return None

    def get(self, fact_id: str) -> Fact | None:
        for fact in self.all():
            if fact.id == fact_id:
                return fact
        return None

    def history(self, holder: str, key: str,
                lane: str | None = None) -> list[Fact]:
        """Every version of one key, oldest first."""
        versions = [f for f in self.all()
                    if f.holder == holder and f.key == key
                    and (lane is None or f.lane == lane)]
        return sorted(versions, key=lambda f: int(f.id.split("-")[1]))

    def log(self, limit: int = 50) -> list[str]:
        out = self._git("log", f"-{limit}", "--format=%h %s")
        return [line for line in out.splitlines() if line.strip()]

    def head(self) -> str:
        try:
            return self._git("rev-parse", "--short", "HEAD").strip()
        except RuntimeError:
            return ""

    def dirty(self) -> list[str]:
        """Fact files changed outside this store: the tamper signal."""
        out = self._git("status", "--porcelain", "--", "facts")
        return [line[3:].strip() for line in out.splitlines() if line.strip()]

    def diff(self, path: str | None = None) -> str:
        args = ["diff", "--", path] if path else ["diff", "--", "facts"]
        return self._git(*args)

    # -- writing -----------------------------------------------------------

    def _next_id(self) -> str:
        nums = [int(p.stem.split("-")[1])
                for p in self.facts_dir.rglob("f-*.md")]
        return f"f-{(max(nums) + 1 if nums else 1):04d}"

    def _path(self, fact: Fact) -> Path:
        return self.facts_dir / _slug(fact.holder) / fact.lane / f"{fact.id}.md"

    def _write_file(self, fact: Fact) -> None:
        path = self._path(fact)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fact.to_file())

    def apply(self, ops: list[dict], source: str, when: str) -> list[Change]:
        """Apply ADD / UPDATE / INVALIDATE operations.

        An op whose target has vanished is downgraded rather than guessed
        at: an UPDATE becomes an ADD, an INVALIDATE is dropped.
        """
        changes: list[Change] = []
        for op in ops:
            kind = (op.get("op") or "").upper()
            holder = op.get("holder", "")
            lane = op.get("lane", "self")
            key = op.get("key", "")
            target = None
            if op.get("target_id"):
                target = self.get(op["target_id"])
            elif key and kind in ("UPDATE", "INVALIDATE"):
                target = self.current(holder, key, lane)
            if kind == "UPDATE" and (target is None or target.status != "active"):
                kind = "ADD"
            if kind == "INVALIDATE" and (target is None
                                         or target.status != "active"):
                continue
            if kind not in ("ADD", "UPDATE", "INVALIDATE"):
                continue

            if kind == "INVALIDATE":
                target.status, target.invalid_at = "invalidated", when
                self._write_file(target)
                changes.append(Change("INVALIDATE", target,
                                      reason=op.get("reason", "")))
                continue

            # An ADD onto a key that already holds an active fact is an
            # update in everything but name; treat it as one so the chain
            # stays unbroken.
            if kind == "ADD" and target is None:
                target = self.current(holder, key, lane)
                if target is not None:
                    kind = "UPDATE"

            statement = op.get("statement", "")
            if statement == "":
                continue
            # Re-asserting a value that is already current is not a change.
            # Recording it as one would fill the history with "was: X,
            # now: X" and bury the versions that mean something.
            if (kind == "UPDATE" and target is not None
                    and target.statement == statement):
                changes.append(Change("NOOP", target, reason="unchanged"))
                continue
            new = Fact(
                id=self._next_id(),
                holder=holder,
                key=key,
                statement=statement,
                subject=op.get("subject", ""),
                lane=lane,
                asserter=op.get("asserter", holder),
                type=op.get("type", "string"),
                scope=op.get("scope", "private"),
                confidence=float(op.get("confidence", 1.0)),
                valid_from=when,
                source=source,
                evidence=str(op.get("evidence", ""))[:300],
            )
            if kind == "UPDATE":
                new.supersedes = target.id
                target.status = "superseded"
                target.superseded_by = new.id
                target.invalid_at = when
                self._write_file(target)
            self._write_file(new)
            changes.append(Change(kind, new,
                                  old=target if kind == "UPDATE" else None,
                                  reason=op.get("reason", "")))
        return changes

    def commit(self, changes: list[Change], source: str, title: str,
               when: str) -> str | None:
        changes = [c for c in changes if c.op != "NOOP"]
        if not changes:
            return None
        counts = {k: sum(c.op == k for c in changes)
                  for k in ("ADD", "UPDATE", "INVALIDATE")}
        summary = (f"+{counts['ADD']} added, ~{counts['UPDATE']} updated, "
                   f"-{counts['INVALIDATE']} invalidated")
        lines = []
        for change in changes:
            fact = change.fact
            head = f"[{fact.holder}/{fact.lane}] {fact.key}"
            if change.op == "UPDATE":
                lines.append(f"~ {change.old.id} -> {fact.id} {head}\n"
                             f"    was: {change.old.statement}\n"
                             f"    now: {fact.statement}")
            elif change.op == "INVALIDATE":
                lines.append(f"- {fact.id} {head}: {fact.statement}")
            else:
                lines.append(f"+ {fact.id} {head}: {fact.statement}")
            if change.reason:
                lines.append(f"    why: {change.reason}")
            if fact.evidence:
                lines.append(f"    evidence: {fact.evidence[:160]}")
            lines.append(f"    asserted by: {fact.asserter}")
        message = (f"memory: {summary} ({title})\n\n" + "\n".join(lines)
                   + f"\n\nSource: {source}\nLogical time: t={when}")
        # Stage only the files this batch wrote. `git add -A facts` would
        # sweep in any fact edited outside the store and commit it as if
        # the store had made the change — laundering a tamper into the
        # history that is supposed to expose it.
        paths = []
        for change in changes:
            for fact in (change.fact, change.old):
                if fact is None:
                    continue
                path = str(self._path(fact).relative_to(self.root))
                if path not in paths:
                    paths.append(path)
        self._git("add", "--", *paths)
        return self._commit(message, when=when)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", str(name).lower()).strip("-") or "agent"


def _stamp(when: str) -> str:
    """Turn logical time into a fixed ISO timestamp, so a replay of the
    same scenario produces the same commit history."""
    try:
        offset = int(float(when) * 1000)
    except (TypeError, ValueError):
        offset = 0
    return f"@{EPOCH + offset} +0000"


def _env(stamp: str | None) -> dict | None:
    import os

    if stamp is None:
        return None
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = stamp
    env["GIT_COMMITTER_DATE"] = stamp
    return env
