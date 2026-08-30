"""Generate bin/rebase-all.py (PEP 723 standalone script) from src/tmuxpull/__init__.py.

The module is the single source of truth. This script prepends the uv shebang
and PEP 723 metadata block, appends the __main__ guard, and writes the result
to bin/rebase-all.py. Run it after any change to the module:

    python scripts/gen_script.py

tests/test_script_sync.py asserts the generated file is up to date, so drift
fails the test suite.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "src" / "tmuxpull" / "__init__.py"
SCRIPT = ROOT / "bin" / "rebase-all.py"

HEADER = """\
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

FOOTER = """\


if __name__ == "__main__":
    main()
"""


def generate() -> str:
    return HEADER + MODULE.read_text(encoding="utf-8").rstrip("\n") + FOOTER


def main() -> None:
    content = generate()
    SCRIPT.write_text(content, encoding="utf-8", newline="\n")
    print(f"wrote {SCRIPT} ({len(content.splitlines())} lines)")


if __name__ == "__main__":
    main()
