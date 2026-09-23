# tmuxpull

Keep every Git repo under a directory in step with its remote, concurrently, so
merge conflicts surface **this morning** instead of at push time — with a tmux
session per repo for the ones that need you.

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

## What it does to each repo

The **default branch** (`main`, or whatever `origin/HEAD` points at) is the
reference point — never whatever happens to be checked out. Every repo gets a
`git fetch --prune` first, then:

| you are on | what happens | your worktree |
|---|---|---|
| the default branch | `git pull --rebase --autostash` — your unpushed commits replay onto the new upstream | rebased in place |
| a branch you never pushed | `git rebase --autostash <remote>/<default>` — nothing is published yet, so replaying is free | rebased in place |
| a branch you **have** pushed | detect only: the branch fast-forwards to its own upstream, and `git merge-tree` probes it against the new default branch | untouched |
| detached HEAD | detect only | untouched |

Two things happen regardless: the local default branch is **fast-forwarded by a
plain ref update, with no checkout**, so the next branch you cut is current; and
if another worktree has it checked out, it's left alone for that worktree's own
run.

### Why pushed branches are only probed

Rebasing commits that already exist on the remote means your next push needs
`--force-with-lease` — not something a batch tool should decide for you across a
dozen repos. `git merge-tree` answers "would this conflict?" entirely in memory:
no checkout, no index, nothing to clean up. It models a *merge*, so a clean
answer is a strong signal rather than a guarantee that replaying every commit is
clean.

Pass `--rebase-pushed` to rebase them for real. It refuses for any branch whose
upstream has commits you don't have — replaying only your side would orphan the
pushed ones.

### Conflicts are left in progress

A rebase that stops is **not** aborted. The repo keeps its conflict markers and
its in-progress rebase, and its tmux session is where you resolve it. That's the
point of the tool: clean repos print a line and disappear, broken ones become
your work queue.

## Usage

```bash
tmuxpull [-d DEPTH] [-j JOBS] [--tmux {on,off}] [--rebase-pushed]
         [--log PATH] [--no-log] [-v] [--dry-run] DIR [DIR ...]
```

### Options

- `-d, --max-depth N` — Directory search depth (default: 2)
- `-j, --jobs N` — Max concurrent repos (default: min(8, 2×CPU))
- `-x, --exclude GLOB` — Skip repos whose name matches the glob (repeatable), e.g. `-x 'kirodotdev/*'`
- `--tmux {on,off}` — Create per-repo tmux sessions (default: on)
- `--rebase-pushed` — Also rebase pushed branches (costs you a `--force-with-lease`)
- `--log PATH` — Write the run report here (default: `$XDG_STATE_HOME/tmuxpull/last-run.log`)
- `--no-log` — Don't write a report file
- `-v, --verbose` — Show incoming commit subjects (`-v` = top 3, `-vv` = all)
- `--dry-run` — List repos that would be processed, then exit
- `-V, --version` — Print the version and the file it's running from, then exit

### Am I running the latest?

```bash
$ tmuxpull --version
tmuxpull 0.2.0 (/home/you/.local/share/uv/tools/tmuxpull/lib/python3.14/site-packages/tmuxpull/__init__.py)
```

The path is there because one machine can easily have three copies on `PATH` —
a `pip install` into whichever Python was current, a `uv tool install`, and a
curl'd standalone script — and the version number alone won't tell you which one
just ran. The second field is always the bare version, so `tmuxpull --version |
awk '{print $2}'` is scriptable.

Compare against what's published:

```bash
curl -s https://pypi.org/pypi/tmuxpull/json | grep -o '"version":"[^"]*"' | head -1
```

Upgrading depends on how it was installed:

```bash
uv tool upgrade tmuxpull      # uv tool install
pip install -U tmuxpull       # pip install
```

A `pip install` into a version-managed Python (mise, pyenv, asdf) is worth
avoiding: the package lives under that exact interpreter, so the next Python
upgrade silently leaves it behind — or drops it off `PATH` entirely. `uv tool
install tmuxpull` keeps it in its own environment instead. The curl one-liners
need no upgrade at all: they read `main` directly, so they're current the moment
a fix lands.

### Per-repo git config

```bash
# Skip a repo entirely (survives every run until unset) — e.g. a broken tip:
git -C ~/github.com/kirodotdev/KiroCrew config tmuxpull.ignore true
git -C ~/github.com/kirodotdev/KiroCrew config --unset tmuxpull.ignore

# Override the detected default branch:
git -C ~/github.com/acme/legacy config tmuxpull.defaultBranch release
```

Default-branch detection order: `tmuxpull.defaultBranch`, then
`refs/remotes/<remote>/HEAD`, then a probe of `main` / `master` / `trunk`.
The remote is `origin` when present, otherwise the first one configured.

