# tmuxpull

Concurrent `git pull --rebase --autostash` across multiple Git repositories with tmux integration.

## Quick Start

```bash
# Install via pip/uv (recommended)
pip install tmuxpull
tmuxpull ~/Workspaces

# Or install as a uv tool
uv tool install tmuxpull
tmuxpull ~/Workspaces

# Zsh version (fallback) - no dependencies
chmod +x bin/rebase-all  
./bin/rebase-all ~/Workspaces
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
- `--tmux {all,attn,off}` — tmux windows for: all repos, attention-only (default), or none
- `-s, --session NAME` — tmux session name (default: "rebase") 
- `-v, --verbose` — Show commit subjects (-v = top 3, -vv = all)
- `--dry-run` — List repos that would be processed, then exit

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

Failed repos open tmux windows in the "rebase" session (or `-s NAME`), landing on `git status` so you see what's broken. Attach with `tmux attach -t rebase`.

## Two Versions

### `bin/rebase-all.py` (Recommended)

- **Standard Python package** with proper console script entry point
- Rich per-repo summaries with git log output and diffstat
- Better error handling and progress reporting
- Structured data model for repo state

**Requirements**: Python 3.11+, tmux, git

### `bin/rebase-all` (Fallback)

- **Pure Zsh** — no Python dependencies
- Basic summaries (commit count only, no diffstat)
- Simpler concurrency model with job control

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