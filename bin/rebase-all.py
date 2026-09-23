#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "libtmux>=0.35",
# ]
# ///
# GENERATED FILE -- do not edit directly.
# Source of truth: src/tmuxpull/__init__.py
# Regenerate with: python scripts/gen_script.py
"""
tmuxpull -- keep every Git repo under the given roots in step with its remote,
concurrently, so merge conflicts surface this morning instead of at push time.

The default ("main"/head) branch is the reference point, never whatever happens
to be checked out. Per repo, after a `git fetch --prune`:

* on the default branch        -> `git pull --rebase --autostash`: your unpushed
                                  commits replay onto the new upstream, so a
                                  conflict shows up now.
* on a never-pushed branch     -> `git rebase --autostash <remote>/<default>`:
                                  nothing is published yet, so replaying your
                                  work is free and surfaces conflicts early.
* on a pushed branch           -> detect only. The branch fast-forwards to its
                                  own upstream when it can, and `git merge-tree`
                                  probes it against the new default branch
                                  WITHOUT touching anything -- rewriting pushed
                                  commits would force a `--force-with-lease` on
                                  you. Pass --rebase-pushed to rebase anyway.
* detached HEAD                -> detect only.

The local default branch is also fast-forwarded (a plain ref update, no
checkout) whenever it is not the branch checked out here, so the next branch you
cut is current. When another worktree has it checked out, that worktree's own
run advances it.

A conflicted rebase is left IN PROGRESS: the repo's tmux session is where you
resolve it. Results are written to a report file and the repos needing attention
are printed to stderr before tmux takes over, so the list survives attaching,
detaching, and closing the terminal.

Usage:
    tmuxpull [-d DEPTH] [-j JOBS] [--tmux {on,off}] [-v] [--dry-run] DIR [DIR ...]
"""
from __future__ import annotations

import argparse
import asyncio
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import libtmux


# Single source of truth for the version: pyproject.toml reads it from here
# (hatch dynamic version), so the PyPI package, the generated standalone script
# and `--version` can never disagree.
__version__ = "0.2.0"


# Directories that are never a repo we want to descend into.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "env",
        "target",
        "build",
        "dist",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
    }
)

# tmux session names cannot contain ':' or '.' but allow '/'. 
# Use Unix-style paths since tmux runs in Unix-like environments.
_UNSAFE_TMUX = re.compile(r"[:.\s]")

# Branch names probed when the remote never published a HEAD (a --single-branch
# clone, or an old git that did not write refs/remotes/<remote>/HEAD).
_DEFAULT_BRANCH_CANDIDATES: tuple[str, ...] = ("main", "master", "trunk")

_TRUTHY: frozenset[str] = frozenset({"true", "1", "yes", "on"})

_REFLOG_MSG = "tmuxpull: fast-forward to remote"

# What was done to the repo.
ACT_PULL = "pull"      # rebased the checked-out default branch in place
ACT_REBASE = "rebase"  # replayed the checked-out branch onto <remote>/<default>
ACT_DETECT = "detect"  # probed for conflicts, rewrote nothing
ACT_NONE = "none"

# How it turned out. The ATTENTION_STATES are the ones you have to act on.
STATE_CLEAN = "clean"          # nothing moved
STATE_UPDATED = "updated"      # something fast-forwarded, no replay needed
STATE_REBASED = "rebased"      # your work was replayed onto the default branch
STATE_DIVERGED = "diverged"    # probe says a rebase onto the default would conflict
STATE_UNSYNCED = "unsynced"    # branch and its own upstream have both moved
STATE_CONFLICT = "conflict"    # rebase stopped and is IN PROGRESS in the worktree
STATE_FAIL = "fail"            # git itself failed (network, auth, hook, ...)
STATE_IGNORED = "ignored"      # git config tmuxpull.ignore
STATE_NO_REMOTE = "no-remote"  # nothing to pull from

ATTENTION_STATES: frozenset[str] = frozenset(
    {STATE_DIVERGED, STATE_UNSYNCED, STATE_CONFLICT, STATE_FAIL}
)
# States where opening a tmux session would be pointless.
NO_SESSION_STATES: frozenset[str] = frozenset({STATE_IGNORED, STATE_NO_REMOTE})


def _s(n: int) -> str:
    return "" if n == 1 else "s"


# --------------------------------------------------------------------------- #
# data model                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Options:
    """Run-wide switches that change what git is allowed to do."""

    rebase_pushed: bool = False


@dataclass(slots=True)
class Repo:
    path: Path  # absolute filesystem path
    name: str   # display + tmux window name (posix-style relative path)


