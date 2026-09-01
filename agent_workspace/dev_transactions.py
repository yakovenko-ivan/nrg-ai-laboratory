#!/usr/bin/env python3
"""Guarded development transactions for the NRG AI Laboratory (v1, corrected v4).

Trusted infrastructure implementing a plan/confirm/execute development
transaction over an *isolated* Git worktree of the nrg-ai-laboratory
repository, so explicitly approved infrastructure fixes can be authored,
validated, and committed on a development branch without broadening ordinary
agent write permissions.

V4 correction log (defects found by independent V3 test execution)
-------------------------------------------------------------------
1. **Directory-prefix semantics survive allowlist normalization** —
   ``_normalize_allowlist_entry`` preserves the trailing ``/`` so
   ``allowed_paths = ["tests/"]`` persists through plan/start/record and
   ``_allow_match("tests/a.py", ["tests/"])`` accepts the descendant.
2. **Path-aware glob matching** — ``_path_aware_fnmatch`` ensures ``*`` does
   NOT span ``/``, so ``campaign_tools/*.py`` matches
   ``campaign_tools/foo.py`` but NOT ``campaign_tools/sub/foo.py``.
3. **Mutation-test fixture** — corrected in the test file (explicit ``git add``
   before commit).
4. **Refuse test execution when the working tree is not the candidate index** —
   ``test_plan`` returns ``can_run=false`` and ``test_execute`` raises if
   unexpected unstaged/untracked source changes exist before launch; the same
   clean-candidate precondition is applied to patch application.
5. **Bind test and commit to HEAD as well as candidate tree** — ``test_plan``
   reports ``current_head_sha``/``current_branch``/``candidate_tree_sha``;
   ``test_execute`` verifies ``expected_head_sha``/``expected_branch``;
   ``commit_execute`` verifies ``expected_head_sha``/``expected_branch`` and
   rejects a stale HEAD (new commit with same tree) as stale provenance.
6. **Bind discard confirmation to the exact state being discarded** —
   ``discard_plan`` reports head/tree/branch/staged/unexpected;
   ``discard_execute`` requires confirmed state fields and rejects if the
   worktree changed after confirmation; return codes from ``git worktree
   remove`` and ``git branch -D`` are verified; ``final_state="discarded"`` is
   not recorded when cleanup was not actually completed.

Design invariants (carried from v3)
-----------------------------------
* Canonical transaction-id grammar shared by creation and validation.
* Candidate state = the Git **index**; ``candidate_tree_sha = git write-tree``.
* Every plan->confirm->execute transition is bound to the exact confirmed plan.
* Commit requires current-tree ``diff_check=pass`` AND a current-tree pytest
  profile with ``pass``.
* Transaction records are invariant-checked on every load against trusted
  derivation.
* Patch artifacts must resolve under the configured studies workspace.
* Semantic write/edit operations accept plain UTF-8 text intent and have the trusted engine generate and persist the canonical Git patch artifact.
* Plan/read-only operations are side-effect-free.
* No push/merge/rebase/tag/runtime promotion. Test execution is NOT an OS
  sandbox; candidate Python runs with the local user's permissions.

Python 3.11+; standard library only.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1

# Canonical transaction-id grammar: dev-YYYYMMDDTHHMMSS-<6hex>
TID_RE = re.compile(r"dev-[0-9]{8}T[0-9]{6}-[0-9a-f]{6}")


class DevTransactionError(Exception):
    """Raised for deterministic transaction policy/validation failures."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_tid(tid: str) -> bool:
    return bool(TID_RE.fullmatch(tid or ""))


def _run_git(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "--no-pager", "--no-optional-locks", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _repo_root(lab) -> Path:
    root = Path(getattr(lab, "research_root", "")).expanduser()
    if not root.is_dir():
        raise DevTransactionError(f"research_root is not a directory: {root}")
    return root.resolve()


def _studies_root(lab) -> Path:
    sr = getattr(lab, "studies_root", None)
    if sr:
        return Path(sr).expanduser().resolve()
    return _repo_root(lab) / "agent_workspace" / "studies"


def _git_dir(repo: Path) -> Path:
    out = _run_git(["rev-parse", "--git-dir"], repo)
    if out.returncode != 0:
        raise DevTransactionError(f"not a git repository: {repo}: {out.stderr.strip()}")
    gd = (repo / out.stdout.strip()).resolve()
    if not gd.is_dir():
        raise DevTransactionError(f"resolved git dir is invalid: {gd}")
    return gd


def _inside(root: Path, target: Path) -> bool:
    try:
        rel = Path(os.path.relpath(target.resolve(), root.resolve()))
    except ValueError:
        return False
    if rel == Path("."):
        return True
    return not (rel.is_absolute() or (rel.parts and rel.parts[0] == ".."))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _short_id() -> str:
    return secrets.token_hex(3)


def _new_transaction_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"dev-{stamp}-{_short_id()}"


def _branch_name(tid: str) -> str:
    return f"agent-dev/{tid}"


# Path computation (side-effect-free; no mkdir):
def _worktree_root_path(lab, policy: dict) -> Path:
    return _repo_root(lab) / Path(policy.get("worktrees_root", ".local/dev_worktrees"))


def _transactions_root_path(lab, policy: dict) -> Path:
    return _repo_root(lab) / Path(policy.get("transactions_root", ".local/dev_transactions"))


def _worktree_dir_path(lab, policy: dict, tid: str) -> Path:
    return _worktree_root_path(lab, policy) / tid


def _transaction_path(lab, policy: dict, tid: str) -> Path:
    if not _valid_tid(tid):
        raise DevTransactionError(f"invalid transaction_id format: {tid!r}")
    return _transactions_root_path(lab, policy) / f"{tid}.json"


def _load_policy(lab) -> dict:
    path = _repo_root(lab) / "config" / "dev_transaction_policy.json"
    if not path.is_file():
        raise DevTransactionError(f"dev transaction policy missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if int(data.get("schema_version", -1)) != SCHEMA_VERSION:
        raise DevTransactionError("unsupported dev_transaction_policy schema_version")
    return data


def _validate_record_invariants(lab, policy: dict, rec: dict) -> None:
    """On every load, reject a record whose security fields diverge from trusted derivation."""
    tid = rec.get("transaction_id", "")
    if not _valid_tid(tid):
        raise DevTransactionError(f"record transaction_id is malformed: {tid!r}")
    repo = _repo_root(lab)
    if rec.get("repository_root") != str(repo):
        raise DevTransactionError("record repository_root does not match configured research root")
    if rec.get("worktree") != str(_worktree_dir_path(lab, policy, tid)):
        raise DevTransactionError("record worktree is not the canonical transaction worktree")
    if rec.get("development_branch") != _branch_name(tid):
        raise DevTransactionError("record development_branch is not canonical")
    if rec.get("denylist") != policy["denylist"]:
        raise DevTransactionError("record denylist does not match current trusted policy")
    if rec.get("self_protected_paths") != policy.get("self_protected_paths", []):
        raise DevTransactionError("record self_protected_paths does not match current trusted policy")


def _load_transaction(lab, policy: dict, tid: str) -> dict:
    path = _transaction_path(lab, policy, tid)
    if not path.is_file():
        raise DevTransactionError(f"no such transaction: {tid}")
    rec = json.loads(path.read_text(encoding="utf-8"))
    _validate_record_invariants(lab, policy, rec)
    return rec


def _save_transaction(lab, policy: dict, tid: str, payload: dict) -> None:
    path = _transaction_path(lab, policy, tid)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _current_head(repo_or_wt: Path) -> str:
    out = _run_git(["rev-parse", "HEAD"], repo_or_wt)
    if out.returncode != 0:
        raise DevTransactionError(f"cannot resolve HEAD: {out.stderr.strip()}")
    return out.stdout.strip()


def _current_branch(wt: Path) -> str:
    out = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], wt)
    if out.returncode != 0:
        raise DevTransactionError(f"cannot resolve current branch: {out.stderr.strip()}")
    return out.stdout.strip()


