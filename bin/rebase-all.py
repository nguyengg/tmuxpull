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
tmuxpull -- run `git pull --rebase --autostash` across every Git repo under
the given roots, concurrently. Print a per-repo summary of what changed, and
create a dedicated tmux session per repo with a rebase window showing `git status`.

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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import libtmux


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


# --------------------------------------------------------------------------- #
# data model                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Repo:
    path: Path  # absolute filesystem path
    name: str   # display + tmux window name (posix-style relative path)


@dataclass(slots=True)
class Result:
    repo: Repo
    returncode: int
    stdout: str
    stderr: str
    old_sha: str = ""
    new_sha: str = ""
    log_lines: list[str] = field(default_factory=list)
    shortstat: str = ""
    skipped: bool = False  # repo opted out via `git config tmuxpull.ignore true`

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def changed(self) -> bool:
        return self.ok and bool(self.old_sha) and self.old_sha != self.new_sha

    @property
    def needs_attention(self) -> bool:
        # A failed rebase leaves the repo in a state you have to intervene in
        # (conflict markers, in-progress rebase, or an unpopped autostash).
        return not self.ok

    def summary_line(self) -> str:
        if self.skipped:
            return "- ignored (git config tmuxpull.ignore)"
        if not self.ok:
            tail = (self.stderr.strip().splitlines() or [f"exit {self.returncode}"])[-1]
            return f"! FAIL: {tail}"
        if not self.changed:
            return "= up to date"
        n = len(self.log_lines)
        return f"+ {n} commit{'s' if n != 1 else ''}  {self.shortstat}".rstrip()


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
# git                                                                         #
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


async def rebase(repo: Repo, sem: asyncio.Semaphore) -> Result:
    async with sem:
        # Per-repo opt-out: `git config tmuxpull.ignore true` skips the pull
        # entirely (and no tmux session is created). Unset with
        # `git config --unset tmuxpull.ignore`.
        rc_ign, ign_out, _ = await _git(repo.path, "config", "--get", "tmuxpull.ignore")
        if rc_ign == 0 and ign_out.strip().lower() in ("true", "1", "yes", "on"):
            return Result(repo=repo, returncode=0, stdout="", stderr="", skipped=True)

        rc, out, err = 0, "", ""
        _, old_sha, _ = await _git(repo.path, "rev-parse", "HEAD")
        old_sha = old_sha.strip()
        rc, out, err = await _git(repo.path, "pull", "--rebase", "--autostash")
        _, new_sha, _ = await _git(repo.path, "rev-parse", "HEAD")
        new_sha = new_sha.strip()

        log_lines: list[str] = []
        shortstat = ""
        if rc == 0 and old_sha and new_sha and old_sha != new_sha:
            _, log_out, _ = await _git(
                repo.path,
                "log",
                "--oneline",
                "--no-decorate",
                f"{old_sha}..{new_sha}",
            )
            log_lines = [ln for ln in log_out.splitlines() if ln]
            _, ss, _ = await _git(repo.path, "diff", "--shortstat", f"{old_sha}..{new_sha}")
            shortstat = ss.strip()

    return Result(
        repo=repo,
        returncode=rc,
        stdout=out,
        stderr=err,
        old_sha=old_sha,
        new_sha=new_sha,
        log_lines=log_lines,
        shortstat=shortstat,
    )


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
    
    Returns the session name for user reference.
    """
    session_name = _make_session_name(r.repo)
    window_name = "rebase"
    
    # Try to get existing session
    sess = server.sessions.get(session_name=session_name, default=None)
    
    if sess is None:
        # Create new session with rebase as the first window
        sess = server.new_session(
            session_name=session_name,
            window_name=window_name,
            start_directory=str(r.repo.path),
            attach=False,
        )
        pane = sess.active_pane
    else:
        # Session exists, add a new rebase window
        # Check if rebase window already exists
        existing_rebase = None
        for window in sess.windows:
            if window.name == window_name:
                existing_rebase = window
                break
        
        if existing_rebase:
            # Rebase window exists, make it unique with timestamp
            import time
            window_name = f"rebase-{int(time.time()) % 10000}"
        
        pane = sess.new_window(
            window_name=window_name,
            start_directory=str(r.repo.path),
        ).active_pane
    
    # Always land on git status to show current state
    pane.send_keys("git status")
    return session_name


# --------------------------------------------------------------------------- #
# main                                                                        #
# --------------------------------------------------------------------------- #


async def _run(repos: list[Repo], jobs: int, verbose: int) -> list[Result]:
    """Rebase all repos concurrently, printing each result AS IT COMPLETES.

    Output is in completion order (not input order) so the user sees live
    progress; each line is prefixed with an [n/N] counter.
    """
    sem = asyncio.Semaphore(jobs)
    width = max((len(r.name) for r in repos), default=0)
    total = len(repos)
    results: list[Result] = []

    tasks = [asyncio.create_task(rebase(r, sem)) for r in repos]
    for done, task in enumerate(asyncio.as_completed(tasks), start=1):
        r = await task
        stream = sys.stdout if r.ok else sys.stderr
        counter = f"[{done}/{total}]"
        print(f"{counter:>9} {r.repo.name:<{width}}  {r.summary_line()}", file=stream, flush=True)
        if verbose > 0 and r.changed:
            preview = r.log_lines if verbose > 1 else r.log_lines[:3]
            for ln in preview:
                print(f"{'':>9} {ln}", file=stream, flush=True)
            if verbose <= 1 and len(r.log_lines) > 3:
                print(f"{'':>9} ... +{len(r.log_lines) - 3} more", file=stream, flush=True)
        results.append(r)
    return results


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="tmuxpull",
        description=(
            "Concurrently `git pull --rebase --autostash` every Git repo under the "
            "given roots. Print a per-repo summary of what changed, and create a "
            "dedicated tmux session per repo with a rebase window."
        ),
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
        help="Max concurrent rebases (default: min(8, 2*CPU)).",
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
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Show commit subjects. -v = top 3, -vv = all.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="List repos that would be pulled, then exit.",
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
        f"rebasing {len(repos)} repo{'s' if len(repos) != 1 else ''} (jobs={args.jobs})...",
        file=sys.stderr,
    )

    results = asyncio.run(_run(repos, args.jobs, args.verbose))
    fails = sum(1 for r in results if not r.ok)

    tmux_wanted = args.tmux == "on"
    if tmux_wanted and shutil.which("tmux") is None:
        print("tmux not on PATH; skipping sessions", file=sys.stderr)
    elif tmux_wanted:
        server = libtmux.Server()
        session_names = []

        for r in results:
            if r.skipped:
                continue
            session_name = open_repo_session(server, r)
            session_names.append(session_name)
        
        if session_names:
            print(f"\n{len(session_names)} tmux session(s) created:", file=sys.stderr)
            for name in sorted(set(session_names)):
                print(f"  tmux attach -t {name}", file=sys.stderr)

    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    main()
