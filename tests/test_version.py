"""The version must be one number, reported the same way by every copy.

There are four places it could drift: the module, the packaging metadata, the
generated standalone script, and the zsh port. `__version__` is the source of
truth (pyproject reads it via hatch's dynamic version) and these pin the rest
to it.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import tmuxpull
from tmuxpull import __version__, version_line

ROOT = Path(__file__).resolve().parent.parent
ZSH_SCRIPT = ROOT / "bin" / "rebase-all"
STANDALONE = ROOT / "bin" / "rebase-all.py"


def _env() -> dict[str, str]:
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    return env


def test_version_is_a_release_number():
    assert re.fullmatch(r"\d+\.\d+\.\d+([a-z0-9.]*)?", __version__), __version__


def test_packaging_metadata_matches_the_module():
    """Proves pyproject's hatch dynamic version is wired to __version__."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        installed = version("tmuxpull")
    except PackageNotFoundError:
        pytest.skip("tmuxpull is not installed in this environment")
    assert installed == __version__


def test_pyproject_does_not_hardcode_a_second_version():
    body = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in body
    assert '[tool.hatch.version]' in body
    assert not re.search(r'(?m)^version = ', body), "a literal version would drift"


def test_version_line_names_the_running_file():
    line = version_line()
    assert line.startswith(f"tmuxpull {__version__} (")
    assert str(Path(tmuxpull.__file__).resolve()) in line


def test_cli_version_flag_prints_and_exits_zero():
    for flag in ("--version", "-V"):
        proc = subprocess.run(
            [sys.executable, "-m", "tmuxpull", flag],
            capture_output=True,
            text=True,
            env=_env(),
        )
        assert proc.returncode == 0, proc.stderr
        out = (proc.stdout + proc.stderr).strip()
        assert out.startswith(f"tmuxpull {__version__} ")
        # the second field is the bare version, so `--version | awk '{print $2}'` works
        assert out.split()[1] == __version__


def test_standalone_script_carries_the_same_version():
    body = STANDALONE.read_text(encoding="utf-8")
    assert f'__version__ = "{__version__}"' in body


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not available")
def test_zsh_port_reports_the_same_version():
    proc = subprocess.run(
        [str(ZSH_SCRIPT), "--version"], capture_output=True, text=True, env=_env()
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    assert out.startswith(f"tmuxpull {__version__} (")
    assert out.split()[1] == __version__
