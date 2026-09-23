"""Scenario tests: what tmuxpull does to each kind of local checkout.

These drive real `git` against local bare "remotes" -- no network. One test per
row of the design: on the default branch, on a never-pushed branch, on a pushed
branch, in a worktree, detached, and the degenerate repos.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from tmuxpull import (
    ACT_DETECT,
    ACT_PULL,
    ACT_REBASE,
    STATE_CLEAN,
    STATE_CONFLICT,
    STATE_DIVERGED,
    STATE_IGNORED,
    STATE_NO_REMOTE,
    STATE_REBASED,
    STATE_UNSYNCED,
    STATE_UPDATED,
    Options,
    Repo,
    Result,
    branch_is_checked_out,
    common_dir,
    current_branch,
    default_branch,
    pick_remote,
    probe_conflicts,
    process,
    rebase_in_progress,
    render_report,
    upstream_of,
)

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available",
)


# --------------------------------------------------------------------------- #
# harness                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def hermetic_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin identity and ignore the developer's own git config.

    Set in os.environ rather than passed per-call, because the git processes
    under test are spawned by tmuxpull itself and inherit this environment.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "tmuxpull test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "tmuxpull test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def write(repo: Path, name: str, body: str) -> None:
    (repo / name).write_text(body, encoding="utf-8")


def commit(repo: Path, name: str, body: str = "x\n") -> str:
    write(repo, name, body)
    git(repo, "add", name)
    git(repo, "commit", "-m", f"touch {name}")
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clone of a bare remote whose default branch is 'main'.

    tmp_path/origin.git (bare) <- tmp_path/seed (stands in for a teammate)
                               -> tmp_path/work (the repo under test)
    """
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(bare))

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--initial-branch=main")
    write(seed, "shared.txt", "base\n")
    commit(seed, "shared.txt", "base\n")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-u", "origin", "main")

    work = tmp_path / "work"
    git(tmp_path, "clone", str(bare), str(work))
    return work


def seed_of(clone_path: Path) -> Path:
    return clone_path.parent / "seed"


def land_upstream(clone_path: Path, name: str = "teammate.txt", body: str = "theirs\n") -> None:
    """Land a commit on origin/main, as a teammate would."""
    seed = seed_of(clone_path)
    commit(seed, name, body)
    git(seed, "push", "origin", "main")


def run(repo_path: Path, **opts) -> Result:
    repo = Repo(path=repo_path, name=repo_path.name)
    return asyncio.run(process(repo, Options(**opts)))


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def ref(repo: Path, name: str) -> str:
    return git(repo, "rev-parse", name).strip()


# --------------------------------------------------------------------------- #
# resolution helpers                                                          #
# --------------------------------------------------------------------------- #


def test_pick_remote_prefers_origin(clone: Path):
    git(clone, "remote", "add", "fork", str(clone.parent / "origin.git"))
    assert asyncio.run(pick_remote(clone)) == "origin"


def test_default_branch_from_remote_head(clone: Path):
    assert asyncio.run(default_branch(clone, "origin")) == "main"


def test_default_branch_config_override_wins(clone: Path):
    git(clone, "config", "tmuxpull.defaultBranch", "release")
    assert asyncio.run(default_branch(clone, "origin")) == "release"


def test_default_branch_probes_when_remote_head_missing(clone: Path):
    git(clone, "update-ref", "-d", "refs/remotes/origin/HEAD")
    assert asyncio.run(default_branch(clone, "origin")) == "main"


def test_current_branch_and_upstream(clone: Path):
    assert asyncio.run(current_branch(clone)) == "main"
    assert asyncio.run(upstream_of(clone)) == "origin/main"
    git(clone, "checkout", "-b", "feat/x")
    assert asyncio.run(current_branch(clone)) == "feat/x"
    assert asyncio.run(upstream_of(clone)) == ""
    git(clone, "checkout", "--detach", "HEAD")
    assert asyncio.run(current_branch(clone)) == ""


def test_worktrees_share_a_common_dir(clone: Path):
    wt = clone.parent / "work.wt"
    git(clone, "worktree", "add", "-b", "feat/wt", str(wt))
    assert asyncio.run(common_dir(clone)) == asyncio.run(common_dir(wt))
    assert asyncio.run(branch_is_checked_out(clone, "feat/wt")) is True
    assert asyncio.run(branch_is_checked_out(clone, "nope")) is False


