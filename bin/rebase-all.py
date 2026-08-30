#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "libtmux>=0.35",
# ]
# ///
"""
rebase-all -- run `git pull --rebase --autostash` across every Git repo under
the given roots, concurrently. Print a per-repo summary of what changed, and
open a tmux window per repo that needs attention (rebase failed, conflict
in progress, or a stash was left behind) landing on `git status` in the repo.

Usage:
    rebase-all.py [-d DEPTH] [-j JOBS] [--tmux {all,attn,off}]
                  [-s SESSION] [-v] [--dry-run] DIR [DIR ...]

The shebang uses uv (https://docs.astral.sh/uv/); the /// script block below
is PEP 723 inline metadata. `chmod +x rebase-all.py && ./rebase-all.py ...`
just works on any machine with uv installed; no venv, no pip install.
"""
from __future__ import annotations

import argparse
import asyncio
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

# tmux session and window names cannot contain ':' or '.' and shouldn't carry
# whitespace. Replace with '_'.
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
        if not self.ok:
            tail = (self.stderr.strip().splitlines() or [f"exit {self.returncode}"])[-1]
            return f"✗ FAIL: {tail}"
        if not self.changed:
            return "· up to date"
        n = len(self.log_lines)
        return f"✓ {n} commit{'s' if n != 1 else ''}  {self.shortstat}".rstrip()


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


def open_pane(server: libtmux.Server, session_name: str, r: Result) -> None:
    """Ensure a tmux session exists and open a window in it rooted at r.repo.

    The window lands on `git status` so a failed rebase is immediately visible.
    """
    win = _sanitize(r.repo.name)
    sess = server.sessions.get(session_name=session_name, default=None)
    if sess is None:
        sess = server.new_session(
            session_name=session_name,
            window_name=win,
            start_directory=str(r.repo.path),
            attach=False,
        )
        pane = sess.active_pane
    else:
        pane = sess.new_window(
            window_name=win,
            start_directory=str(r.repo.path),
        ).active_pane
    pane.send_keys("git status")


# --------------------------------------------------------------------------- #
# main                                                                        #
# --------------------------------------------------------------------------- #


async def _run(repos: list[Repo], jobs: int) -> list[Result]:
    sem = asyncio.Semaphore(jobs)
    return await asyncio.gather(*(rebase(r, sem) for r in repos))


def _print_report(results: list[Result], verbose: int) -> int:
    fails = 0
    width = max((len(r.repo.name) for r in results), default=0)
    for r in results:
        stream = sys.stdout if r.ok else sys.stderr
        print(f"{r.repo.name:<{width}}  {r.summary_line()}", file=stream)
        if verbose > 0 and r.changed:
            preview = r.log_lines if verbose > 1 else r.log_lines[:3]
            for ln in preview:
                print(f"  {ln}", file=stream)
            if verbose <= 1 and len(r.log_lines) > 3:
                print(f"  ... +{len(r.log_lines) - 3} more", file=stream)
        if not r.ok:
            fails += 1
    return fails


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="rebase-all.py",
        description=(
            "Concurrently `git pull --rebase --autostash` every Git repo under the "
            "given roots. Print a per-repo summary of what changed, and open a tmux "
            "window per repo that needs your attention."
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
        choices=("all", "attn", "off"),
        default="attn",
        help="Open tmux windows for: all repos, only ones needing attention (default), or none.",
    )
    ap.add_argument(
        "-s",
        "--session",
        default="rebase",
        help="tmux session name (default: 'rebase').",
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

    results = asyncio.run(_run(repos, args.jobs))
    fails = _print_report(results, args.verbose)

    tmux_wanted = args.tmux != "off"
    if tmux_wanted and shutil.which("tmux") is None:
        print("tmux not on PATH; skipping windows", file=sys.stderr)
    elif tmux_wanted:
        server = libtmux.Server()
        session = _sanitize(args.session)
        opened = 0
        for r in results:
            if args.tmux == "attn" and not r.needs_attention:
                continue
            open_pane(server, session, r)
            opened += 1
        if opened:
            print(
                f"\n{opened} tmux window{'s' if opened != 1 else ''} in session "
                f"'{session}'.  attach: tmux attach -t {session}",
                file=sys.stderr,
            )

    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()