def _write_tree(wt: Path) -> str:
    out = _run_git(["write-tree"], wt)
    if out.returncode != 0:
        raise DevTransactionError(f"git write-tree failed: {out.stderr.strip()}")
    return out.stdout.strip()


def _porcelain_status(repo_or_wt: Path) -> list[str]:
    out = _run_git(["status", "--porcelain"], repo_or_wt)
    if out.returncode != 0:
        raise DevTransactionError(f"git status failed: {out.stderr.strip()}")
    return [line for line in out.stdout.splitlines() if line.strip()]


def _staged_paths(wt: Path) -> list[str]:
    out = _run_git(["diff", "--cached", "--name-only"], wt)
    if out.returncode != 0:
        raise DevTransactionError(f"git diff --cached failed: {out.stderr.strip()}")
    return [x for x in out.stdout.splitlines() if x.strip()]


def _unexpected_worktree_changes(wt: Path) -> list[str]:
    """Return porcelain lines for unstaged tracked modifications and untracked files.

    These are changes in the working tree that are NOT in the index.  If non-empty
    before a test, the working tree does not match the candidate index, so the test
    would run against code not represented by ``candidate_tree_sha``.
    """
    out = []
    for line in _porcelain_status(wt):
        if len(line) < 2:
            out.append(line)
            continue
        x, y = line[0], line[1]
        # Untracked files (??) and unstaged working-tree modifications (second char != ' ')
        if x == "?" and y == "?":
            out.append(line)
        elif y != " ":
            out.append(line)
    return out


# ---------------------------------------------------------------------------
# path policy
# ---------------------------------------------------------------------------

_GLOB_CHARS = set("*?[]")


def _normalize_repo_rel(root: Path, raw: str) -> str:
    """Normalize a repository-relative path (no trailing-slash semantics)."""
    p = Path(raw)
    if p.is_absolute() or ".." in p.parts:
        raise DevTransactionError(f"path is absolute or traverses: {raw!r}")
    s = p.as_posix()
    if s.startswith("/"):
        raise DevTransactionError(f"absolute path rejected: {raw!r}")
    return s


def _normalize_allowlist_entry(root: Path, raw: str) -> str:
    """Normalize an allowlist entry, PRESERVING directory-prefix semantics.

    A trailing ``/`` means directory + descendants.  This must survive
    canonicalization so the persisted transaction contract remains unambiguous.

    V4 defect #1: previously ``_normalize_repo_rel`` converted
    ``Path("tests/").as_posix()`` to ``"tests"``, losing the trailing ``/`` and
    breaking descendant matching.
    """
    is_dir = raw.endswith("/")
    p = Path(raw)
    if p.is_absolute() or ".." in p.parts:
        raise DevTransactionError(f"allowlist path is absolute or traverses: {raw!r}")
    s = p.as_posix()
    if s.startswith("/"):
        raise DevTransactionError(f"absolute allowlist path rejected: {raw!r}")
    if not s:
        raise DevTransactionError(f"empty allowlist entry: {raw!r}")
    # Preserve the trailing '/' so _allow_match sees directory semantics.
    return s + "/" if is_dir else s


def _is_glob(pat: str) -> bool:
    return any(c in pat for c in _GLOB_CHARS)


def _path_aware_fnmatch(rel: str, pattern: str) -> bool:
    """fnmatch where ``*`` does NOT span ``/`` (POSIX path-aware matching).

    V4 defect #2: ``fnmatch.fnmatch("campaign_tools/sub/foo.py",
    "campaign_tools/*.py")`` is True because Python fnmatch allows ``*`` to
    span ``/``.  This function ensures ``campaign_tools/*.py`` matches
    ``campaign_tools/foo.py`` but NOT ``campaign_tools/sub/foo.py``.
    """
    parts: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == '*':
            parts.append('[^/]*')
        elif c == '?':
            parts.append('[^/]')
        elif c == '[':
            j = i + 1
            if j < n and pattern[j] in ('!', '^'):
                j += 1
            if j < n and pattern[j] == ']':
                j += 1
            while j < n and pattern[j] != ']':
                j += 1
            if j >= n:
                parts.append(re.escape(c))
            else:
                cls_content = pattern[i + 1:j]
                if cls_content.startswith('!'):
                    cls_content = '^' + cls_content[1:]
                parts.append('[' + cls_content + ']')
                i = j
        else:
            parts.append(re.escape(c))
        i += 1
    regex = '^' + ''.join(parts) + r'\Z'
    return re.match(regex, rel) is not None


def _prefix_match_dir(rel: str, entry_dir: str) -> bool:
    """entry ending '/' => directory and descendants."""
    e = entry_dir.rstrip("/")
    if not e:
        return False
    return rel == e or rel.startswith(e + "/")


def _deny_match(rel: str, denylist: list[str]) -> bool:
    rel = rel.strip("/")
    for entry in denylist:
        e = entry.strip("/")
        if not e:
            continue
        if entry.endswith("/"):
            if rel == e or rel.startswith(e + "/"):
                return True
        elif _is_glob(e):
            if _path_aware_fnmatch(rel, e):
                return True
        else:
            if rel == e or rel.startswith(e + "/"):
                return True
    return False


def _exact_or_prefix_match(rel: str, patterns: list[str]) -> bool:
    rel = rel.strip("/")
    for entry in patterns:
        e = entry.strip("/")
        if not e:
            continue
        if entry.endswith("/"):
            if rel == e or rel.startswith(e + "/"):
                return True
        else:
            if rel == e or rel.startswith(e + "/"):
                return True
    return False


def _allow_match(rel: str, allowlist: list[str]) -> bool:
    """Directory-prefix / exact / path-aware-glob allowlist (documented contract).

    - entry ending '/'  => that directory and all descendants
    - entry with glob metacharacters (* ? [ ]) => path-aware fnmatch (* does NOT span /)
    - otherwise => exact file match
    """
    rel = rel.strip("/")
    for entry in allowlist:
        e = entry.strip("/")
        if not e:
            continue
        if entry.endswith("/"):
            if rel == e or rel.startswith(e + "/"):
                return True
        elif _is_glob(e):
            if _path_aware_fnmatch(rel, e):
                return True
        else:
            if rel == e:
                return True
    return False


def _validate_touched_path(root: Path, rel: str, allowlist: list[str],
                           denylist: list[str], self_protected: list[str]) -> None:
    norm = _normalize_repo_rel(root, rel)
    if _deny_match(norm, denylist):
        raise DevTransactionError(f"patch touches denied path: {norm}")
    if _exact_or_prefix_match(norm, self_protected):
        raise DevTransactionError(
            f"patch touches self-protected development-TCB path: {norm}; "
            "changes to the guarded-development security boundary require an "
            "out-of-band security bootstrap, not an ordinary nrg_dev_* transaction."
        )
    if not _allow_match(norm, allowlist):
        raise DevTransactionError(f"patch touches path outside transaction allowlist: {norm}")
    target = (root / norm).resolve()
    if not _inside(root, target):
        raise DevTransactionError(f"path resolves outside checkout: {norm}")
    cur = root
    for part in Path(norm).parts:
        cur = cur / part
        if cur.is_symlink():
            real = cur.resolve(strict=False)
            if not _inside(root, real):
                raise DevTransactionError(f"symlink escapes checkout: {cur} -> {real}")