def test_probe_conflicts_reads_without_touching_anything(clone: Path):
    git(clone, "checkout", "-b", "feat/x")
    commit(clone, "shared.txt", "mine\n")
    land_upstream(clone, "shared.txt", "theirs\n")
    git(clone, "fetch", "origin")
    before = head(clone)

    ran, files = asyncio.run(probe_conflicts(clone, "origin/main", "HEAD"))

    assert ran
    assert files == ["shared.txt"]
    assert head(clone) == before
    assert git(clone, "status", "--porcelain") == ""


# --------------------------------------------------------------------------- #
# scenario 1: on the default branch                                           #
# --------------------------------------------------------------------------- #


def test_on_main_fast_forwards(clone: Path):
    land_upstream(clone)

    r = run(clone)

    assert r.action == ACT_PULL
    assert r.state == STATE_UPDATED
    assert r.incoming == 1
    assert r.replayed == 0
    assert r.summary_line().startswith("+ 1 commit  ")
    assert ref(clone, "refs/heads/main") == ref(clone, "refs/remotes/origin/main")


def test_on_main_up_to_date(clone: Path):
    r = run(clone)
    assert r.state == STATE_CLEAN
    assert r.summary_line() == "= up to date"


def test_on_main_replays_unpushed_commits(clone: Path):
    commit(clone, "mine.txt")
    land_upstream(clone)

    r = run(clone)

    assert r.state == STATE_REBASED
    assert r.replayed == 1
    assert r.incoming == 1
    assert r.summary_line().startswith("~ rebased 1 onto main, pulled 1 commit  ")
    # my commit is now on top of theirs
    assert git(clone, "log", "--oneline", "-1", "--format=%s").strip() == "touch mine.txt"


def test_on_main_conflict_is_left_in_progress(clone: Path):
    commit(clone, "shared.txt", "mine\n")
    land_upstream(clone, "shared.txt", "theirs\n")

    r = run(clone)

    assert r.state == STATE_CONFLICT
    assert r.needs_attention
    assert r.conflict_files == ["shared.txt"]
    assert asyncio.run(rebase_in_progress(clone)) is True
    assert "resolve here" in r.summary_line()


def test_on_main_autostash_keeps_uncommitted_work(clone: Path):
    write(clone, "scratch.txt", "not staged\n")
    land_upstream(clone)

    r = run(clone)

    assert r.state == STATE_UPDATED
    assert (clone / "scratch.txt").read_text(encoding="utf-8") == "not staged\n"


# --------------------------------------------------------------------------- #
# scenario 2a: never-pushed feature branch (the original bug)                 #
# --------------------------------------------------------------------------- #


def test_unpushed_branch_is_replayed_onto_default(clone: Path):
    """`git pull --rebase` used to fail outright here: no upstream."""
    git(clone, "checkout", "-b", "feat/local-only")
    commit(clone, "wip.txt")
    land_upstream(clone)

    r = run(clone)

    assert r.action == ACT_REBASE
    assert r.state == STATE_REBASED
    assert r.replayed == 1
    assert r.main_ff == 1  # local main kept current too, without a checkout
    assert git(clone, "rev-parse", "--abbrev-ref", "HEAD").strip() == "feat/local-only"
    # my work now sits on top of the new origin/main
    assert ref(clone, "HEAD~1") == ref(clone, "refs/remotes/origin/main")
    assert ref(clone, "refs/heads/main") == ref(clone, "refs/remotes/origin/main")


def test_unpushed_branch_conflict_is_left_in_progress(clone: Path):
    git(clone, "checkout", "-b", "feat/clash")
    commit(clone, "shared.txt", "mine\n")
    land_upstream(clone, "shared.txt", "theirs\n")

    r = run(clone)

    assert r.state == STATE_CONFLICT
    assert r.conflict_files == ["shared.txt"]
    assert asyncio.run(rebase_in_progress(clone)) is True


def test_unpushed_branch_nothing_new_upstream(clone: Path):
    git(clone, "checkout", "-b", "feat/quiet")
    commit(clone, "wip.txt")

    r = run(clone)

    assert r.state == STATE_CLEAN
    assert r.summary_line() == "= up to date"


def test_unpushed_branch_autostash_keeps_uncommitted_work(clone: Path):
    git(clone, "checkout", "-b", "feat/dirty")
    commit(clone, "wip.txt")
    write(clone, "scratch.txt", "not staged\n")
    land_upstream(clone)

    r = run(clone)

    assert r.state == STATE_REBASED
    assert (clone / "scratch.txt").read_text(encoding="utf-8") == "not staged\n"