@dataclass(slots=True)
class Result:
    repo: Repo
    state: str = STATE_CLEAN
    action: str = ACT_NONE

    branch: str = ""          # branch checked out here ("" = detached HEAD)
    default_branch: str = ""  # the repo's "main"/head branch
    remote: str = ""          # remote it is tracked from
    upstream: str = ""        # upstream of the checked-out branch, if any

    incoming: int = 0         # commits <remote>/<default> gained in this run
    replayed: int = 0         # commits of YOUR work replayed onto the default
    main_ff: int = 0          # commits the local default branch fast-forwarded
    branch_ff: int = 0        # commits the checked-out branch gained from upstream
    main_created: bool = False    # local default branch did not exist, now does
    main_diverged: bool = False   # local default branch cannot fast-forward

    shortstat: str = ""                                    # of the incoming commits
    log_lines: list[str] = field(default_factory=list)     # ditto, --oneline
    conflict_files: list[str] = field(default_factory=list)

    returncode: int = 0
    stderr: str = ""
    session: str = ""  # tmux session name, filled in once created

    @property
    def ok(self) -> bool:
        return self.state != STATE_FAIL

    @property
    def needs_attention(self) -> bool:
        return self.state in ATTENTION_STATES

    @property
    def wants_session(self) -> bool:
        return self.state not in NO_SESSION_STATES

    @property
    def target(self) -> str:
        """The branch everything is measured against."""
        return self.default_branch or "HEAD"

    def _movement(self) -> list[str]:
        """Fragments describing what refs moved, in report order."""
        parts: list[str] = []
        if self.replayed:
            parts.append(f"rebased {self.replayed} onto {self.target}")
        if self.action == ACT_PULL:
            if self.incoming:
                n = self.incoming
                # after a replay, "1 commit" alone would be ambiguous
                parts.append(
                    f"pulled {n} commit{_s(n)}" if self.replayed else f"{n} commit{_s(n)}"
                )
        elif self.main_ff:
            parts.append(f"{self.target} +{self.main_ff}")
        elif self.main_created:
            parts.append(f"{self.target} created")
        elif self.incoming:
            parts.append(f"{self.remote}/{self.target} +{self.incoming}")
        if self.main_diverged:
            parts.append(f"{self.target} diverged locally")
        if self.branch_ff:
            parts.append(f"{self.branch} +{self.branch_ff}")
        return parts

    def summary_line(self) -> str:
        if self.state == STATE_IGNORED:
            return "- ignored (git config tmuxpull.ignore)"
        if self.state == STATE_NO_REMOTE:
            return "- no remote"
        if self.state == STATE_FAIL:
            tail = (self.stderr.strip().splitlines() or [f"exit {self.returncode}"])[-1]
            return f"! FAIL: {tail}"

        if self.state in (STATE_CONFLICT, STATE_DIVERGED, STATE_UNSYNCED):
            n = len(self.conflict_files)
            if self.state == STATE_CONFLICT:
                head = f"! CONFLICT: rebase stopped, {n} file{_s(n)} -- resolve here"
            elif self.state == STATE_DIVERGED:
                head = f"! DIVERGES from {self.target}: {n} file{_s(n)} would conflict"
            else:
                head = f"! UNSYNCED: {self.branch} and {self.upstream} both moved"
            moved = [p for p in self._movement() if not p.startswith("rebased ")]
            return f"{head} ({', '.join(moved)})" if moved else head

        parts = self._movement()
        if not parts:
            return "= up to date"
        lead = "~" if self.replayed else "+"
        return f"{lead} {', '.join(parts)}  {self.shortstat}".rstrip()


# --------------------------------------------------------------------------- #
# repo discovery                                                              #
# --------------------------------------------------------------------------- #


def find_repos(roots: Iterable[str], max_depth: int) -> list[Repo]:
    """Walk each root looking for directories containing a .git entry.

    Prunes noise directories (see _SKIP_DIRS), enforces a max depth relative
    to each root, and never descends into a repo (so nested submodules are
    ignored -- typically what you want for a "pull everything" script).
    """
    out: list[Repo] = []
    for root in roots:
        top = Path(root).expanduser().resolve()
        if not top.is_dir():
            print(f"skip: {top} is not a directory", file=sys.stderr)
            continue
        base = len(top.parts)
        for dirpath, dirs, _ in os.walk(top):
            here = Path(dirpath)
            depth = len(here.parts) - base
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            if depth > max_depth:
                dirs[:] = []
                continue
            if (here / ".git").exists():
                dirs[:] = []
                name = "." if here == top else here.relative_to(top).as_posix()
                out.append(Repo(path=here, name=name))

    # dedupe (overlapping roots)
    seen: set[Path] = set()
    uniq: list[Repo] = []
    for r in out:
        if r.path in seen:
            continue
        seen.add(r.path)
        uniq.append(r)
    return uniq


