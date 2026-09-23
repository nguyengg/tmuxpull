"""The zsh port must behave like the Python source of truth.

The two implementations have drifted before, and the README promises parity, so
this builds the SAME multi-scenario fixture twice, runs one implementation
against each, and diffs their normalised summary lines.

The fixture never pushes: remotes are made with `git clone --bare` and advanced
with `git fetch <seed> main:main`, which keeps it entirely local.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ZSH_SCRIPT = ROOT / "bin" / "rebase-all"

pytestmark = pytest.mark.skipif(
    shutil.which("zsh") is None, reason="zsh not available"
)


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
        PYTHONPATH=str(ROOT / "src"),
    )
    return env


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=_env())


def build_fixture(root: Path) -> Path:
    """One repo per scenario under <root>/work. Returns the work dir."""
    remotes = root / "remotes"
    work = root / "work"
    remotes.mkdir(parents=True)
    work.mkdir(parents=True)

    def mk(name: str) -> Path:
        seed = root / f"seed-{name}"
        seed.mkdir()
        git(seed.parent, "init", "-q", "--initial-branch=main", str(seed))
        (seed / "shared.txt").write_text("base\n", encoding="utf-8")
        git(seed, "add", ".")
        git(seed, "commit", "-qm", "init")
        git(root, "clone", "-q", "--bare", str(seed), str(remotes / f"{name}.git"))
        git(root, "clone", "-q", str(remotes / f"{name}.git"), str(work / name))
        return work / name

    def land(name: str) -> None:
        seed = root / f"seed-{name}"
        (seed / "upstream.txt").write_text("theirs\n", encoding="utf-8")
        git(seed, "add", ".")
        git(seed, "commit", "-qm", "upstream work")
        git(remotes / f"{name}.git", "fetch", "-q", str(seed), "main:main")

    def clash(name: str) -> None:
        seed = root / f"seed-{name}"
        (seed / "shared.txt").write_text("theirs\n", encoding="utf-8")
        git(seed, "commit", "-qam", "theirs")
        git(remotes / f"{name}.git", "fetch", "-q", str(seed), "main:main")

    mk("on-main-ff")
    land("on-main-ff")

    w = mk("on-main-replay")
    git(w, "commit", "-q", "--allow-empty", "-m", "mine")
    land("on-main-replay")

    w = mk("feat-unpushed")
    git(w, "checkout", "-qb", "feat/x")
    git(w, "commit", "-q", "--allow-empty", "-m", "wip")
    land("feat-unpushed")

    w = mk("feat-conflict")
    git(w, "checkout", "-qb", "feat/y")
    (w / "shared.txt").write_text("mine\n", encoding="utf-8")
    git(w, "commit", "-qam", "mine")
    clash("feat-conflict")

    w = mk("feat-pushed")
    git(w, "checkout", "-qb", "feat/z")
    (w / "shared.txt").write_text("mine\n", encoding="utf-8")
    git(w, "commit", "-qam", "mine")
    git(remotes / "feat-pushed.git", "fetch", "-q", str(w), "feat/z:feat/z")
    git(w, "fetch", "-q", "origin")
    git(w, "branch", "-q", "--set-upstream-to=origin/feat/z")
    clash("feat-pushed")

    w = mk("detached")
    git(w, "checkout", "-q", "--detach", "HEAD")
    land("detached")

    w = mk("wt-owner")
    git(w, "worktree", "add", "-q", "-b", "feat/wt", str(work / "wt-owner.wt"))
    git(work / "wt-owner.wt", "commit", "-q", "--allow-empty", "-m", "wip")
    land("wt-owner")

    w = mk("quiet")
    git(w, "config", "tmuxpull.ignore", "true")

    solo = work / "no-remote"
    solo.mkdir()
    git(work, "init", "-q", "--initial-branch=main", str(solo))
    git(solo, "commit", "-q", "--allow-empty", "-m", "x")

    return work


def summaries(argv: list[str], work: Path, state: Path) -> list[str]:
    """Run an implementation and return its per-repo lines, order-independent."""
    env = _env()
    env["XDG_STATE_HOME"] = str(state)
    proc = subprocess.run(
        [*argv, "--tmux", "off", "-d", "1", str(work)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(work.parent),
    )
    lines: list[str] = []
    for raw in (proc.stdout + proc.stderr).splitlines():
        line = raw.strip()
        if not line.startswith("["):
            continue
        _, _, rest = line.partition("] ")
        lines.append(" ".join(rest.split()))
    assert lines, f"no per-repo output:\n{proc.stdout}\n{proc.stderr}"
    return sorted(lines)


def test_zsh_port_matches_python(tmp_path: Path):
    py_work = build_fixture(tmp_path / "py")
    zsh_work = build_fixture(tmp_path / "zsh")

    from_python = summaries(
        [sys.executable, "-m", "tmuxpull"], py_work, tmp_path / "py-state"
    )
    from_zsh = summaries([str(ZSH_SCRIPT)], zsh_work, tmp_path / "zsh-state")

    assert from_python == from_zsh
    # and the fixture really did exercise every branch of the dispatch
    joined = "\n".join(from_python)
    assert "CONFLICT" in joined
    assert "DIVERGES from main" in joined
    assert "rebased 1 onto main" in joined
    assert "- ignored" in joined
    assert "- no remote" in joined


def test_zsh_port_writes_the_same_report_shape(tmp_path: Path):
    work = build_fixture(tmp_path / "zsh")
    state = tmp_path / "state"
    summaries([str(ZSH_SCRIPT)], work, state)

    report = (state / "tmuxpull" / "last-run.log").read_text(encoding="utf-8")
    assert "need attention" in report
    assert "[conflict " in report
    assert "conflicts: shared.txt" in report
    # attention-first ordering
    first = [ln for ln in report.splitlines() if ln.startswith("[")][0]
    assert first.startswith("[diverged") or first.startswith("[conflict")