### Examples

```bash
# Morning sync across your workspace
tmuxpull ~/Workspaces ~/Projects

# High concurrency
tmuxpull -j 16 ~/Code

# Just print what would happen
tmuxpull --dry-run ~/Projects

# Verbose output showing incoming commit messages
tmuxpull -v ~/Workspaces
```

## Output

Per-repo summary lines, printed as each repo finishes:

```
on-main-ff        + 1 commit  1 file changed, 1 insertion(+)
on-main-replay    ~ rebased 1 onto main, pulled 1 commit  1 file changed, 1 insertion(+)
feat-unpushed     ~ rebased 1 onto main, main +1  1 file changed, 1 insertion(+)
up-to-date-repo   = up to date
feat-pushed       ! DIVERGES from main: 1 file would conflict (main +1)
feat-conflict     ! CONFLICT: rebase stopped, 1 file -- resolve here (main +1)
broken-remote     ! FAIL: could not read from remote repository
quiet             - ignored (git config tmuxpull.ignore)
no-remote         - no remote
```

`!` lines go to stderr and mean you have to act; `+`/`~`/`=`/`-` go to stdout.
The exit code is 1 when anything needs attention.

### The result list outlives the handoff

Choosing a tmux session used to cost you the summary — the process was replaced
by tmux and nothing survived a detach. Now three things persist it:

1. **An attention block on stderr** before tmux takes the terminal, so it stays
   in the launching shell's scrollback:

   ```
   2 of 9 repos need attention:
     feat-pushed    ! DIVERGES from main: 1 file would conflict (main +1)
                    tmux attach -t Projects/feat-pushed
     feat-conflict  ! CONFLICT: rebase stopped, 1 file -- resolve here (main +1)
                    tmux attach -t Projects/feat-conflict
   full report: ~/.local/state/tmuxpull/last-run.log
   ```

2. **A report file**, always written, whether or not you pick a session — every
   repo, attention first, greppable by state (`grep '^\[conflict' last-run.log`).
   It survives closing the terminal entirely.

3. **A picker you come back to.** tmux runs as a child process, so detaching
   returns you to the picker with the list reprinted: fix one repo, detach, pick
   the next, `q` when you're done. Inside an existing tmux client this becomes
   `tmux switch-client` (nested `attach` is refused) and control does not return.

When output is piped or redirected the picker is skipped and the full
`tmux attach -t <name>` list is printed instead, so scripts and CI still work.

## Worktrees

A repo and its worktrees (`repo` + `repo.wt/feat`) are separate directories, so a
scan finds both — but they share **one object store and one ref namespace**.
tmuxpull groups repos by `git rev-parse --git-common-dir` and serializes each
group while running different groups in parallel, so concurrent jobs can't race
on ref locks or `FETCH_HEAD`.

## Two Versions

### `src/tmuxpull/` + `bin/rebase-all.py` (single source of truth)

The PyPI package (`src/tmuxpull/__init__.py`) is the canonical implementation.
`bin/rebase-all.py` — the standalone PEP 723 script the curl one-liners use — is
**generated from it** (`python scripts/gen_script.py`, or `mise run gen-script`);
a test fails if the two drift.

**Requirements**: Python 3.11+, git 2.38+ (for `merge-tree --write-tree`), tmux,
plus [uv](https://docs.astral.sh/uv/) for the standalone script

### `bin/rebase-all` (Fallback)

- **Pure Zsh** — no Python dependencies
- Same branch handling, same report file, same attention block, same picker
- Same options, including `--rebase-pushed`, `--log` / `--no-log`, and both
  `git config tmuxpull.*` knobs
- `tests/test_zsh_parity.py` builds one fixture per implementation and asserts
  their summary lines are identical, so the two cannot drift silently

The one deliberate difference: summaries print in input order at the end of the
run (vs. Python's live completion-order `[n/N]` counter). If the two ever
disagree otherwise, the Python version is the source of truth.

**Requirements**: Zsh, git 2.38+, tmux

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

Finds Git repos by walking the filesystem looking for `.git` entries, up to a
configurable depth. Prunes common noise directories (`node_modules`, build
artifacts, Python venvs) to avoid slow traversals.

Work runs concurrently via `asyncio` (Python) or Zsh job control, capped to
avoid overwhelming git servers, and serialized per shared object store. Each
repo is isolated — one failure doesn't stop the others.

The tmux integration is the key workflow piece: clean repos just print their
summary and disappear, while repos needing intervention (conflict resolution,
diverged branches) open interactive sessions where you fix things. The picker is
your morning work queue, and the report file is what's left of it after you close
the terminal.

## License

MIT