def apply_excludes(repos: list[Repo], patterns: list[str]) -> tuple[list[Repo], list[Repo]]:
    """Split repos into (kept, excluded) by fnmatch of display name against patterns.

    Patterns match the repo's display name (e.g. 'kirodotdev/KiroCrew'), so
    both exact names ('-x kirodotdev/KiroCrew') and globs ('-x "kirodotdev/*"')
    work.
    """
    if not patterns:
        return repos, []
    kept: list[Repo] = []
    excluded: list[Repo] = []
    for r in repos:
        if any(fnmatch.fnmatch(r.name, p) for p in patterns):
            excluded.append(r)
        else:
            kept.append(r)
    return kept, excluded


# --------------------------------------------------------------------------- #
# git plumbing                                                                #
# --------------------------------------------------------------------------- #


async def _git(repo: Path, *args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def _config(repo: Path, key: str) -> str:
    rc, out, _ = await _git(repo, "config", "--get", key)
    return out.strip() if rc == 0 else ""


async def _rev(repo: Path, ref: str) -> str:
    """Resolve ref to a sha, or "" when it does not exist."""
    if not ref:
        return ""
    rc, out, _ = await _git(repo, "rev-parse", "--verify", "--quiet", ref)
    return out.strip() if rc == 0 else ""


async def _count(repo: Path, rng: str) -> int:
    rc, out, _ = await _git(repo, "rev-list", "--count", rng)
    try:
        return int(out.strip()) if rc == 0 else 0
    except ValueError:
        return 0


async def _is_ancestor(repo: Path, maybe_ancestor: str, descendant: str) -> bool:
    rc, _, _ = await _git(repo, "merge-base", "--is-ancestor", maybe_ancestor, descendant)
    return rc == 0


async def common_dir(repo: Path) -> str:
    """The shared .git dir of this repo AND all its worktrees.

    Worktrees have their own working tree but ONE object store and ONE ref
    namespace, so jobs that share a common dir must not run concurrently --
    they would race on ref locks and on FETCH_HEAD.
    """
    rc, out, _ = await _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if rc == 0 and out.strip():
        return out.strip()
    return str(repo)


async def pick_remote(repo: Path) -> str:
    """The remote to pull from: 'origin' when present, else the first one."""
    rc, out, _ = await _git(repo, "remote")
    if rc != 0:
        return ""
    remotes = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not remotes:
        return ""
    return "origin" if "origin" in remotes else remotes[0]


async def current_branch(repo: Path) -> str:
    """Checked-out branch name, or "" on a detached HEAD."""
    rc, out, _ = await _git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    return out.strip() if rc == 0 else ""


async def upstream_of(repo: Path) -> str:
    """The checked-out branch's upstream (e.g. 'origin/feat/x'), or ""."""
    rc, out, _ = await _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    return out.strip() if rc == 0 else ""


async def default_branch(repo: Path, remote: str) -> str:
    """Resolve the repo's default ("main"/head) branch name.

    In order: a `tmuxpull.defaultBranch` override, the remote's published HEAD
    (refs/remotes/<remote>/HEAD, written at clone time), then a probe of the
    usual names. Returns "" when none of those answer, in which case the caller
    falls back to pulling whatever is checked out.
    """
    override = await _config(repo, "tmuxpull.defaultBranch")
    if override:
        return override

    rc, out, _ = await _git(repo, "symbolic-ref", "--short", "-q", f"refs/remotes/{remote}/HEAD")
    head = out.strip()
    prefix = f"{remote}/"
    if rc == 0 and head.startswith(prefix):
        return head[len(prefix) :]

    for cand in _DEFAULT_BRANCH_CANDIDATES:
        if await _rev(repo, f"refs/remotes/{remote}/{cand}"):
            return cand
    return ""


async def branch_is_checked_out(repo: Path, branch: str) -> bool:
    """True if <branch> is checked out in any worktree of this repo.

    Callers only ask about a branch they are NOT on, so a hit always means
    "another worktree owns it" -- and its ref must be left alone, or that
    worktree's index and HEAD would silently disagree.
    """
    rc, out, _ = await _git(repo, "worktree", "list", "--porcelain")
    if rc != 0:
        return False
    wanted = f"branch refs/heads/{branch}"
    return any(ln.strip() == wanted for ln in out.splitlines())


async def rebase_in_progress(repo: Path) -> bool:
    """True when a rebase is stopped mid-way in this worktree."""
    for name in ("rebase-merge", "rebase-apply"):
        rc, out, _ = await _git(repo, "rev-parse", "--git-path", name)
        if rc != 0 or not out.strip():
            continue
        p = Path(out.strip())
        if not p.is_absolute():
            p = repo / p
        if p.exists():
            return True
    return False


async def unmerged_files(repo: Path) -> list[str]:
    rc, out, _ = await _git(repo, "diff", "--name-only", "--diff-filter=U")
    return [ln.strip() for ln in out.splitlines() if ln.strip()] if rc == 0 else []


async def probe_conflicts(repo: Path, base: str, tip: str) -> tuple[bool, list[str]]:
    """Ask git whether merging <base> into <tip> would conflict. Read-only.

    `git merge-tree` does the whole thing in memory: no checkout, no index, no
    worktree, nothing to clean up. It models a MERGE, so a clean answer is a
    strong signal rather than a guarantee that replaying every commit is clean.

    Returns (probe_ran, conflicted_paths).
    """
    rc, out, _ = await _git(repo, "merge-tree", "--write-tree", "--name-only", base, tip)
    if rc == 0:
        return True, []
    if rc != 1:
        # unrelated histories, or a git too old for --write-tree
        return False, []
    files: list[str] = []
    for ln in out.splitlines()[1:]:  # first line is the written tree's oid
        if not ln.strip():
            break  # informational messages follow the blank line
        files.append(ln.strip())
    return True, files


# --------------------------------------------------------------------------- #
# per-repo work                                                               #
# --------------------------------------------------------------------------- #


async def _record_incoming(repo: Path, r: Result, before: str, after: str) -> None:
    """Describe what arrived on <remote>/<default> during the fetch."""
    if not (before and after and before != after):
        return
    rng = f"{before}..{after}"
    r.incoming = await _count(repo, rng)
    _, log_out, _ = await _git(repo, "log", "--oneline", "--no-decorate", rng)
    r.log_lines = [ln for ln in log_out.splitlines() if ln]
    _, ss, _ = await _git(repo, "diff", "--shortstat", rng)
    r.shortstat = ss.strip()


async def _fast_forward_default(repo: Path, r: Result) -> None:
    """Move the local default branch up to the remote WITHOUT a checkout.

    Only ever a fast-forward, guarded on the old value, so it cannot rewrite
    anything and cannot race another job. Skipped when another worktree has the
    branch checked out (that worktree's own run advances it).
    """
    tip = await _rev(repo, f"refs/remotes/{r.remote}/{r.default_branch}")
    if not tip or await branch_is_checked_out(repo, r.default_branch):
        return

    ref = f"refs/heads/{r.default_branch}"
    local = await _rev(repo, ref)
    if not local:
        # create it with tracking, so the next `git checkout main` is normal
        rc, _, _ = await _git(
            repo, "branch", "--track", r.default_branch, f"{r.remote}/{r.default_branch}"
        )
        r.main_created = rc == 0
        return
    if local == tip:
        return
    if not await _is_ancestor(repo, local, tip):
        r.main_diverged = True
        return
    n = await _count(repo, f"{local}..{tip}")
    rc, _, _ = await _git(repo, "update-ref", "-m", _REFLOG_MSG, ref, tip, local)
    if rc == 0:
        r.main_ff = n


async def _align_with_upstream(repo: Path, r: Result) -> bool:
    """Fast-forward the checked-out branch to its own upstream.

    Picks up what you pushed from another machine before anything is measured
    against the default branch. Returns False when the branch and its upstream
    have BOTH moved -- that is yours to reconcile, and no rebase should touch it
    (replaying only the local side would orphan the pushed commits).
    """
    if not r.upstream:
        return True
    up = await _rev(repo, r.upstream)
    head = await _rev(repo, "HEAD")
    if not up or not head or up == head:
        return True
    if await _is_ancestor(repo, up, head):
        return True  # merely ahead: normal unpushed work
    if not await _is_ancestor(repo, head, up):
        r.state = STATE_UNSYNCED
        return False
    n = await _count(repo, f"{head}..{up}")
    rc, out, err = await _git(repo, "merge", "--ff-only", r.upstream)
    if rc != 0:
        r.state, r.returncode, r.stderr = STATE_FAIL, rc, err or out
        return False
    r.branch_ff = n
    return True


async def _classify_failure(repo: Path, r: Result, rc: int, err: str) -> None:
    """A stopped rebase is a conflict to resolve; anything else is a failure."""
    r.returncode, r.stderr = rc, err
    if await rebase_in_progress(repo):
        r.state = STATE_CONFLICT
        r.conflict_files = await unmerged_files(repo)
    else:
        r.state = STATE_FAIL


async def _settled_state(r: Result) -> str:
    moved = bool(r.main_ff or r.branch_ff or r.main_created or r.incoming)
    return STATE_UPDATED if moved else STATE_CLEAN


async def _do_pull(repo: Path, r: Result) -> None:
    """On the default branch: rebase local commits onto the new upstream."""
    args = ["pull", "--rebase", "--autostash"]
    if r.default_branch and not r.upstream:
        # never-pushed default branch: name the remote explicitly
        args += [r.remote, r.default_branch]
    before = await _rev(repo, "HEAD")
    rc, out, err = await _git(repo, *args)
    if rc != 0:
        await _classify_failure(repo, r, rc, err or out)
        return
    after = await _rev(repo, "HEAD")
    if before == after:
        r.state = await _settled_state(r)
        return
    ahead = await _count(repo, f"refs/remotes/{r.remote}/{r.default_branch}..HEAD")
    r.replayed = ahead
    r.state = STATE_REBASED if ahead else STATE_UPDATED


async def _do_rebase(repo: Path, r: Result) -> None:
    """On a branch nobody else has seen: replay it onto the default branch."""
    target = f"{r.remote}/{r.default_branch}"
    before = await _rev(repo, "HEAD")
    rc, out, err = await _git(repo, "rebase", "--autostash", target)
    if rc != 0:
        await _classify_failure(repo, r, rc, err or out)
        return
    after = await _rev(repo, "HEAD")
    if before == after:
        r.state = await _settled_state(r)
        return
    r.replayed = await _count(repo, f"{target}..HEAD")
    r.state = STATE_REBASED


async def _do_detect(repo: Path, r: Result) -> None:
    """Rewrite nothing: just report whether a rebase onto the default would hurt."""
    if not r.default_branch:
        r.state = await _settled_state(r)
        return
    ran, files = await probe_conflicts(repo, f"{r.remote}/{r.default_branch}", "HEAD")
    if ran and files:
        r.conflict_files = files
        r.state = STATE_DIVERGED
        return
    r.state = await _settled_state(r)


async def process(repo: Repo, opts: Options) -> Result:
    r = Result(repo=repo)
    # cheap and always worth having in the report, even for a repo we skip
    r.branch = await current_branch(repo.path)

    # Per-repo opt-out: `git config tmuxpull.ignore true` skips the repo
    # entirely (and no tmux session is created). Unset with
    # `git config --unset tmuxpull.ignore`.
    if (await _config(repo.path, "tmuxpull.ignore")).lower() in _TRUTHY:
        r.state = STATE_IGNORED
        return r

    r.remote = await pick_remote(repo.path)
    if not r.remote:
        r.state = STATE_NO_REMOTE
        return r
    r.default_branch = await default_branch(repo.path, r.remote)
    r.upstream = await upstream_of(repo.path)

    # 1. always pick up the latest remote state -- no side effects on your work
    remote_ref = (
        f"refs/remotes/{r.remote}/{r.default_branch}" if r.default_branch else ""
    )
    before = await _rev(repo.path, remote_ref)
    rc, out, err = await _git(repo.path, "fetch", "--prune", r.remote)
    if rc != 0:
        r.state, r.returncode, r.stderr = STATE_FAIL, rc, err or out
        return r
    await _record_incoming(repo.path, r, before, await _rev(repo.path, remote_ref))

    # 2. decide how much we are allowed to rewrite
    on_default = not r.default_branch or r.branch == r.default_branch
    if on_default:
        r.action = ACT_PULL
    elif not r.branch:
        r.action = ACT_DETECT  # detached HEAD: nothing to rebase
    elif not r.upstream or opts.rebase_pushed:
        r.action = ACT_REBASE
    else:
        r.action = ACT_DETECT  # pushed: a rewrite would cost you a force-push

    # 3. keep the local default branch current for the next branch you cut
    if r.default_branch and not on_default:
        await _fast_forward_default(repo.path, r)

    # 4. align with the branch's own upstream before measuring against default
    if r.action != ACT_PULL and not await _align_with_upstream(repo.path, r):
        return r

    if r.action == ACT_PULL:
        await _do_pull(repo.path, r)
    elif r.action == ACT_REBASE:
        await _do_rebase(repo.path, r)
    else:
        await _do_detect(repo.path, r)
    return r


# --------------------------------------------------------------------------- #
# tmux                                                                        #
# --------------------------------------------------------------------------- #


def _sanitize(name: str) -> str:
    return _UNSAFE_TMUX.sub("_", name) or "rebase"


def _make_session_name(repo: Repo) -> str:
    """Create a tmux session name that looks like a Unix path to the repo."""
    parent = repo.path.parent.name
    repo_name = repo.path.name
    
    # Handle edge cases
    if not parent or parent == "/":
        session_name = repo_name
    else:
        # Use Unix-style forward slash (tmux runs in Unix-like environments)
        session_name = f"{parent}/{repo_name}"
    
    return _sanitize(session_name)


def open_repo_session(server: libtmux.Server, r: Result) -> str:
    """Create or update a tmux session for a specific repo.

    Returns the session name for user reference. The window name is left as
    tmux's default (typically the running command) — the session name already
    identifies the repo, so a hard-coded "rebase" window name adds nothing.
    """
    session_name = _make_session_name(r.repo)

    # Try to get existing session
    sess = server.sessions.get(session_name=session_name, default=None)

    if sess is None:
        sess = server.new_session(
            session_name=session_name,
            start_directory=str(r.repo.path),
            attach=False,
        )
        pane = sess.active_pane
    else:
        # Session already exists (re-run) — add a new window so nothing is lost.
        pane = sess.new_window(
            start_directory=str(r.repo.path),
        ).active_pane

    # Always land on git status to show current state
    pane.send_keys("git status")
    return session_name


# --------------------------------------------------------------------------- #
# reporting                                                                   #
# --------------------------------------------------------------------------- #


def default_log_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base).expanduser() / "tmuxpull" / "last-run.log"