def _validate_patch_artifact_path(lab, patch_path: str) -> Path:
    """Patch artifacts must resolve under the configured studies workspace."""
    sr = _studies_root(lab)
    p = Path(patch_path).expanduser()
    if not p.is_file():
        raise DevTransactionError(f"patch artifact not found or not a regular file: {patch_path}")
    resolved = p.resolve()
    if p.is_symlink():
        if not _inside(sr, resolved):
            raise DevTransactionError(f"patch artifact symlink escapes studies workspace: {patch_path}")
    if not _inside(sr, resolved):
        raise DevTransactionError(f"patch artifact is outside studies workspace: {patch_path}")
    if not resolved.is_file():
        raise DevTransactionError(f"patch artifact is not a regular file: {patch_path}")
    return resolved


# ---------------------------------------------------------------------------
# patch parsing
# ---------------------------------------------------------------------------

_PATCH_PATH_RE = re.compile(r"^\+\+\+ b/(.+)$")
_PATCH_OLD_RE = re.compile(r"^--- a/(.+)$")
_BINARY_MARKERS = ("Binary files", "GIT binary patch")


def _parse_patch(patch_text: str) -> dict:
    touched: set[str] = set()
    is_binary = False
    insertions = 0
    deletions = 0
    for line in patch_text.splitlines():
        if any(line.startswith(m) for m in _BINARY_MARKERS):
            is_binary = True
            continue
        m = _PATCH_PATH_RE.match(line)
        if m:
            touched.add(m.group(1).strip())
            continue
        m = _PATCH_OLD_RE.match(line)
        if m:
            touched.add(m.group(1).strip())
            continue
        if line.startswith("+") and not line.startswith("+++"):
            insertions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    touched.discard("/dev/null")
    return {
        "touched": sorted(touched),
        "is_binary": is_binary,
        "insertions": insertions,
        "deletions": deletions,
    }


# ---------------------------------------------------------------------------
# transaction lifecycle
# ---------------------------------------------------------------------------

def _normalize_allowlist(repo: Path, allowed_paths: list[str]) -> list[str]:
    out = [_normalize_allowlist_entry(repo, raw) for raw in allowed_paths]
    if not out:
        raise DevTransactionError("transaction allowlist must be non-empty")
    return out


def transaction_plan(lab, task: str, rationale: str, allowed_paths: list[str]) -> dict:
    """Read-only preflight. No filesystem side effects (no .local/ creation)."""
    repo = _repo_root(lab)
    policy = _load_policy(lab)
    _git_dir(repo)
    dirty = _porcelain_status(repo)
    allow = _normalize_allowlist(repo, allowed_paths)
    tid = _new_transaction_id()
    return {
        "can_start": not dirty,
        "transaction_id": tid,
        "issue": task,
        "rationale": rationale,
        "repository_root": str(repo),
        "baseline_sha": _current_head(repo) if not dirty else None,
        "development_branch": _branch_name(tid),
        "worktree": str(_worktree_dir_path(lab, policy, tid)),
        "allowed_paths": allow,
        "denylist": policy["denylist"],
        "self_protected_paths": policy.get("self_protected_paths", []),
        "dirty_paths": dirty,
        "dirty_count": len(dirty),
    }


def transaction_start(lab, transaction_id: str, task: str, rationale: str,
                       allowed_paths: list[str], baseline_sha: str) -> dict:
    if not _valid_tid(transaction_id):
        raise DevTransactionError(f"invalid transaction_id format: {transaction_id!r}")
    repo = _repo_root(lab)
    policy = _load_policy(lab)
    _git_dir(repo)
    dirty = _porcelain_status(repo)
    if dirty:
        raise DevTransactionError(f"baseline worktree is dirty; refuse to start: {dirty[:8]}")
    head = _current_head(repo)
    if head != baseline_sha:
        raise DevTransactionError(
            f"baseline HEAD mismatch: plan expected {baseline_sha}, now {head}"
        )
    allow = _normalize_allowlist(repo, allowed_paths)
    branch = _branch_name(transaction_id)
    wt_root = _worktree_root_path(lab, policy)
    wt = _worktree_dir_path(lab, policy, transaction_id)
    if wt.exists():
        raise DevTransactionError(f"worktree already exists: {wt}")
    wt_root.mkdir(parents=True, exist_ok=True)
    out = _run_git(["worktree", "add", "-b", branch, str(wt), baseline_sha], repo)
    if out.returncode != 0:
        raise DevTransactionError(f"git worktree add failed: {out.stderr.strip()}")
    record = {
        "schema_version": SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "created_at_utc": utc_now(),
        "issue": task,
        "rationale": rationale,
        "repository_root": str(repo),
        "baseline_sha": baseline_sha,
        "development_branch": branch,
        "worktree": str(wt),
        "allowed_paths": allow,
        "denylist": policy["denylist"],
        "self_protected_paths": policy.get("self_protected_paths", []),
        "patches": [],
        "test_results": [],
        "development_commit_sha": None,
        "final_state": "active",
    }
    try:
        _save_transaction(lab, policy, transaction_id, record)
    except Exception:
        # never leave an orphan worktree/branch on record failure
        _run_git(["worktree", "remove", "--force", str(wt)], repo)
        _run_git(["branch", "-D", branch], repo)
        raise
    return {
        "started": True,
        "transaction_id": transaction_id,
        "baseline_sha": baseline_sha,
        "development_branch": branch,
        "worktree": str(wt),
        "allowed_paths": allow,
    }


def transaction_status(lab, tid: str) -> dict:
    policy = _load_policy(lab)
    rec = _load_transaction(lab, policy, tid)
    wt = Path(rec["worktree"])
    if not wt.is_dir():
        return {**rec, "worktree_present": False, "final_state": rec.get("final_state", "active")}
    dirty = _porcelain_status(wt)
    head = _current_head(wt)
    branch = _current_branch(wt)
    candidate_tree = _write_tree(wt)
    last_test = rec["test_results"][-1] if rec["test_results"] else None
    return {
        "transaction_id": tid,
        "baseline_sha": rec["baseline_sha"],
        "current_head_sha": head,
        "candidate_tree_sha": candidate_tree,
        "development_branch": rec["development_branch"],
        "current_branch": branch,
        "worktree": str(wt),
        "worktree_present": True,
        "allowed_paths": rec["allowed_paths"],
        "staged_paths": _staged_paths(wt),
        "unexpected_changes": _unexpected_worktree_changes(wt),
        "porcelain": dirty,
        "patch_count": len(rec["patches"]),
        "last_test": last_test,
        "development_commit_sha": rec.get("development_commit_sha"),
        "final_state": rec.get("final_state", "active"),
    }


def transaction_list(lab) -> dict:
    policy = _load_policy(lab)
    root = _transactions_root_path(lab, policy)
    items = []
    if root.is_dir():
        for p in sorted(root.glob("dev-*.json")):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            items.append({
                "transaction_id": rec.get("transaction_id"),
                "issue": rec.get("issue"),
                "branch": rec.get("development_branch"),
                "final_state": rec.get("final_state"),
                "baseline_sha": rec.get("baseline_sha"),
            })
    return {"transactions": items, "count": len(items)}


def read_file(lab, tid: str, relpath: str, offset: int = 1, limit: int = 2000) -> dict:
    policy = _load_policy(lab)
    rec = _load_transaction(lab, policy, tid)
    wt = Path(rec["worktree"])
    if not wt.is_dir():
        raise DevTransactionError(f"worktree is missing for transaction {tid}")
    norm = _normalize_repo_rel(wt, relpath)
    target = (wt / norm).resolve()
    if not _inside(wt, target) or not target.is_file():
        raise DevTransactionError(f"path is outside worktree or not a file: {relpath}")
    max_lines = int(policy.get("max_read_lines", 2000))
    limit = max(1, min(int(limit), max_lines))
    offset = max(1, int(offset))
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    chunk = lines[offset - 1: offset - 1 + limit]
    return {
        "transaction_id": tid,
        "path": norm,
        "total_lines": total,
        "offset": offset,
        "limit": limit,
        "lines": chunk,
    }


