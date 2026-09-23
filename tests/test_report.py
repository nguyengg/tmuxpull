"""Tests for the durable result trail: report file, attention block, session walk.

Picking a tmux session used to cost you the result list -- tmuxpull `exec`ed
tmux and never regained control. These cover the three things that replaced it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import tmuxpull
from tmuxpull import (
    ACT_DETECT,
    ACT_PULL,
    ACT_REBASE,
    STATE_CLEAN,
    STATE_CONFLICT,
    STATE_DIVERGED,
    STATE_UPDATED,
    Repo,
    Result,
    default_log_path,
    print_attention,
    render_report,
    walk_sessions,
    write_report,
)


def _result(name: str, state: str, **kw) -> Result:
    kw.setdefault("remote", "origin")
    kw.setdefault("default_branch", "main")
    return Result(repo=Repo(path=Path("/r") / name, name=name), state=state, **kw)


# --------------------------------------------------------------------------- #
# report file                                                                 #
# --------------------------------------------------------------------------- #


def test_default_log_path_follows_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert default_log_path() == tmp_path / "state" / "tmuxpull" / "last-run.log"


def test_default_log_path_falls_back_to_local_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert default_log_path() == Path.home() / ".local/state/tmuxpull/last-run.log"


def test_report_orders_attention_first(monkeypatch: pytest.MonkeyPatch):
    results = [
        _result("aaa-clean", STATE_CLEAN, action=ACT_PULL, branch="main"),
        _result("zzz-broken", STATE_CONFLICT, action=ACT_REBASE, branch="feat/z"),
        _result("mmm-diverged", STATE_DIVERGED, action=ACT_DETECT, branch="feat/m"),
    ]
    body = render_report(results, ["/r"])
    order = [ln.split("] ")[1].split()[0] for ln in body.splitlines() if ln.startswith("[")]
    assert order == ["mmm-diverged", "zzz-broken", "aaa-clean"]
    assert "# 3 repos, 2 need attention" in body


def test_report_records_branch_conflicts_and_attach_command():
    r = _result(
        "repo",
        STATE_CONFLICT,
        action=ACT_REBASE,
        branch="feat/x",
        conflict_files=["a.py", "b/c.py"],
        session="parent/repo",
    )
    body = render_report([r], ["/r"])
    assert "(feat/x)" in body
    assert "conflicts: a.py b/c.py" in body
    assert "tmux attach -t parent/repo" in body


def test_report_writes_and_creates_parent_dirs(tmp_path: Path):
    target = tmp_path / "nested" / "deeper" / "run.log"
    written = write_report(target, "hello\n")
    assert written == target
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_report_write_failure_is_not_fatal(tmp_path: Path, capsys: pytest.CaptureFixture):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file\n", encoding="utf-8")
    assert write_report(blocker / "sub" / "run.log", "x") is None
    assert "could not write report" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# attention block                                                             #
# --------------------------------------------------------------------------- #


def test_attention_block_lists_only_problem_repos(capsys: pytest.CaptureFixture):
    results = [
        _result("fine", STATE_UPDATED, action=ACT_PULL, branch="main", incoming=2),
        _result(
            "stuck",
            STATE_CONFLICT,
            action=ACT_REBASE,
            branch="feat/x",
            conflict_files=["a.py"],
            session="p/stuck",
        ),
    ]
    print_attention(results, Path("/tmp/report.log"))
    err = capsys.readouterr().err
    assert "1 of 2 repos need attention" in err
    assert "stuck" in err
    assert "tmux attach -t p/stuck" in err
    assert "fine" not in err
    assert "full report: /tmp/report.log" in err


def test_attention_block_still_points_at_the_report_when_all_clean(
    capsys: pytest.CaptureFixture,
):
    results = [_result("fine", STATE_CLEAN, action=ACT_PULL, branch="main")]
    print_attention(results, Path("/tmp/report.log"))
    err = capsys.readouterr().err
    assert "need attention" not in err
    assert "full report: /tmp/report.log" in err


# --------------------------------------------------------------------------- #
# session walk                                                                #
# --------------------------------------------------------------------------- #


class _FakeRun:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, check=False):  # noqa: ANN001 - subprocess.run stand-in
        self.calls.append(list(argv))
        return None


def test_walk_returns_to_the_picker_after_detach(monkeypatch: pytest.MonkeyPatch):
    """Detaching comes back here, so the queue can be walked repo by repo."""
    monkeypatch.delenv("TMUX", raising=False)
    picks = iter(["p/a", "p/b", None])
    monkeypatch.setattr(tmuxpull, "_pick_session", lambda names, failed: next(picks))
    runner = _FakeRun()
    monkeypatch.setattr(tmuxpull.subprocess, "run", runner)
    reprints: list[int] = []

    walk_sessions(["p/a", "p/b"], {"p/a"}, lambda: reprints.append(1))

    assert runner.calls == [
        ["tmux", "attach", "-t", "p/a"],
        ["tmux", "attach", "-t", "p/b"],
    ]
    assert len(reprints) == 2  # the list is reprinted every time control returns


def test_walk_skips_immediately_when_nothing_is_picked(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(tmuxpull, "_pick_session", lambda names, failed: None)
    runner = _FakeRun()
    monkeypatch.setattr(tmuxpull.subprocess, "run", runner)

    walk_sessions(["p/a"], set(), lambda: pytest.fail("should not reprint"))

    assert runner.calls == []


def test_walk_switches_client_and_stops_inside_tmux(monkeypatch: pytest.MonkeyPatch):
    """Nested attach is refused; switch-client returns at once, so do not loop."""
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    picks = iter(["p/a", "p/b", None])
    monkeypatch.setattr(tmuxpull, "_pick_session", lambda names, failed: next(picks))
    runner = _FakeRun()
    monkeypatch.setattr(tmuxpull.subprocess, "run", runner)

    walk_sessions(["p/a", "p/b"], set(), lambda: pytest.fail("should not reprint"))

    assert runner.calls == [["tmux", "switch-client", "-t", "p/a"]]