def render_report(results: list[Result], roots: list[str]) -> str:
    """The full run as plain text: every repo, one block each, greppable by state.

    Ordered attention-first, then by name -- a stable document, unlike the live
    output's completion order.
    """
    attn = [r for r in results if r.needs_attention]
    lines = [
        f"# tmuxpull {time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        f"# roots: {' '.join(roots)}",
        f"# {len(results)} repo{_s(len(results))}, {len(attn)} need attention",
        "",
    ]
    width = max((len(r.repo.name) for r in results), default=0)
    for r in sorted(results, key=lambda x: (not x.needs_attention, x.repo.name)):
        where = r.branch or "detached HEAD"
        lines.append(f"[{r.state:<9}] {r.repo.name:<{width}}  ({where})  {r.summary_line()}")
        if r.conflict_files:
            lines.append(f"{'':>12} conflicts: {' '.join(r.conflict_files)}")
        if r.session:
            lines.append(f"{'':>12} tmux attach -t {r.session}")
        if r.state == STATE_FAIL and r.stderr.strip():
            for ln in r.stderr.strip().splitlines()[-5:]:
                lines.append(f"{'':>12} | {ln}")
    lines.append("")
    return "\n".join(lines)


def write_report(path: Path, body: str) -> Path | None:
    """Write the report, best effort -- a failure here must not fail the run."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path
    except OSError as exc:
        print(f"could not write report to {path}: {exc}", file=sys.stderr)
        return None


def print_attention(results: list[Result], log_file: Path | None) -> None:
    """The durable reminder: which repos still need you, and how to get there.

    Printed to stderr before tmux takes over and again when it gives control
    back, so picking a session does not cost you the list.
    """
    attn = [r for r in results if r.needs_attention]
    if attn:
        width = max(len(r.repo.name) for r in attn)
        print(
            f"\n{len(attn)} of {len(results)} repo{_s(len(results))} need attention:",
            file=sys.stderr,
        )
        for r in attn:
            where = f"tmux attach -t {r.session}" if r.session else "(no session)"
            print(f"  {r.repo.name:<{width}}  {r.summary_line()}", file=sys.stderr)
            print(f"  {'':<{width}}  {where}", file=sys.stderr)
    if log_file:
        print(f"full report: {log_file}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# interactive picker                                                          #
# --------------------------------------------------------------------------- #


def _pick_session(names: list[str], failed: set[str]) -> str | None:
    """Interactive TTY picker for the tmux sessions just created.

    Uses stdlib curses. Repos needing attention are listed first and highlighted
    red so they're the natural first pick. Returns the chosen session name, or
    None if the user skipped (Esc/q) or the terminal isn't interactive.

    Keys: Up/Down or j/k to move, PgUp/PgDn to page, g/G for top/bottom,
    Enter to attach, q or Esc to skip.
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    import curses

    ordered = sorted(set(names), key=lambda n: (n not in failed, n))
    if not ordered:
        return None

    def _loop(stdscr: curses.window) -> str | None:
        curses.curs_set(0)
        stdscr.keypad(True)
        try:
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_RED, -1)
        except curses.error:
            pass
        idx = 0
        top = 0
        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            attn = f", {len(failed)} need attention" if failed else ""
            hdr = (
                f"tmuxpull: {len(ordered)} session(s){attn}. "
                "Up/Down (j/k) to move, Enter to attach, q/Esc to skip."
            )
            stdscr.addnstr(0, 0, hdr, max(1, w - 1), curses.A_BOLD)
            body_h = max(1, h - 2)
            if idx < top:
                top = idx
            elif idx >= top + body_h:
                top = idx - body_h + 1
            for row, i in enumerate(range(top, min(top + body_h, len(ordered)))):
                name = ordered[i]
                marker = "! " if name in failed else "  "
                text = f"{marker}{name}"
                attr = curses.A_REVERSE if i == idx else 0
                if name in failed:
                    attr |= curses.color_pair(1)
                stdscr.addnstr(row + 1, 0, text, max(1, w - 1), attr)
            stdscr.refresh()
            k = stdscr.getch()
            if k in (curses.KEY_UP, ord("k")):
                idx = (idx - 1) % len(ordered)
            elif k in (curses.KEY_DOWN, ord("j")):
                idx = (idx + 1) % len(ordered)
            elif k == curses.KEY_HOME or k == ord("g"):
                idx = 0
            elif k == curses.KEY_END or k == ord("G"):
                idx = len(ordered) - 1
            elif k == curses.KEY_NPAGE:
                idx = min(len(ordered) - 1, idx + body_h)
            elif k == curses.KEY_PPAGE:
                idx = max(0, idx - body_h)
            elif k in (curses.KEY_ENTER, 10, 13):
                return ordered[idx]
            elif k in (27, ord("q")):  # Esc or q
                return None
            elif k == curses.KEY_RESIZE:
                continue

    try:
        return curses.wrapper(_loop)
    except (KeyboardInterrupt, curses.error):
        return None