# ---------------------------------------------------------------------------
# patch apply (stages into the index -> candidate tree)
# ---------------------------------------------------------------------------

def _read_patch(patch_path: str, lab) -> tuple[Path, str, str, dict]:
    p = _validate_patch_artifact_path(lab, patch_path)
    text = p.read_text(encoding="utf-8", errors="replace")
    return p, text, _sha256_text(text), _parse_patch(text)


def apply_patch_plan(lab, tid: str, patch_path: str) -> dict:
    policy = _load_policy(lab)
    rec = _load_transaction(lab, policy, tid)
    if rec.get("final_state") not in (None, "active"):
        raise DevTransactionError(f"transaction is not active: {rec.get('final_state')}")
    wt = Path(rec["worktree"])
    if not wt.is_dir():
        raise DevTransactionError(f"worktree is missing for transaction {tid}")
    p, text, sha, info = _read_patch(patch_path, lab)
    if info["is_binary"]:
        raise DevTransactionError("binary patches are not supported by the dev transaction v1")
    if not info["touched"]:
        raise DevTransactionError("patch touches no repository paths")
    max_bytes = int(policy.get("max_patch_bytes", 524288))
    if len(text.encode("utf-8")) > max_bytes:
        raise DevTransactionError(f"patch exceeds max_patch_bytes={max_bytes}")
    for rel in info["touched"]:
        _validate_touched_path(wt, rel, rec["allowed_paths"], rec["denylist"], rec["self_protected_paths"])
    head = _current_head(wt)
    tree_before = _write_tree(wt)
    check = _run_git(["apply", "--check", "--index", "-p1", "--", str(p)], wt)
    stat = _run_git(["apply", "--stat", "-p1", "--", str(p)], wt)
    unexpected = _unexpected_worktree_changes(wt)
    return {
        "can_apply": check.returncode == 0,
        "transaction_id": tid,
        "patch_artifact": str(p),
        "patch_sha256": sha,
        "touched_paths": info["touched"],
        "insertions": info["insertions"],
        "deletions": info["deletions"],
        "current_head_sha": head,
        "current_branch": _current_branch(wt),
        "applied_against_tree_sha": tree_before,
        "apply_check_exit_code": check.returncode,
        "apply_check_stderr": check.stderr.strip() or None,
        "unexpected_changes": unexpected,
        "stat": stat.stdout.strip(),
    }


def apply_patch_execute(lab, tid: str, patch_path: str, expected_patch_sha256: str,
                         expected_head_sha: str | None = None,
                         expected_candidate_tree_sha: str | None = None) -> dict:
    policy = _load_policy(lab)
    rec = _load_transaction(lab, policy, tid)
    wt = Path(rec["worktree"])
    if not wt.is_dir():
        raise DevTransactionError(f"worktree is missing for transaction {tid}")
    p, text, sha, info = _read_patch(patch_path, lab)
    if sha != expected_patch_sha256:
        raise DevTransactionError(
            f"patch content changed after confirmation: expected {expected_patch_sha256}, got {sha}"
        )
    # V4 defect #4: reject pre-existing unexpected working-tree changes so an
    # unrelated change cannot produce an applied-but-unrecorded candidate state.
    unexpected_pre = _unexpected_worktree_changes(wt)
    if unexpected_pre:
        raise DevTransactionError(
            f"refuse apply: pre-existing unexpected working-tree changes: {unexpected_pre}"
        )
    head = _current_head(wt)
    if expected_head_sha and head != expected_head_sha:
        raise DevTransactionError(f"head changed after confirmation: expected {expected_head_sha}, got {head}")
    tree_before = _write_tree(wt)
    if expected_candidate_tree_sha and tree_before != expected_candidate_tree_sha:
        raise DevTransactionError(
            f"candidate tree changed after confirmation: expected {expected_candidate_tree_sha}, got {tree_before}"
        )
    if info["is_binary"]:
        raise DevTransactionError("binary patches are not supported")
    if not info["touched"]:
        raise DevTransactionError("patch touches no repository paths")
    for rel in info["touched"]:
        _validate_touched_path(wt, rel, rec["allowed_paths"], rec["denylist"], rec["self_protected_paths"])
    check = _run_git(["apply", "--check", "--index", "-p1", "--", str(p)], wt)
    if check.returncode != 0:
        raise DevTransactionError(f"git apply --check failed at execute time: {check.stderr.strip()}")
    out = _run_git(["apply", "--index", "-p1", "--", str(p)], wt)
    if out.returncode != 0:
        raise DevTransactionError(f"git apply --index failed: {out.stderr.strip()}")
    staged = _staged_paths(wt)
    for rel in staged:
        _validate_touched_path(wt, rel, rec["allowed_paths"], rec["denylist"], rec["self_protected_paths"])
    unexpected = _unexpected_worktree_changes(wt)
    if unexpected:
        raise DevTransactionError(f"unexpected unstaged/untracked changes after apply: {unexpected}")
    tree_after = _write_tree(wt)
    entry = {
        "patch_artifact": str(p),
        "patch_sha256": sha,
        "applied_at_utc": utc_now(),
        "touched_paths": info["touched"],
        "insertions": info["insertions"],
        "deletions": info["deletions"],
        "applied_against_head_sha": head,
        "applied_against_tree_sha": tree_before,
        "resulting_tree_sha": tree_after,
    }
    rec["patches"].append(entry)
    _save_transaction(lab, policy, tid, rec)
    return {
        "applied": True,
        "transaction_id": tid,
        "patch_sha256": sha,
        "staged_paths": staged,
        "applied_against_head_sha": head,
        "applied_against_tree_sha": tree_before,
        "resulting_tree_sha": tree_after,
    }


# ---------------------------------------------------------------------------
# semantic text edits (trusted engine authors the canonical Git patch)
# ---------------------------------------------------------------------------

def _semantic_spec_sha(payload: dict) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return _sha256_text(text)


def _semantic_preview(rel: str, before: str, after: str, limit: int = 8192) -> str:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
    )
    text = "".join(lines)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... <truncated {len(text) - limit} chars>\n"


def _semantic_context(lab, tid: str, relpath: str) -> tuple[dict, dict, Path, str, Path]:
    policy = _load_policy(lab)
    rec = _load_transaction(lab, policy, tid)
    if rec.get("final_state") not in (None, "active"):
        raise DevTransactionError(f"transaction is not active: {rec.get('final_state')}")
    wt = Path(rec["worktree"])
    if not wt.is_dir():
        raise DevTransactionError(f"worktree is missing for transaction {tid}")
    norm = _normalize_repo_rel(wt, relpath)
    _validate_touched_path(wt, norm, rec["allowed_paths"], rec["denylist"], rec["self_protected_paths"])
    target = wt / norm
    if target.is_symlink():
        raise DevTransactionError(f"semantic edits refuse symlink targets: {norm}")
    if not target.parent.is_dir():
        raise DevTransactionError(
            f"semantic edits require an existing parent directory: {target.parent}"
        )
    unexpected = _unexpected_worktree_changes(wt)
    if unexpected:
        raise DevTransactionError(
            f"refuse semantic edit: pre-existing unexpected working-tree changes: {unexpected}"
        )
    branch = _current_branch(wt)
    if branch != rec["development_branch"]:
        raise DevTransactionError(
            f"current branch {branch!r} is not the development branch {rec['development_branch']!r}"
        )
    return policy, rec, wt, norm, target


