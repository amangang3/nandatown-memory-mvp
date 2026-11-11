"""Signed commits: who is allowed to have changed this memory.

Without signatures, the store can tell a file edited *around* it (git sees
a change nobody committed) but not a change someone made and then
committed with plain `git commit`: that commit sits in the history looking
like any other. With `signed_commits: "require"` every commit must carry an
SSH signature from a trusted key, and the right one for what it claims:

    gitmem-store     the store's own key; signs everything the store
                     commits for agents, the observer, run reports and
                     restores
    human:<name>     a person's key; signs that person's corrections, and
                     only theirs ("correction by human:alex" signed by
                     anyone else is not trusted)

Keys live outside the memory folder, in TOWN_GITMEM_KEYS (default
~/.config/gitmem/keys): the point is that someone able to edit the memory
files cannot also sign as the store. `allowed_signers` there is git's own
format, and it is the list of who may change memory at all.

The trust anchor is the first commit made with signing on. It is recorded
in the keys folder, not the store, so it cannot be edited along with the
memory. Commits before it are legacy (reported, not failed); if the anchor
is no longer in the history, the history was rewritten and nothing in it
is trusted.

Needs git 2.34 or newer and ssh-keygen.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .contract import HUMAN_PREFIX, KEY_SHAPE

STORE_PRINCIPAL = "gitmem-store"
UNDO_SUBJECT = "memory: undo untrusted commits"
CORRECTION_SUBJECT = re.compile(r"^memory: .*\(correction by (human:[^:)\s]+):")


def keys_dir() -> Path:
    return Path(os.environ.get("TOWN_GITMEM_KEYS")
                or Path.home() / ".config" / "gitmem" / "keys").expanduser()


@dataclass
class Signer:
    principal: str
    key: Path                       # private key file

    def git_flags(self) -> list[str]:
        return ["-c", "gpg.format=ssh", "-c", f"user.signingkey={self.key}",
                "-c", "commit.gpgsign=true"]


class Keyring:
    """The keys folder: private keys this machine holds, the list of
    trusted public keys, and the trust anchor of each store."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else keys_dir()
        self.allowed = self.root / "allowed_signers"
        self.anchors = self.root / "anchors.json"

    # -- keys ------------------------------------------------------------

    def _key_path(self, principal: str) -> Path:
        return self.root / re.sub(r"[^A-Za-z0-9_.-]+", "-", principal)

    def _check_principal(self, principal: str) -> None:
        if principal == STORE_PRINCIPAL:
            return
        name = principal[len(HUMAN_PREFIX):]
        if not principal.startswith(HUMAN_PREFIX) or not KEY_SHAPE.match(name):
            raise ValueError(f"a signer is {STORE_PRINCIPAL!r} or"
                             f" {HUMAN_PREFIX}<name>; got {principal!r}")

    def keygen(self, principal: str) -> Signer:
        """Make a key for a principal on this machine and trust it."""
        self._check_principal(principal)
        key = self._key_path(principal)
        if not key.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                            "-C", principal, "-f", str(key)], check=True)
        self.trust(principal, Path(str(key) + ".pub"))
        return Signer(principal, key)

    def trust(self, principal: str, public_key: Path) -> None:
        """Add a public key to the list of who may sign memory commits."""
        self._check_principal(principal)
        parts = Path(public_key).read_text().split()
        if len(parts) < 2 or not parts[0].startswith(("ssh-", "ecdsa-")):
            raise ValueError(f"{public_key} is not an SSH public key")
        line = f'{principal} namespaces="git" {parts[0]} {parts[1]}\n'
        self.root.mkdir(parents=True, exist_ok=True)
        existing = self.allowed.read_text() if self.allowed.exists() else ""
        if line not in existing:
            with open(self.allowed, "a") as handle:
                handle.write(line)

    def signer(self, principal: str, key: Path | None = None) -> Signer | None:
        """The signer for a principal, if this machine holds its key and
        the key is trusted."""
        key = Path(key) if key else self._key_path(principal)
        if not key.exists() or principal not in self.principals():
            return None
        # The key has to be the one trusted for this principal: holding
        # *a* trusted key is not enough to sign as someone else.
        derived = subprocess.run(["ssh-keygen", "-y", "-f", str(key)],
                                 capture_output=True, text=True)
        if derived.returncode != 0:
            return None
        public = " ".join(derived.stdout.split()[:2])
        if public not in self.trusted_keys(principal):
            return None
        return Signer(principal, key)

    def trusted_keys(self, principal: str) -> set[str]:
        if not self.allowed.exists():
            return set()
        keys = set()
        for line in self.allowed.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0] == principal:
                keys.add(f"{parts[2]} {parts[3]}")
        return keys

    def store_signer(self) -> Signer:
        """The store's own key, made on first use."""
        return (self.signer(STORE_PRINCIPAL)
                or self.keygen(STORE_PRINCIPAL))

    def principals(self) -> set[str]:
        if not self.allowed.exists():
            return set()
        return {line.split()[0] for line in self.allowed.read_text()
                .splitlines() if line.strip() and not line.startswith("#")}

    # -- anchors ------------------------------------------------------------

    def _anchors(self) -> dict:
        try:
            return json.loads(self.anchors.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def anchor(self, store_root: Path) -> str | None:
        return self._anchors().get(str(Path(store_root).resolve()))

    def set_anchor(self, store_root: Path, commit: str) -> None:
        data = self._anchors()
        data.setdefault(str(Path(store_root).resolve()), commit)
        self.root.mkdir(parents=True, exist_ok=True)
        self.anchors.write_text(json.dumps(data, indent=2) + "\n")


@dataclass
class CommitCheck:
    commit: str
    subject: str
    status: str          # trusted | legacy | undone | unsigned |
                         # untrusted_key | wrong_signer | bad_signature
    signer: str = ""
    expected: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("trusted", "legacy", "undone")


def expected_signer(subject: str) -> str:
    match = CORRECTION_SUBJECT.match(subject)
    return match.group(1) if match else STORE_PRINCIPAL


def verify(store, keyring: Keyring) -> dict:
    """Check every commit in the store against the trusted keys.

    Returns {"anchor", "rewritten", "commits": [CommitCheck], "bad": [...]}.
    """
    allowed = keyring.allowed if keyring.allowed.exists() else os.devnull
    out = store._git("-c", f"gpg.ssh.allowedSignersFile={allowed}", "log",
                     "--reverse", "--format=%H%x1f%G?%x1f%GS%x1f%s")
    rows = [line.split("\x1f") for line in out.splitlines() if line]
    anchor = keyring.anchor(store.root)
    hashes = [r[0] for r in rows]
    rewritten = bool(anchor) and anchor not in hashes
    start = hashes.index(anchor) if anchor in hashes else (
        0 if rewritten else len(hashes))
    checks = []
    for i, (commit, mark, signer, subject) in enumerate(rows):
        want = expected_signer(subject)
        if i < start:
            status = "legacy"
        elif mark == "N":
            status = "unsigned"
        elif mark in ("U", "E"):
            status = "untrusted_key"
        elif mark != "G":
            status = "bad_signature"
        elif signer != want:
            status = "wrong_signer"
        else:
            status = "trusted"
        checks.append(CommitCheck(commit[:12], subject, status, signer, want))
    # An untrusted commit that a later, trusted undo commit names has been
    # dealt with: it stays in the history, but it is no longer pending.
    undone: set[str] = set()
    for check in checks:
        if check.status == "trusted" and check.subject == UNDO_SUBJECT:
            body = store._git("show", "-s", "--format=%b", check.commit)
            for line in body.splitlines():
                if line.startswith("Untrusted: "):
                    undone |= {c.strip() for c in line[11:].split(",")}
    for check in checks:
        if not check.ok and check.commit in undone:
            check.status = "undone"
    return {"anchor": anchor, "rewritten": rewritten, "commits": checks,
            "bad": [c for c in checks if not c.ok]}


def start(store, keyring: Keyring) -> str:
    """Make sure the store has a trust anchor, and return it.

    A new store's first commit is already signed and becomes the anchor.
    An existing store with unsigned history gets one signed, empty commit
    saying signing was turned on; everything before it is legacy.
    """
    anchor = keyring.anchor(store.root)
    if anchor:
        return anchor
    allowed = keyring.allowed if keyring.allowed.exists() else os.devnull
    head = store._git("-c", f"gpg.ssh.allowedSignersFile={allowed}", "log",
                      "-1", "--format=%H%x1f%G?%x1f%GS").strip().split("\x1f")
    if not (len(head) == 3 and head[1] == "G" and head[2] == STORE_PRINCIPAL):
        store._git(*store.signer.git_flags(), "commit", "-q",
                   "--allow-empty", "-m",
                   "memory: signing turned on\n\nEvery commit from here on"
                   " must carry a trusted signature.")
    full = store._git("rev-parse", "HEAD").strip()
    keyring.set_anchor(store.root, full)
    return full


def undo_untrusted(store, keyring: Keyring, report: dict) -> list[dict]:
    """Put every fact file an untrusted commit touched back to its latest
    trusted version (or remove it, if no trusted commit ever wrote it),
    and commit that with the store's key."""
    bad = {c.commit for c in report["bad"]}
    trusted = {c.commit for c in report["commits"] if c.ok}
    touched: set[str] = set()
    for commit in bad:
        names = store._git("show", "--name-only", "--format=", commit)
        touched |= {n for n in names.splitlines() if n.startswith("facts/")}
    done = []
    for path in sorted(touched):
        history = store._git("log", "--format=%H", "--", path).split()
        good = next((h for h in history if h[:12] in trusted), None)
        if good is None:
            if (store.root / path).exists():
                store._git("rm", "-q", "--", path)
                done.append({"path": path, "action": "removed"})
            continue
        # The version that trusted commit left behind ("good" may have
        # deleted it, in which case it should not exist).
        exists = store._git("ls-tree", "--name-only", good, "--", path).strip()
        if exists:
            store._git("checkout", good, "--", path)
            done.append({"path": path, "action": "restored",
                         "from": good[:12]})
        elif (store.root / path).exists():
            store._git("rm", "-q", "--", path)
            done.append({"path": path, "action": "removed"})
    # `checkout <commit> -- path` and `rm` both stage what they change, so
    # the index already holds exactly this undo.
    # Always commit, even when nothing changed (an untrusted commit that
    # touched no fact): the undo commit is what records that these commits
    # were seen and dealt with.
    store._git(*store.signer.git_flags(), "commit", "-q", "--allow-empty",
               "-m", UNDO_SUBJECT + "\n\n"
               + ("\n".join(f"{d['action']} {d['path']}" for d in done)
                  or "no fact files to put back")
               + "\n\nUntrusted: " + ", ".join(sorted(bad)))
    return done