def walk_sessions(names: list[str], failed: set[str], reprint) -> None:
    """Pick a session, hand the terminal to tmux, and come back for the next one.

    tmux runs as a CHILD, not via exec, so detaching returns here instead of
    ending the process: the queue can be walked repo by repo, and the attention
    list is reprinted every time control comes back. Inside an existing tmux
    client there is nothing to come back to -- switch-client returns at once --
    so that path hands over and stops.
    """
    inside_tmux = bool(os.environ.get("TMUX"))
    while True:
        picked = _pick_session(names, failed)
        if not picked:
            return
        if inside_tmux:
            # nested `attach` is refused; switching moves the client and returns
            subprocess.run(["tmux", "switch-client", "-t", picked], check=False)
            return
        subprocess.run(["tmux", "attach", "-t", picked], check=False)
        reprint()


# --------------------------------------------------------------------------- #
# main                                                                        #
# --------------------------------------------------------------------------- #


async def _run(repos: list[Repo], jobs: int, verbose: int, opts: Options) -> list[Result]:
    """Update all repos concurrently, printing each result AS IT COMPLETES.

    Output is in completion order (not input order) so the user sees live
    progress; each line is prefixed with an [n/N] counter. Repos sharing an
    object store (a repo and its worktrees) are serialized against each other.
    """
    sem = asyncio.Semaphore(jobs)
    width = max((len(r.name) for r in repos), default=0)
    total = len(repos)
    results: list[Result] = []

    common = await asyncio.gather(*(common_dir(r.path) for r in repos))
    locks: dict[str, asyncio.Lock] = {}
    for cd in common:
        locks.setdefault(cd, asyncio.Lock())

    async def one(repo: Repo, lock: asyncio.Lock) -> Result:
        async with sem, lock:
            return await process(repo, opts)

    tasks = [
        asyncio.create_task(one(repo, locks[cd]))
        for repo, cd in zip(repos, common, strict=True)
    ]
    for done, task in enumerate(asyncio.as_completed(tasks), start=1):
        r = await task
        stream = sys.stdout if not r.needs_attention else sys.stderr
        counter = f"[{done}/{total}]"
        print(f"{counter:>9} {r.repo.name:<{width}}  {r.summary_line()}", file=stream, flush=True)
        if verbose > 0 and r.log_lines:
            preview = r.log_lines if verbose > 1 else r.log_lines[:3]
            for ln in preview:
                print(f"{'':>9} {ln}", file=stream, flush=True)
            if verbose <= 1 and len(r.log_lines) > 3:
                print(f"{'':>9} ... +{len(r.log_lines) - 3} more", file=stream, flush=True)
        results.append(r)
    return results