def _read_text_target(target: Path, rel: str) -> tuple[bool, str, str | None]:
    if not target.exists():
        return False, "", None
    if not target.is_file():
        raise DevTransactionError(f"semantic edit target is not a regular file: {rel}")
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise DevTransactionError(f"semantic edits support UTF-8 text files only: {rel}") from exc
    return True, text, _sha256_text(text)


def _semantic_size_limit(policy: dict) -> int:
    return int(policy.get("max_semantic_text_bytes", policy.get("max_patch_bytes", 524288)))


def _validate_semantic_text(policy: dict, text: str, label: str) -> int:
    if "\x00" in text:
        raise DevTransactionError(f"{label} contains NUL; semantic edits are text-only")
    size = len(text.encode("utf-8"))
    limit = _semantic_size_limit(policy)
    if size > limit:
        raise DevTransactionError(f"{label} exceeds semantic text limit {limit} bytes")
    return size


def write_plan(lab, tid: str, relpath: str, content: str, mode: str) -> dict:
    policy, _rec, wt, norm, target = _semantic_context(lab, tid, relpath)
    mode = str(mode).strip().lower()
    if mode not in {"create", "replace"}:
        raise DevTransactionError("semantic write mode must be 'create' or 'replace'")
    content_bytes = _validate_semantic_text(policy, content, "content")
    exists, before, target_sha = _read_text_target(target, norm)
    if mode == "create" and exists:
        raise DevTransactionError(f"create refused because target already exists: {norm}")
    if mode == "replace" and not exists:
        raise DevTransactionError(f"replace refused because target does not exist: {norm}")
    head = _current_head(wt)
    branch = _current_branch(wt)
    tree = _write_tree(wt)
    content_sha = _sha256_text(content)
    spec_sha = _semantic_spec_sha({
        "kind": "write", "path": norm, "mode": mode,
        "content_sha256": content_sha, "content_bytes": content_bytes,
    })
    return {
        "can_write": True,
        "transaction_id": tid,
        "path": norm,
        "mode": mode,
        "target_exists": exists,
        "current_target_sha256": target_sha,
        "resulting_target_sha256": content_sha,
        "content_bytes": content_bytes,
        "semantic_spec_sha256": spec_sha,
        "current_head_sha": head,
        "current_branch": branch,
        "applied_against_tree_sha": tree,
        "preview_diff": _semantic_preview(norm, before, content),
    }


def edit_plan(lab, tid: str, relpath: str, old_text: str, new_text: str,
              expected_occurrences: int = 1) -> dict:
    policy, _rec, wt, norm, target = _semantic_context(lab, tid, relpath)
    if not old_text:
        raise DevTransactionError("old_text must be non-empty")
    if int(expected_occurrences) < 1:
        raise DevTransactionError("expected_occurrences must be >= 1")
    _validate_semantic_text(policy, old_text, "old_text")
    _validate_semantic_text(policy, new_text, "new_text")
    exists, before, target_sha = _read_text_target(target, norm)
    if not exists:
        raise DevTransactionError(f"edit target does not exist: {norm}")
    actual = before.count(old_text)
    can_edit = actual == int(expected_occurrences)
    after = before.replace(old_text, new_text) if can_edit else before
    result_bytes = _validate_semantic_text(policy, after, "resulting content")
    head = _current_head(wt)
    branch = _current_branch(wt)
    tree = _write_tree(wt)
    spec_sha = _semantic_spec_sha({
        "kind": "edit", "path": norm,
        "old_text_sha256": _sha256_text(old_text),
        "new_text_sha256": _sha256_text(new_text),
        "expected_occurrences": int(expected_occurrences),
    })
    return {
        "can_edit": can_edit,
        "transaction_id": tid,
        "path": norm,
        "expected_occurrences": int(expected_occurrences),
        "actual_occurrences": actual,
        "current_target_sha256": target_sha,
        "resulting_target_sha256": _sha256_text(after),
        "resulting_bytes": result_bytes,
        "semantic_spec_sha256": spec_sha,
        "current_head_sha": head,
        "current_branch": branch,
        "applied_against_tree_sha": tree,
        "preview_diff": _semantic_preview(norm, before, after) if can_edit else "",
        "issue": None if can_edit else (
            f"expected {int(expected_occurrences)} exact occurrence(s), found {actual}"
        ),
    }


def _semantic_patch_artifact(lab, policy: dict, tid: str, sequence: int, patch_text: str) -> tuple[str, str]:
    base = _transactions_root_path(lab, policy) / tid / "semantic_edits"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"edit_{sequence:04d}.patch"
    path.write_text(patch_text, encoding="utf-8")
    return str(path), _sha256_text(patch_text)


def _rollback_semantic_change(wt: Path, target: Path, existed: bool, old_text: str,
                              tree_before: str) -> None:
    if existed:
        target.write_text(old_text, encoding="utf-8")
    elif target.exists():
        target.unlink()
    reset = _run_git(["read-tree", tree_before], wt)
    if reset.returncode != 0:
        raise DevTransactionError(
            f"semantic edit rollback could not restore candidate index: {reset.stderr.strip()}"
        )


def _apply_semantic_text(lab, tid: str, kind: str, norm: str, new_text: str,
                         spec_sha: str, metadata: dict,
                         expected_head_sha: str | None,
                         expected_candidate_tree_sha: str | None,
                         expected_branch: str | None,
                         expected_target_sha256: str | None) -> dict:
    policy, rec, wt, norm2, target = _semantic_context(lab, tid, norm)
    if norm2 != norm:
        raise DevTransactionError("normalized semantic path changed unexpectedly")
    existed, old_text, target_sha = _read_text_target(target, norm)
    if expected_target_sha256 != target_sha:
        raise DevTransactionError(
            f"target content changed after confirmation: expected {expected_target_sha256}, got {target_sha}"
        )
    head = _current_head(wt)
    branch = _current_branch(wt)
    tree_before = _write_tree(wt)
    if expected_head_sha and head != expected_head_sha:
        raise DevTransactionError(
            f"head changed after confirmation: expected {expected_head_sha}, got {head}"
        )
    if expected_branch and branch != expected_branch:
        raise DevTransactionError(
            f"branch changed after confirmation: expected {expected_branch}, got {branch}"
        )
    if expected_candidate_tree_sha and tree_before != expected_candidate_tree_sha:
        raise DevTransactionError(
            f"candidate tree changed after confirmation: expected {expected_candidate_tree_sha}, got {tree_before}"
        )

    artifact_path: Path | None = None
    try:
        target.write_text(new_text, encoding="utf-8")
        add = _run_git(["add", "--", norm], wt)
        if add.returncode != 0:
            raise DevTransactionError(f"git add failed for semantic edit: {add.stderr.strip()}")
        staged = _staged_paths(wt)
        for rel in staged:
            _validate_touched_path(
                wt, rel, rec["allowed_paths"], rec["denylist"], rec["self_protected_paths"]
            )
        unexpected = _unexpected_worktree_changes(wt)
        if unexpected:
            raise DevTransactionError(
                f"unexpected unstaged/untracked changes after semantic edit: {unexpected}"
            )
        tree_after = _write_tree(wt)
        canonical = _run_git(
            ["diff", "--no-ext-diff", tree_before, tree_after, "--", norm], wt
        )
        if canonical.returncode != 0:
            raise DevTransactionError(
                f"could not generate canonical semantic patch: {canonical.stderr.strip()}"
            )
        patch_text = canonical.stdout
        if not patch_text:
            raise DevTransactionError("semantic edit produced no candidate-tree change")
        artifact, patch_sha = _semantic_patch_artifact(
            lab, policy, tid, len(rec["patches"]) + 1, patch_text
        )
        artifact_path = Path(artifact)
        info = _parse_patch(patch_text)
        entry = {
            "kind": kind,
            "semantic_spec_sha256": spec_sha,
            "patch_artifact": artifact,
            "patch_sha256": patch_sha,
            "applied_at_utc": utc_now(),
            "touched_paths": [norm],
            "insertions": info["insertions"],
            "deletions": info["deletions"],
            "applied_against_head_sha": head,
            "applied_against_tree_sha": tree_before,
            "resulting_tree_sha": tree_after,
            **metadata,
        }
        rec["patches"].append(entry)
        _save_transaction(lab, policy, tid, rec)
    except Exception:
        try:
            _rollback_semantic_change(wt, target, existed, old_text, tree_before)
        finally:
            if artifact_path is not None:
                artifact_path.unlink(missing_ok=True)
        raise

    return {
        "applied": True,
        "kind": kind,
        "transaction_id": tid,
        "path": norm,
        "semantic_spec_sha256": spec_sha,
        "canonical_patch_artifact": str(artifact_path),
        "canonical_patch_sha256": patch_sha,
        "staged_paths": staged,
        "applied_against_head_sha": head,
        "applied_against_tree_sha": tree_before,
        "resulting_tree_sha": tree_after,
    }


