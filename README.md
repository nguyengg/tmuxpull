# tmuxpull

Concurrent `git pull --rebase --autostash` across multiple Git repositories with tmux integration.

## Quick Start

### Run instantly with curl (no install)

```bash
# Python version — requires uv (https://docs.astral.sh/uv/)
curl -fsSL https://raw.githubusercontent.com/nguyengg/tmuxpull/main/bin/rebase-all.py | uv run - ~/Workspaces

# Zsh version — zero dependencies (just git + tmux)
curl -fsSL https://raw.githubusercontent.com/nguyengg/tmuxpull/main/bin/rebase-all | zsh -s -- ~/Workspaces
```

### Install with curl (one-liner)

```bash
# Download the self-contained script to ~/.local/bin
curl -fsSL https://raw.githubusercontent.com/nguyengg/tmuxpull/main/bin/rebase-all.py -o ~/.local/bin/tmuxpull && chmod +x ~/.local/bin/tmuxpull
tmuxpull ~/Workspaces
```

The script carries its own dependency metadata (PEP 723), so with `uv` on your PATH it bootstraps its own environment on first run — no venv, no pip install.

### Install from PyPI

```bash
# Install via pip/uv (recommended)
pip install tmuxpull
tmuxpull ~/Workspaces

# Or install as a uv tool
uv tool install tmuxpull
tmuxpull ~/Workspaces
```

Both scan for Git repos under the given directories, pull with rebase concurrently, print a summary per repo, and open tmux windows for repos that need your attention (conflicts, failures, etc.).

## Features

- **Concurrent execution** with configurable job limits
- **Smart repo discovery** with depth limits and noise filtering (skips `node_modules`, `.venv`, etc.)
- **Per-repo summaries** showing commits pulled and file change stats (Python version)
- **tmux integration** — opens windows for repos needing attention, landing on `git status`
- **Multiple modes**: attention-only (default), all repos, or no tmux
- **PEP 723 packaging** (Python) — zero-setup single file with dependencies declared inline

## Usage

```bash
tmuxpull [-d DEPTH] [-j JOBS] [--tmux {all,attn,off}] 
         [-s SESSION] [-v] [--dry-run] DIR [DIR ...]
```

### Options

- `-d, --max-depth N` — Directory search depth (default: 2)
- `-j, --jobs N` — Max concurrent rebases (default: min(8, 2×CPU))
- `-x, --exclude GLOB` — Skip repos whose name matches the glob (repeatable), e.g. `-x 'kirodotdev/*'`
- `--tmux {on,off}` — Create per-repo tmux sessions (default: on)
- `-v, --verbose` — Show commit subjects (-v = top 3, -vv = all)
- `--dry-run` — List repos that would be processed, then exit

### Ignoring a repo

Two ways to skip a repo:

```bash
# Sticky, per-repo (survives every run until unset) — e.g. a repo whose tip is broken:
git -C ~/github.com/kirodotdev/KiroCrew config tmuxpull.ignore true
# undo:
git -C ~/github.com/kirodotdev/KiroCrew config --unset tmuxpull.ignore

# One-off, per-run:
tmuxpull -x 'kirodotdev/*' ~/github.com
```

Ignored repos print `- ignored` in the summary and get no tmux session.

### Examples

```bash
# Morning sync across your workspace
tmuxpull ~/Workspaces ~/Projects

# High concurrency, all repos get tmux windows
tmuxpull -j 16 --tmux all ~/Code

# Just print what would happen
tmuxpull --dry-run ~/Projects

# Verbose output showing commit messages  
tmuxpull -v ~/Workspaces
```

## Output

Per-repo summary lines:
```
my-project        ✓ 3 commits  8 files changed, 213 insertions(+), 41 deletions(-)
other-repo        · up to date
broken-thing      ✗ FAIL: could not apply autostash
```

After the rebase finishes, if you're on a TTY you get an **interactive session picker**: use ↑/↓ (or `j`/`k`), Enter to `tmux attach` straight into the chosen session, `q`/Esc to skip. Failed repos are listed first and highlighted red so they're the natural first pick. Inside an existing tmux client this becomes `tmux switch-client` (nested `attach` is refused). When output is piped or redirected the picker is skipped and the full `tmux attach -t <name>` list is printed instead, so scripts and CI still work.

## Two Versions

### `src/tmuxpull/` + `bin/rebase-all.py` (single source of truth)

The PyPI package (`src/tmuxpull/__init__.py`) is the canonical implementation.
`bin/rebase-all.py` — the standalone PEP 723 script the curl one-liners use — is
**generated from it** (`python scripts/gen_script.py`, or `mise run gen-script`);
a test fails if the two drift, so they always ship identical behavior.

- Rich per-repo summaries with git log output and diffstat
- Better error handling and live progress reporting
- Structured data model for repo state

**Requirements**: Python 3.11+, tmux, git (plus [uv](https://docs.astral.sh/uv/) for the standalone script)

### `bin/rebase-all` (Fallback)

- **Pure Zsh** — no Python dependencies
- Same per-repo tmux sessions, same TTY session picker (arrow keys, failures
  first, Enter attach / switch-client, q or Esc skip, piped-mode list print)
- Same options: `-j`, `-d`, `-x`, `--tmux on/off`, `-v`/`-vv`, `--dry-run`,
  plus the `git config tmuxpull.ignore` per-repo opt-out
- Same summary shape: `+ N commits  <shortstat>`, `= up to date`, `! FAIL:`

> **Note on drift**: the Zsh port has been re-aligned to the Python version and
> now matches its feature set. The one deliberate difference: summaries print
> in input order at the end of the run (vs. Python's live completion-order
> `[n/N]` counter). If the two ever drift again, the Python version wins as
> source of truth.

**Requirements**: Zsh, tmux, git

## Installation

### From PyPI (Recommended)

```bash
# Install globally
pip install tmuxpull

# Or as a uv tool (isolated)
uv tool install tmuxpull
```

### From Source

```bash
# Clone and install
git clone https://github.com/nguyengg/tmuxpull.git
cd tmuxpull
pip install .

# Or for development
uv sync --dev
```

### Zsh Fallback

For machines without Python, use the dependency-free Zsh script:
```bash
chmod +x bin/rebase-all
ln -s $PWD/bin/rebase-all ~/.local/bin/
```

## Design

Finds Git repos by walking the filesystem looking for `.git` directories, up to a configurable depth. Prunes common noise directories (`node_modules`, build artifacts, Python venvs) to avoid slow traversals.

Rebases run concurrently via `asyncio` (Python) or Zsh job control, capped at a reasonable limit to avoid overwhelming git servers. Each repo is isolated — failures don't stop other repos.

The tmux integration is the key workflow piece: clean repos just print their summary and disappear, while repos needing intervention (conflict resolution, stash conflicts, etc.) open interactive windows where you can fix things. `tmux attach -t rebase` becomes your "work queue" for the morning.

## License

MIT