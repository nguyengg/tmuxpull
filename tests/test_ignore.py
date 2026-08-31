"""Tests for repo ignore mechanisms: -x/--exclude globs and Result.skipped."""
from __future__ import annotations

from pathlib import Path

from tmuxpull import Repo, Result, apply_excludes


def _repo(name: str) -> Repo:
    return Repo(path=Path("/fake") / name, name=name)


def test_apply_excludes_empty_patterns():
    repos = [_repo("a/x"), _repo("b/y")]
    kept, excluded = apply_excludes(repos, [])
    assert kept == repos
    assert excluded == []


def test_apply_excludes_exact_name():
    repos = [_repo("kirodotdev/KiroCrew"), _repo("nguyengg/xy3w")]
    kept, excluded = apply_excludes(repos, ["kirodotdev/KiroCrew"])
    assert [r.name for r in kept] == ["nguyengg/xy3w"]
    assert [r.name for r in excluded] == ["kirodotdev/KiroCrew"]


def test_apply_excludes_glob():
    repos = [_repo("kirodotdev/KiroCrew"), _repo("nguyengg/xy3w"), _repo("nguyengg/unrpaw")]
    kept, excluded = apply_excludes(repos, ["nguyengg/*"])
    assert [r.name for r in kept] == ["kirodotdev/KiroCrew"]
    assert len(excluded) == 2


def test_apply_excludes_multiple_patterns():
    repos = [_repo("a/one"), _repo("b/two"), _repo("c/three")]
    kept, excluded = apply_excludes(repos, ["a/*", "c/three"])
    assert [r.name for r in kept] == ["b/two"]
    assert len(excluded) == 2


def test_skipped_result_summary_and_flags():
    r = Result(repo=_repo("a/x"), returncode=0, stdout="", stderr="", skipped=True)
    assert r.ok
    assert not r.needs_attention
    assert not r.changed
    assert r.summary_line() == "- ignored (git config tmuxpull.ignore)"