def write_execute(lab, tid: str, relpath: str, content: str, mode: str,
                  expected_semantic_spec_sha256: str,
                  expected_head_sha: str | None = None,
                  expected_candidate_tree_sha: str | None = None,
                  expected_branch: str | None = None,
                  expected_target_sha256: str | None = None) -> dict:
    plan = write_plan(lab, tid, relpath, content, mode)
    if plan["semantic_spec_sha256"] != expected_semantic_spec_sha256:
        raise DevTransactionError(
            "semantic write specification changed after confirmation: "
            f"expected {expected_semantic_spec_sha256}, got {plan['semantic_spec_sha256']}"
        )
    return _apply_semantic_text(
        lab, tid, "semantic_write", plan["path"], content, plan["semantic_spec_sha256"],
        {"mode": plan["mode"], "resulting_target_sha256": plan["resulting_target_sha256"]},
        expected_head_sha, expected_candidate_tree_sha, expected_branch,
        expected_target_sha256,
    )


def edit_execute(lab, tid: str, relpath: str, old_text: str, new_text: str,
                 expected_occurrences: int, expected_semantic_spec_sha256: str,
                 expected_head_sha: str | None = None,
                 expected_candidate_tree_sha: str | None = None,
                 expected_branch: str | None = None,
                 expected_target_sha256: str | None = None) -> dict:
    plan = edit_plan(lab, tid, relpath, old_text, new_text, expected_occurrences)
    if plan["semantic_spec_sha256"] != expected_semantic_spec_sha256:
        raise DevTransactionError(
            "semantic edit specification changed after confirmation: "
            f"expected {expected_semantic_spec_sha256}, got {plan['semantic_spec_sha256']}"
        )
    if not plan["can_edit"]:
        raise DevTransactionError(plan["issue"] or "semantic edit precondition failed")
    _policy, _rec, _wt, norm, target = _semantic_context(lab, tid, plan["path"])
    _exists, before, _sha = _read_text_target(target, norm)
    after = before.replace(old_text, new_text)
    return _apply_semantic_text(
        lab, tid, "semantic_edit", norm, after, plan["semantic_spec_sha256"],
        {
            "expected_occurrences": int(expected_occurrences),
            "resulting_target_sha256": plan["resulting_target_sha256"],
        },
        expected_head_sha, expected_candidate_tree_sha, expected_branch,
        expected_target_sha256,
    )


def diff(lab, tid: str) -> dict:
    policy = _load_policy(lab)
    rec = _load_transaction(lab, policy, tid)
    wt = Path(rec["worktree"])
    if not wt.is_dir():
        raise DevTransactionError(f"worktree is missing for transaction {tid}")
    stat = _run_git(["diff", "--cached", "--stat"], wt)
    names = _staged_paths(wt)
    full = _run_git(["diff", "--cached"], wt)
    return {
        "transaction_id": tid,
        "baseline_sha": rec["baseline_sha"],
        "current_head_sha": _current_head(wt),
        "candidate_tree_sha": _write_tree(wt),
        "staged_files": names,
        "unexpected_changes": _unexpected_worktree_changes(wt),
        "stat": stat.stdout.strip(),
        "diff": full.stdout,
    }


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

_TEST_PATH_RE = re.compile(r"^tests/[A-Za-z0-9_./-]+\.py$")
_NODE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALLOWED_PROFILES = {"diff_check", "targeted_pytest", "full_pytest", "lab_doctor", "lab_config_validate"}


