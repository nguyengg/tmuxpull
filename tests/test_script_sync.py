"""Assert bin/rebase-all.py is in sync with src/tmuxpull/__init__.py.

The standalone PEP 723 script is GENERATED from the module (the single source
of truth). If this test fails, run:

    python scripts/gen_script.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from gen_script import SCRIPT, generate  # noqa: E402


def test_standalone_script_is_in_sync():
    assert SCRIPT.exists(), f"{SCRIPT} missing -- run: python scripts/gen_script.py"
    actual = SCRIPT.read_text(encoding="utf-8")
    expected = generate()
    assert actual == expected, (
        "bin/rebase-all.py has drifted from src/tmuxpull/__init__.py.\n"
        "Regenerate it with: python scripts/gen_script.py"
    )