def version_line() -> str:
    """Version plus the file it is running from.

    The path matters as much as the number: the same machine can have a PyPI
    install, a `uv tool` install and a curl'd standalone script on PATH, and
    this says which one you just ran.
    """
    return f"tmuxpull {__version__} ({Path(__file__).resolve()})"


class _VersionAction(argparse.Action):
    """Print version_line() verbatim.

    argparse's built-in "version" action runs its text through the help
    formatter, which re-wraps at terminal width and splits the path onto its
    own line -- so `--version` output would depend on how wide the window is.
    """

    def __init__(self, option_strings: list[str], dest: str, **kw: object) -> None:
        super().__init__(option_strings, dest, nargs=0, **kw)  # type: ignore[arg-type]

    def __call__(self, parser, namespace, values, option_string=None) -> None:  # noqa: ANN001
        print(version_line())
        parser.exit()


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="tmuxpull",
        description=(
            "Concurrently bring every Git repo under the given roots in step with its "
            "remote, so merge conflicts surface now instead of at push time. A repo on "
            "its default branch is rebased in place; a repo on a never-pushed branch is "
            "replayed onto the default branch; a repo on a PUSHED branch is only probed "
            "for conflicts (rewriting it would cost you a force-push). Prints a summary, "
            "writes a report, and opens one tmux session per repo."
        ),
        epilog=(
            "Per-repo git config: `tmuxpull.ignore true` skips a repo entirely; "
            "`tmuxpull.defaultBranch <name>` overrides the detected default branch "
            "(detection order: this override, refs/remotes/<remote>/HEAD, then "
            "main/master/trunk)."
        ),
    )
    ap.add_argument(
        "-V",
        "--version",
        action=_VersionAction,
        help="Show the version and which copy is running, then exit.",
    )
    ap.add_argument(
        "-d",
        "--max-depth",
        type=int,
        default=2,
        metavar="N",
        help="Directory search depth (default: 2).",
    )
    ap.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=min(8, (os.cpu_count() or 2) * 2),
        metavar="N",
        help="Max concurrent repos (default: min(8, 2*CPU)).",
    )
    ap.add_argument(
        "--tmux",
        choices=("on", "off"),
        default="on",
        help="Create tmux sessions: on (default) or off.",
    )

    ap.add_argument(
        "-x",
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help=(
            "Exclude repos whose display name matches this glob (repeatable), "
            "e.g. -x 'kirodotdev/*'. For a sticky per-repo skip, run "
            "`git config tmuxpull.ignore true` in the repo instead."
        ),
    )
    ap.add_argument(
        "--rebase-pushed",
        action="store_true",
        help=(
            "Also rebase branches that have been pushed. Rewrites published "
            "commits, so the next push needs --force-with-lease. Refused for a "
            "branch whose upstream has commits you do not have."
        ),
    )
    ap.add_argument(
        "--log",
        metavar="PATH",
        default=None,
        help=(
            "Write the run report here "
            "(default: $XDG_STATE_HOME/tmuxpull/last-run.log)."
        ),
    )
    ap.add_argument(
        "--no-log",
        action="store_true",
        help="Do not write a report file.",
    )
    ap.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Show incoming commit subjects. -v = top 3, -vv = all.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="List repos that would be updated, then exit.",
    )
    ap.add_argument(
        "dirs",
        nargs="+",
        metavar="dir",
        help="Root directories to scan for Git repos.",
    )
    args = ap.parse_args()

    repos = find_repos(args.dirs, args.max_depth)
    repos, excluded = apply_excludes(repos, args.exclude)
    for r in excluded:
        print(f"excluded: {r.name}", file=sys.stderr)
    if not repos:
        print("no git repos found", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        for r in repos:
            print(r.path)
        return

    print(
        f"updating {len(repos)} repo{_s(len(repos))} (jobs={args.jobs})...",
        file=sys.stderr,
    )

    opts = Options(rebase_pushed=args.rebase_pushed)
    results = asyncio.run(_run(repos, args.jobs, args.verbose, opts))

    tmux_wanted = args.tmux == "on"
    if tmux_wanted and shutil.which("tmux") is None:
        print("tmux not on PATH; skipping sessions", file=sys.stderr)
        tmux_wanted = False

    session_names: list[str] = []
    attention_names: set[str] = set()
    if tmux_wanted:
        server = libtmux.Server()
        for r in results:
            if not r.wants_session:
                continue
            r.session = open_repo_session(server, r)
            session_names.append(r.session)
            if r.needs_attention:
                attention_names.add(r.session)

    log_file: Path | None = None
    if not args.no_log:
        path = Path(args.log).expanduser() if args.log else default_log_path()
        log_file = write_report(path, render_report(results, args.dirs))

    def reprint() -> None:
        print_attention(results, log_file)

    # The list has to outlive the handoff: print it BEFORE tmux takes the
    # terminal (so it stays in the launching shell's scrollback) and again
    # whenever control comes back.
    reprint()

    if session_names:
        if sys.stdin.isatty() and sys.stdout.isatty():
            walk_sessions(session_names, attention_names, reprint)
        else:
            # Non-interactive (piped/redirected): print the full list.
            print(f"\n{len(session_names)} tmux session(s) created:", file=sys.stderr)
            for name in sorted(set(session_names)):
                print(f"  tmux attach -t {name}", file=sys.stderr)

    sys.exit(1 if any(r.needs_attention for r in results) else 0)

if __name__ == "__main__":
    main()