# --------------------------------------------------------------------------- #
# scenario 2b: pushed feature branch -- detect only                           #
# --------------------------------------------------------------------------- #


def test_pushed_branch_is_probed_not_rewritten(clone: Path):
    git(clone, "checkout", "-b", "feat/pushed")
    tip = commit(clone, "wip.txt")
    git(clone, "push", "-u", "origin", "feat/pushed")
    land_upstream(clone, "shared.txt", "theirs\n")
    # make it genuinely conflict with the new main
    git(clone, "rm", "--cached", "-q", "wip.txt")
    git(clone, "checkout", "HEAD", "--", "wip.txt")
    write(clone, "shared.txt", "mine\n")
    git(clone, "add", "shared.txt")
    git(clone, "commit", "-m", "touch shared.txt")
    tip = head(clone)
    git(clone, "push", "origin", "feat/pushed")

    r = run(clone)

    assert r.action == ACT_DETECT
    assert r.state == STATE_DIVERGED
    assert r.needs_attention
    assert r.conflict_files == ["shared.txt"]
    assert head(clone) == tip  # nothing was rewritten
    assert asyncio.run(rebase_in_progress(clone)) is False
    assert "DIVERGES from main" in r.summary_line()


def test_pushed_branch_clean_against_default(clone: Path):
    git(clone, "checkout", "-b", "feat/pushed")
    tip = commit(clone, "wip.txt")
    git(clone, "push", "-u", "origin", "feat/pushed")
    land_upstream(clone)

    r = run(clone)

    assert r.action == ACT_DETECT
    assert r.state == STATE_UPDATED
    assert not r.needs_attention
    assert r.main_ff == 1
    assert head(clone) == tip
    assert r.summary_line().startswith("+ main +1")


def test_pushed_branch_fast_forwards_to_its_own_upstream(clone: Path):
    """Work pushed from another machine is picked up -- that is a ff, not a rewrite."""
    git(clone, "checkout", "-b", "feat/shared")
    git(clone, "push", "-u", "origin", "feat/shared")
    # "other machine" pushes one more commit on the same branch
    other = clone.parent / "other"
    git(clone.parent, "clone", "--branch", "feat/shared", str(clone.parent / "origin.git"), str(other))
    commit(other, "from-laptop.txt")
    git(other, "push", "origin", "feat/shared")

    r = run(clone)

    assert r.action == ACT_DETECT
    assert r.branch_ff == 1
    assert r.state == STATE_UPDATED
    assert (clone / "from-laptop.txt").exists()
    assert "feat/shared +1" in r.summary_line()


def test_pushed_branch_diverged_from_upstream_is_flagged(clone: Path):
    git(clone, "checkout", "-b", "feat/split")
    git(clone, "push", "-u", "origin", "feat/split")
    other = clone.parent / "other"
    git(clone.parent, "clone", "--branch", "feat/split", str(clone.parent / "origin.git"), str(other))
    commit(other, "theirs.txt")
    git(other, "push", "origin", "feat/split")
    mine = commit(clone, "mine.txt")  # both sides moved

    r = run(clone)

    assert r.state == STATE_UNSYNCED
    assert r.needs_attention
    assert head(clone) == mine  # left exactly as it was
    assert "UNSYNCED" in r.summary_line()


def test_rebase_pushed_opt_in_rewrites_the_branch(clone: Path):
    git(clone, "checkout", "-b", "feat/pushed")
    tip = commit(clone, "wip.txt")
    git(clone, "push", "-u", "origin", "feat/pushed")
    land_upstream(clone)

    r = run(clone, rebase_pushed=True)

    assert r.action == ACT_REBASE
    assert r.state == STATE_REBASED
    assert r.replayed == 1
    assert head(clone) != tip  # rewritten: this is what costs a force-push
    assert ref(clone, "HEAD~1") == ref(clone, "refs/remotes/origin/main")


def test_rebase_pushed_refuses_when_upstream_has_commits(clone: Path):
    git(clone, "checkout", "-b", "feat/split")
    git(clone, "push", "-u", "origin", "feat/split")
    other = clone.parent / "other"
    git(clone.parent, "clone", "--branch", "feat/split", str(clone.parent / "origin.git"), str(other))
    commit(other, "theirs.txt")
    git(other, "push", "origin", "feat/split")
    mine = commit(clone, "mine.txt")

    r = run(clone, rebase_pushed=True)

    assert r.state == STATE_UNSYNCED
    assert head(clone) == mine  # refused rather than orphaning the pushed commit