def _bounded(text: str, limit: int = 8192) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-(limit // 2):]
    return head + "\n... [truncated; full log persisted] ...\n" + tail


def _persist_log(lab, policy, tid: str, profile: str, text: str) -> str:
    base = _transactions_root_path(lab, policy) / tid
    base.mkdir(parents=True, exist_ok=True)
    name = f"test_{profile}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.log"
    p = base / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _sanitized_env(wt: Path) -> dict:
    keep = {"PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT",
            "TEMP", "TMP", "TMPDIR", "NUMBER_OF_PROCESSORS", "OS"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["PYTHONPATH"] = str(wt)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["LC_ALL"] = env.get("LC_ALL", "C")
    env["PYTHONNOUSERSITE"] = "1"
    env.pop("PYTHONHOME", None)
    return env


def _normalize_test_targets(wt: Path, test_files: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in test_files or []:
        file_part, sep, node = raw.partition("::")
        norm = _normalize_repo_rel(wt, file_part)
        if not _TEST_PATH_RE.match(norm):
            raise DevTransactionError(f"targeted test path is not allowed: {raw!r}")
        if sep:
            if not node or not _NODE_RE.match(node):
                raise DevTransactionError(f"invalid test node selector: {raw!r}")
            out.append(f"{norm}::{node}")
        else:
            out.append(norm)
    return out


def _build_test_cmd(wt: Path, profile: str, norm_files: list[str], require_nrg: bool) -> list[str]:
    if profile == "diff_check":
        return ["git", "--no-pager", "diff", "--cached", "--check"]
    if profile == "targeted_pytest":
        return [sys.executable, "-m", "pytest", *norm_files, "-q"]
    if profile == "full_pytest":
        return [sys.executable, "-m", "pytest", "-q"]
    if profile == "lab_doctor":
        cmd = [sys.executable, "-m", "nrg_analysis.doctor",
               "--laboratory", str(wt / "config" / "laboratory.toml"), "--no-local"]
        if require_nrg:
            cmd.append("--require-nrg")
        return cmd
    if profile == "lab_config_validate":
        return [sys.executable, "-m", "nrg_analysis.config_cli",
                "--laboratory", str(wt / "config" / "laboratory.toml"), "validate", "--no-runtime"]
    raise DevTransactionError(f"profile not implemented: {profile}")


def test_plan(lab, tid: str, profile: str, test_files: list[str] | None = None,
              require_nrg: bool = False) -> dict:
    """Read-only test preflight.

    V4 defect #4: returns ``can_run=false`` if unexpected unstaged/untracked
    source changes exist, because pytest executes files from the working tree
    while ``candidate_tree_sha`` represents the index.  A non-empty
    ``unexpected_changes`` means the working tree does not correspond to the
    candidate index, so a test result would not be valid for that tree.
    V4 defect #5: reports ``current_head_sha`` and ``current_branch`` alongside
    ``candidate_tree_sha`` so execute can verify provenance preservation.
    """
    policy = _load_policy(lab)
    if profile not in _ALLOWED_PROFILES:
        raise DevTransactionError(f"unknown test profile: {profile!r}")
    if profile not in set(policy.get("allowed_test_profiles", [])):
        raise DevTransactionError(f"profile {profile!r} is not permitted by policy")
    rec = _load_transaction(lab, policy, tid)
    wt = Path(rec["worktree"])
    if not wt.is_dir():
        raise DevTransactionError(f"worktree is missing for transaction {tid}")
    norm_files: list[str] = []
    if profile == "targeted_pytest":
        norm_files = _normalize_test_targets(wt, test_files)
        if not norm_files:
            raise DevTransactionError("targeted_pytest requires at least one tests/*.py path")
    cmd = _build_test_cmd(wt, profile, norm_files, bool(require_nrg))
    unexpected = _unexpected_worktree_changes(wt)
    return {
        "can_run": len(unexpected) == 0,
        "transaction_id": tid,
        "profile": profile,
        "normalized_command": cmd,
        "test_targets": norm_files,
        "require_nrg": bool(require_nrg),
        "current_head_sha": _current_head(wt),
        "current_branch": _current_branch(wt),
        "candidate_tree_sha": _write_tree(wt),
        "unexpected_changes": unexpected,
    }


def _classify_test(profile: str, require_nrg: bool, returncode: int, stdout: str) -> str:
    if returncode != 0:
        return "fail"
    if profile in ("lab_doctor", "lab_config_validate"):
        try:
            payload = json.loads((stdout or "").strip())
        except Exception:
            return "fail"
        if profile == "lab_doctor" and require_nrg:
            if payload.get("summary", {}).get("nrg_runtime_ready") is False:
                return "environment_blocked"
        if payload.get("error"):
            return "fail"
    return "pass"


def test_execute(lab, tid: str, profile: str, test_files: list[str] | None = None,
                 require_nrg: bool = False,
                 expected_candidate_tree_sha: str | None = None,
                 expected_head_sha: str | None = None,
                 expected_branch: str | None = None) -> dict:
    """Execute a trusted test profile in the isolated worktree.

    V4 defect #4: rejects if unexpected unstaged/untracked source changes exist
    before launch (working tree ≠ candidate index).
    V4 defect #5: verifies ``expected_head_sha`` and ``expected_branch`` to
    prevent stale-HEAD test execution.
    """
    policy = _load_policy(lab)
    rec = _load_transaction(lab, policy, tid)
    wt = Path(rec["worktree"])
    if not wt.is_dir():
        raise DevTransactionError(f"worktree is missing for transaction {tid}")
    if profile not in _ALLOWED_PROFILES:
        raise DevTransactionError(f"unknown test profile: {profile!r}")
    if profile not in set(policy.get("allowed_test_profiles", [])):
        raise DevTransactionError(f"profile {profile!r} is not permitted by policy")
    norm_files: list[str] = []
    if profile == "targeted_pytest":
        norm_files = _normalize_test_targets(wt, test_files)
        if not norm_files:
            raise DevTransactionError("targeted_pytest requires at least one tests/*.py path")
    cmd = _build_test_cmd(wt, profile, norm_files, bool(require_nrg))

    head_before = _current_head(wt)
    branch_before = _current_branch(wt)
    candidate_before = _write_tree(wt)
    unexpected_before = _unexpected_worktree_changes(wt)

    # V4 defect #4: refuse test if working tree does not match candidate index
    if unexpected_before:
        raise DevTransactionError(
            f"refuse test: working tree does not match candidate index; "
            f"unexpected changes: {unexpected_before}"
        )
    # V4 defect #5: verify confirmed provenance fields
    if expected_head_sha and head_before != expected_head_sha:
        raise DevTransactionError(
            f"stale HEAD before test: expected {expected_head_sha}, got {head_before}"
        )
    if expected_branch and branch_before != expected_branch:
        raise DevTransactionError(
            f"branch changed before test: expected {expected_branch}, got {branch_before}"
        )
    if expected_candidate_tree_sha and candidate_before != expected_candidate_tree_sha:
        raise DevTransactionError(
            f"candidate tree changed after confirmation: expected {expected_candidate_tree_sha}, got {candidate_before}"
        )

    env = _sanitized_env(wt) if profile != "diff_check" else os.environ.copy()
    started = time.monotonic()
    proc = subprocess.run(cmd, cwd=str(wt), text=True, capture_output=True,
                          timeout=1800, env=env, shell=False)
    duration_ms = int((time.monotonic() - started) * 1000)
    log = (proc.stdout or "") + ("\n--- stderr ---\n" if proc.stderr else "") + (proc.stderr or "")
    log_path = _persist_log(lab, policy, tid, profile, log)
    head_after = _current_head(wt)
    branch_after = _current_branch(wt)
    candidate_after = _write_tree(wt)
    unexpected_after = _unexpected_worktree_changes(wt)
    new_unexpected = [u for u in unexpected_after if u not in unexpected_before]
    integrity_ok = (head_after == head_before and branch_after == branch_before
                    and candidate_after == candidate_before and not new_unexpected)
    base = _classify_test(profile, bool(require_nrg), proc.returncode, proc.stdout or "")
    if not integrity_ok:
        classification = "integrity_failed"
    else:
        classification = base
    result = {
        "profile": profile,
        "normalized_command": cmd,
        "exit_code": proc.returncode,
        "duration_ms": duration_ms,
        "classification": classification,
        "log_path": log_path,
        "head_sha": head_before,
        "branch": branch_before,
        "candidate_tree_sha": candidate_before,
        "integrity_ok": integrity_ok,
        "integrity_changes": {
            "head_changed": head_after != head_before,
            "branch_changed": branch_after != branch_before,
            "candidate_tree_changed": candidate_after != candidate_before,
            "new_unexpected_changes": new_unexpected,
        },
        "ran_at_utc": utc_now(),
        "stdout": _bounded(proc.stdout or ""),
        "stderr": _bounded(proc.stderr or ""),
    }
    rec["test_results"].append({k: v for k, v in result.items() if k not in ("stdout", "stderr")})
    _save_transaction(lab, policy, tid, rec)
    return result


# ---------------------------------------------------------------------------
# commit (requires current-tree validation; commits the staged candidate tree)
# ---------------------------------------------------------------------------

def _current_tree_validation(rec: dict, candidate_tree: str) -> dict:
    cur = [r for r in rec["test_results"] if r.get("candidate_tree_sha") == candidate_tree]
    diff_check_pass = any(r["profile"] == "diff_check" and r["classification"] == "pass" for r in cur)
    pytest_pass = any(r["profile"] in ("targeted_pytest", "full_pytest") and r["classification"] == "pass" for r in cur)
    blocking = any(r["classification"] in ("fail", "integrity_failed") for r in cur)
    return {
        "diff_check_pass": diff_check_pass,
        "pytest_pass": pytest_pass,
        "current_blocking_result": blocking,
        "current_results": cur,
    }


def commit_plan(lab, tid: str, message: str) -> dict:
    policy = _load_policy(lab)
    rec = _load_transaction(lab, policy, tid)
    wt = Path(rec["worktree"])
    if not wt.is_dir():
        raise DevTransactionError(f"worktree is missing for transaction {tid}")
    branch = _current_branch(wt)
    if branch != rec["development_branch"]:
        raise DevTransactionError(
            f"current branch {branch!r} is not the development branch {rec['development_branch']!r}"
        )
    if not message.strip():
        raise DevTransactionError("commit message must be non-empty")
    staged = _staged_paths(wt)
    if not staged:
        raise DevTransactionError("no staged candidate changes to commit")
    for rel in staged:
        _validate_touched_path(wt, rel, rec["allowed_paths"], rec["denylist"], rec["self_protected_paths"])
    unexpected = _unexpected_worktree_changes(wt)
    candidate_tree = _write_tree(wt)
    head = _current_head(wt)
    val = _current_tree_validation(rec, candidate_tree)
    validation_ok = (val["diff_check_pass"] and val["pytest_pass"] and not val["current_blocking_result"])
    stat = _run_git(["diff", "--cached", "--stat"], wt)
    return {
        "can_commit": (not unexpected) and validation_ok,
        "validation_ok": validation_ok,
        "diff_check_pass": val["diff_check_pass"],
        "pytest_pass": val["pytest_pass"],
        "current_blocking_result": val["current_blocking_result"],
        "transaction_id": tid,
        "current_head_sha": head,
        "candidate_tree_sha": candidate_tree,
        "current_branch": branch,
        "staged_paths": staged,
        "unexpected_changes": unexpected,
        "message": message,
        "diff_stat": stat.stdout.strip(),
        "current_tree_results": val["current_results"],
    }


def commit_execute(lab, tid: str, message: str,
                   expected_candidate_tree_sha: str | None = None,
                   expected_head_sha: str | None = None,
                   expected_branch: str | None = None) -> dict:
    """Commit the validated staged candidate on ``agent-dev/<tid>`` only.

    V4 defect #5: verifies ``expected_head_sha`` and ``expected_branch`` so a
    new commit with the same tree between plan and execute is rejected as stale
    rather than silently changing the parent/provenance of the confirmed commit.
    """
    policy = _load_policy(lab)
    plan = commit_plan(lab, tid, message)
    if expected_candidate_tree_sha and plan["candidate_tree_sha"] != expected_candidate_tree_sha:
        raise DevTransactionError(
            f"candidate tree changed after confirmation: expected {expected_candidate_tree_sha}, got {plan['candidate_tree_sha']}"
        )
    if expected_head_sha and plan["current_head_sha"] != expected_head_sha:
        raise DevTransactionError(
            f"stale HEAD before commit: expected {expected_head_sha}, got {plan['current_head_sha']}; "
            "a new commit was made after plan confirmation"
        )
    if expected_branch and plan["current_branch"] != expected_branch:
        raise DevTransactionError(
            f"branch changed before commit: expected {expected_branch}, got {plan['current_branch']}"
        )
    if not plan["can_commit"]:
        return plan
    rec = _load_transaction(lab, policy, tid)
    wt = Path(rec["worktree"])
    commit = _run_git(["commit", "-m", message], wt)
    if commit.returncode != 0:
        raise DevTransactionError(f"git commit failed: {commit.stderr.strip()}")
    new_sha = _current_head(wt)
    rec["development_commit_sha"] = new_sha
    rec["final_state"] = "committed"
    _save_transaction(lab, policy, tid, rec)
    return {**plan, "committed": True, "development_commit_sha": new_sha}


# ---------------------------------------------------------------------------
# discard (bound to the exact confirmed state)
# ---------------------------------------------------------------------------

def discard_plan(lab, tid: str) -> dict:
    """Report the exact state being discarded.

    V4 defect #6: reports ``current_head_sha``, ``candidate_tree_sha``,
    ``development_branch``, ``staged_paths``, and ``unexpected_changes`` so the
    human confirms the exact state, not a stale snapshot.
    """
    policy = _load_policy(lab)
    rec = _load_transaction(lab, policy, tid)
    wt = Path(rec["worktree"])
    present = wt.is_dir()
    result: dict[str, Any] = {
        "can_discard": True,
        "transaction_id": tid,
        "worktree": str(wt),
        "development_branch": rec["development_branch"],
        "worktree_present": present,
        "staged_paths": [],
        "unexpected_changes": [],
        "current_head_sha": None,
        "candidate_tree_sha": None,
        "current_branch": None,
    }
    if present:
        result["current_head_sha"] = _current_head(wt)
        result["candidate_tree_sha"] = _write_tree(wt)
        result["current_branch"] = _current_branch(wt)
        result["staged_paths"] = _staged_paths(wt)
        result["unexpected_changes"] = _unexpected_worktree_changes(wt)
    return result


def discard_execute(lab, tid: str,
                    expected_head_sha: str | None = None,
                    expected_candidate_tree_sha: str | None = None,
                    expected_branch: str | None = None,
                    expected_staged_paths: list[str] | None = None,
                    expected_unexpected_changes: list[str] | None = None) -> dict:
    """Remove only the isolated worktree and branch.

    V4 defect #6: requires the confirmed state fields and rejects if the
    worktree changed after confirmation.  Verifies return codes from
    ``git worktree remove`` and ``git branch -D``; does not record
    ``final_state="discarded"`` when cleanup was not actually completed.
    """
    policy = _load_policy(lab)
    rec = _load_transaction(lab, policy, tid)
    repo = _repo_root(lab)
    wt_root = _worktree_root_path(lab, policy)
    wt = Path(rec["worktree"])
    canonical = _worktree_dir_path(lab, policy, tid)
    if wt.resolve() != canonical.resolve() or not _inside(wt_root, wt.resolve()) or wt.resolve() == repo.resolve():
        raise DevTransactionError("discard refused: worktree is not the canonical transaction worktree")

    # Verify confirmed state hasn't changed (V4 defect #6)
    if wt.is_dir():
        cur_head = _current_head(wt)
        cur_branch = _current_branch(wt)
        cur_candidate = _write_tree(wt)
        cur_staged = _staged_paths(wt)
        cur_unexpected = _unexpected_worktree_changes(wt)
        if expected_head_sha is not None and cur_head != expected_head_sha:
            raise DevTransactionError(
                f"discard state changed: HEAD expected {expected_head_sha}, got {cur_head}"
            )
        if expected_candidate_tree_sha is not None and cur_candidate != expected_candidate_tree_sha:
            raise DevTransactionError(
                f"discard state changed: candidate tree expected {expected_candidate_tree_sha}, got {cur_candidate}"
            )
        if expected_branch is not None and cur_branch != expected_branch:
            raise DevTransactionError(
                f"discard state changed: branch expected {expected_branch}, got {cur_branch}"
            )
        if expected_staged_paths is not None and cur_staged != expected_staged_paths:
            raise DevTransactionError(
                f"discard state changed: staged paths expected {expected_staged_paths}, got {cur_staged}"
            )
        if expected_unexpected_changes is not None and cur_unexpected != expected_unexpected_changes:
            raise DevTransactionError(
                f"discard state changed: unexpected changes expected {expected_unexpected_changes}, got {cur_unexpected}"
            )

    wt_removed = False
    if wt.is_dir():
        out = _run_git(["worktree", "remove", "--force", str(wt)], repo)
        if out.returncode != 0:
            # only filesystem-delete when the exact safety invariants are proven
            if wt.resolve() == canonical.resolve() and _inside(wt_root, wt.resolve()) and wt != repo:
                shutil.rmtree(wt, ignore_errors=True)
            else:
                raise DevTransactionError(
                    f"git worktree remove failed and safety invariants unproven: {out.stderr.strip()}"
                )
        if wt.exists():
            raise DevTransactionError(f"worktree removal incomplete: {wt} still exists")
        wt_removed = True

    branch_out = _run_git(["branch", "-D", rec["development_branch"]], repo)
    if branch_out.returncode != 0:
        # Branch removal failed — do not report success
        rec["final_state"] = "discard_partial"
        _save_transaction(lab, policy, tid, rec)
        raise DevTransactionError(f"git branch -D failed: {branch_out.stderr.strip()}")

    rec["final_state"] = "discarded"
    _save_transaction(lab, policy, tid, rec)
    return {
        "transaction_id": tid,
        "discarded": True,
        "final_state": "discarded",
        "worktree_removed": wt_removed,
        "branch_removed": True,
    }