# --------------------------------------------------------------------------- #
# scenario 3: worktrees                                                       #
# --------------------------------------------------------------------------- #


def test_worktree_leaves_the_default_branch_to_its_owner(clone: Path):
    """repo on main + repo.wt on a feature branch: only the owner moves main."""
    wt = clone.parent / "work.wt"
    git(clone, "worktree", "add", "-b", "feat/wt", str(wt))
    commit(wt, "wip.txt")
    main_tip = ref(clone, "refs/heads/main")
    land_upstream(clone)

    r = run(wt)

    assert r.action == ACT_REBASE
    assert r.state == STATE_REBASED
    assert r.main_ff == 0  # untouched: main is checked out in the other worktree
    assert ref(clone, "refs/heads/main") == main_tip
    # ...but the work was still replayed onto the fresh remote state
    assert ref(wt, "HEAD~1") == ref(wt, "refs/remotes/origin/main")
    assert ref(wt, "refs/remotes/origin/main") != main_tip


def test_worktree_and_owner_are_serialized(clone: Path):
    """Both are found by a scan; they must not run concurrently on one object store."""
    wt = clone.parent / "work.wt"
    git(clone, "worktree", "add", "-b", "feat/wt", str(wt))
    assert asyncio.run(common_dir(wt)) == asyncio.run(common_dir(clone))


# --------------------------------------------------------------------------- #
# scenario 4: detached HEAD and degenerate repos                              #
# --------------------------------------------------------------------------- #


def test_detached_head_is_probe_only(clone: Path):
    git(clone, "checkout", "--detach", "HEAD")
    tip = head(clone)
    land_upstream(clone)

    r = run(clone)

    assert r.action == ACT_DETECT
    assert r.branch == ""
    assert head(clone) == tip
    assert r.main_ff == 1  # main was not checked out, so it could still advance


def test_repo_without_remote_is_reported_not_failed(tmp_path: Path):
    solo = tmp_path / "solo"
    solo.mkdir()
    git(solo, "init", "--initial-branch=main")
    commit(solo, "a.txt")

    r = run(solo)

    assert r.state == STATE_NO_REMOTE
    assert r.ok
    assert not r.wants_session
    assert r.summary_line() == "- no remote"


def test_ignore_config_short_circuits(clone: Path):
    git(clone, "config", "tmuxpull.ignore", "true")
    land_upstream(clone)

    r = run(clone)

    assert r.state == STATE_IGNORED
    assert not r.wants_session
    assert r.summary_line() == "- ignored (git config tmuxpull.ignore)"
    # nothing ran at all, not even the fetch
    assert ref(clone, "refs/remotes/origin/main") != ref(seed_of(clone), "HEAD")


def test_unreachable_remote_fails_without_touching_the_repo(clone: Path):
    git(clone, "remote", "set-url", "origin", str(clone.parent / "does-not-exist.git"))
    tip = head(clone)

    r = run(clone)

    assert r.state == "fail"
    assert not r.ok
    assert r.needs_attention
    assert head(clone) == tip
    assert r.summary_line().startswith("! FAIL:")


# --------------------------------------------------------------------------- #
# report                                                                      #
# --------------------------------------------------------------------------- #


def test_report_lists_every_repo_and_flags_attention():
    clean = Result(
        repo=Repo(path=Path("/r/a"), name="a"),
        state=STATE_UPDATED,
        action=ACT_PULL,
        branch="main",
        default_branch="main",
        remote="origin",
        incoming=2,
        session="r/a",
    )
    broken = Result(
        repo=Repo(path=Path("/r/b"), name="b"),
        state=STATE_CONFLICT,
        action=ACT_REBASE,
        branch="feat/x",
        default_branch="main",
        remote="origin",
        conflict_files=["src/x.py"],
        session="r/b",
    )
    body = render_report([clean, broken], ["/r"])

    assert "# 2 repos, 1 need attention" in body
    assert "[updated  ] a  (main)  + 2 commits" in body
    assert "[conflict ] b  (feat/x)  ! CONFLICT" in body
    assert "conflicts: src/x.py" in body
    assert "tmux attach -t r/b" in body
