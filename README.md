# clean

Aggressive but safe disk cleanup for macOS developer machines. Designed for developers who work with lots of git worktrees.

Cleans system caches, Docker resources, per-project build artifacts, and stale worktrees/branches for closed GitHub issues — with multiple safety nets to prevent data loss.

## What it cleans

**System-level:**
- Homebrew, npm, pnpm, pip, bun, uv caches
- HuggingFace cache (info only — interactive cleanup recommended)
- Docker (containers >24h, dangling images, unused volumes, build cache)
- VS Code extension caches
- System logs >30 days old
- Stale `/tmp` clones from dev tools (PR reviews, issue work) — only when idle,
  with no open handles and no uncommitted/unpushed work

**Per-project (auto-detected):**
- `node_modules`, `.next` — only when the project itself has been idle >7 days
- Python caches (`__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`) >14 days
- Python virtualenvs (`venv`, `.venv`) >7 days
- Rust `target/` directories >7 days
- Java/Kotlin `build/`, `.gradle/`
- Git maintenance (`gc --auto`, `worktree prune`)

**Worktree cleanup (requires [gh CLI](https://cli.github.com)):**
- Removes worktrees for closed GitHub issues (`.loom/worktrees/issue-*`, `.claude/worktrees/issue-*`)
- Removes stale agent-workflow worktrees (everything else under those roots) once
  idle >14 days with no WIP
- Deletes local feature branches for closed issues (`feature/issue-*`)
- Kills stale Loom tmux sessions (`loom-*`)

## Safety nets

1. **Age thresholds** — never deletes trees with recent activity. Recursive for
   git trees: an edit *anywhere* in the tree counts, not just the root dir's mtime. For
   `node_modules`/`.next` the age test is applied to the **project**, not to the
   artifact: a `node_modules` installed months ago and built against hourly is
   in use, and its own mtime cannot tell you that
2. **Open-handle check** (`lsof`) — skips directories with active processes
3. **Active worktree check** — never touches files inside live git worktrees
   (symlink-safe, so `/tmp` vs `/private/tmp` aliasing can't defeat it)
4. **WIP check** — never deletes uncommitted/untracked files or commits that no
   remote ref contains, including on branches with no upstream configured
5. **GitHub issue check** — only cleans issue worktrees/branches for CLOSED issues
6. **Tool-native commands** — uses `cargo clean`, `uv cache prune`, etc. over `rm -rf`;
   registered worktrees are removed via `git worktree remove` (no `--force`, no
   `rm -rf` fallback), so git can veto if state changed since the safety checks
7. **Pattern matching** — only known cache/temp patterns, never source code
8. **Runtime projects** — a project that another program runs in place keeps
   its `node_modules`, `target/`, venvs and `build/` whatever their age. That
   covers MCP servers named in `~/.claude.json` (user and per-project),
   every project's `.mcp.json`, Claude Desktop, Cursor and Codex
   `[mcp_servers.*]`; CLIs symlinked into `~/.local/bin`, `~/bin`,
   Homebrew's `bin` or the global `node_modules` (`npm link`); and workspaces
   that set Loom's `clean.keepBuildArtifacts`. Neither the age test nor `lsof`
   can see this dependency: a running MCP server holds no open handle on
   modules it has already loaded. Add others with `CLEAN_KEEP_PROJECTS`

## Install

```bash
git clone https://github.com/rjwalters/clean.git ~/GitHub/clean
cd ~/GitHub/clean
./install.sh
```

The installer symlinks `clean` into `/usr/local/bin` (or `~/.local/bin` if that's not writable). Re-run `./install.sh` anytime to repair or move the install — it's idempotent.

To update, just `git pull` in the repo. The symlink keeps pointing at the live script, so no reinstall is needed.

To uninstall: `./install.sh --uninstall`.

## Usage

```bash
clean              # Run cleanup
clean --check      # Preview what would be cleaned (dry run); short form -c (-a for --aggressive)
clean --aggressive # Also run 'docker system prune -af --volumes'
clean --system-only # Skip per-project cleanup
clean --help       # Show detailed help
```

## Project directory

The script auto-detects your project directory by checking (in order):

1. `CLEAN_PROJECT_DIR` environment variable
2. `~/GitHub`
3. `~/Projects`
4. `~/repos`
5. `~/src`

Override with:
```bash
CLEAN_PROJECT_DIR=~/code clean
```

## Scheduled fleet runs

Select exact workspaces with repeatable `--project DIR` flags or a colon-separated
`CLEAN_PROJECTS` list. Both forms can be combined. Select a single phase with
`--projects-only`, `--worktrees-only` or `--system-only`. The default runs all
three phases. The system phase operates on host caches. Global tmux sessions
are skipped in scoped or unattended runs. Worktree cleanup skips GitHub lookups
when authentication or the network is unavailable.

```sh
clean --projects-only --project /work/app --project '/work/tool kit' --check --json
CLEAN_PROJECTS=/work/app:/work/tool CLEAN_TARGET_AGE_DAYS=14 clean --projects-only --json
```

With `--json`, progress goes to stderr and stdout contains one JSON object
with `exit_code`, `check`, `bytes_freed`, `bytes_reclaimable` and `items`.
Each item has a path, reason, byte count and status (`removed`, `would_remove`,
`kept`, `skipped` or `failed`). Totals use measured allocated sizes and tool
reports. A native prune with no available estimate contributes zero bytes.
Failed cleanup steps return nonzero. Invalid options, unsupported platforms,
and unavailable prerequisites exit before cleanup. Rust targets in active
projects remain protected regardless of size.

## Configuration

Override thresholds through the environment. Use positive integers from 1 to 99999.

| Variable | Default | Description |
|---|---|---|
| `CLEAN_VENV_AGE_DAYS` | `7` | Min age before cleaning virtualenvs |
| `CLEAN_NODE_MODULES_AGE_DAYS` | `7` | Min idle time (recursive, of the **project**) before cleaning `node_modules`/`.next` |
| `CLEAN_TMP_AGE_HOURS` | `1` | Min idle time (recursive) before cleaning /tmp clones |
| `CLEAN_WORKTREE_AGE_DAYS` | `14` | Min idle time (recursive) before cleaning agent worktrees |
| `CLEAN_PYCACHE_AGE_DAYS` | `14` | Min age before cleaning Python caches |
| `CLEAN_TARGET_AGE_DAYS` | `7` | Min age before cleaning Rust target dirs |

To keep a project's artifacts that detection misses, set
`CLEAN_KEEP_PROJECTS=name1:name2` (top-level names under the project
directory). Each run lists the runtime projects it found and why, under
"Runtime projects".

Tests: `/bin/bash tests/test-runtime-projects.sh` (hermetic: throwaway `HOME` and
project dir).

## Project activity, Lean, and shared Cargo builds

For git checkouts, the project idle window includes the latest commit and
recently edited tracked or non-ignored files. The age check skips background
fetches, Loom runtime metadata, and generated build trees. A preview prints the path
or commit that kept a project active. Other directories retain a recursive
mtime check.

The project pass finds nested Lean packages and reclaims their `.lake/build`
once the owning checkout is idle for `CLEAN_LAKE_AGE_DAYS` (default 7) and
has no open handles. It preserves `.lake/packages`. With an explicit workspace
list, select a nested worktree separately to clean its artifacts.

The system pass reports the allocated size of a shared Cargo target configured
by `CARGO_TARGET_DIR`, `CARGO_BUILD_TARGET_DIR`, or `build.target-dir` in
the Cargo home configuration. It recognizes Cargo-tagged target trees and
prunes each idle profile independently, including named profiles, target
triples, and task-specific target trees. The age threshold is
`CLEAN_TARGET_AGE_DAYS`. An active Cargo or rustc process, recent activity
anywhere in the profile, a nested checkout, or open handles keep the profile.
In a primary git checkout, the project pass also recognizes idle local Cargo targets
and Python virtual environments by their Cargo cache tag or `pyvenv.cfg`. Linked
checkouts keep their local Cargo and venv output. These build passes check for
tracked or non-ignored work, nested repositories and mounted filesystems.

Config-file discovery uses Python’s TOML reader or `tomli`, including the copy
shipped with the macOS developer tools’ Python 3.9.

## Worktree workflow

This tool is built for workflows that use git worktrees for issue-based development (e.g., [Loom](https://github.com/rjwalters/loom)). The worktree cleanup section:

- Scans each repo's `.loom/worktrees/` and `.claude/worktrees/` directories
- For `issue-N` worktrees: checks the GitHub issue via `gh issue view` and only
  removes worktrees where the issue is **CLOSED**
- For everything else (agent scratch worktrees): only removes after
  `WORKTREE_AGE_DAYS` of no activity anywhere in the tree
- Always keeps worktrees with uncommitted changes or commits not on any remote
  (even when the branch has no upstream), or with a live process inside
- Directories that aren't git trees at all are reported and never deleted
  (they may be residue of a lost worktree registration)
- Cleans up orphaned `feature/issue-*` branches for closed issues

## System diagnostics

The system phase reports swap and memory pressure, executables with more than
50 processes, temporary executables running longer than six hours, and the
five processes with the highest RSS. It reports the combined RSS and oldest
process age for each large group. Process inspection uses executable names
and paths, without printing process arguments.

The summary lists allocated disk space, newest modification time, and counts
of files older than 30 and 90 days in known tool homes, including Codex
sessions and history databases, Claude projects, Loom vendored runtime state,
Elan, Rustup and individual directories under `~/.local/state`. It offers
toolchain review commands and flags vendored versions with no detected
configuration or active executable reference. All these reports are read-only.

For Docker Desktop, the summary reads `DataFolder` from `settings-store.json`,
shows allocated image sizes and the free space on that volume even when the
engine is down. It looks for other `Docker.raw` images in the default directory
and Spotlight's index. For unindexed former locations, set
`CLEAN_DOCKER_SEARCH_ROOTS` to a colon-separated list of directories to scan.
Images outside the configured data directory include their modification date
for manual review.

## Scheduled cleanup on macOS

```sh
clean schedule install --interval-hours 24
clean schedule status
clean schedule uninstall
```

Installation loads a per-user LaunchAgent. The default interval is 24 hours.
The agent runs ordinary cleanup with stdin closed and writes to
`~/Library/Logs/clean.log`. Status includes the last completed run, exit code
and free space. The log records free space before and after each run.
Set `CLEAN_LOW_DISK_GIB` (default 30) before installation to change the warning
threshold shared by the schedule and Docker report. Reinstall to update the
interval or captured cleanup settings. Scheduling and system diagnostics
require Python 3.

## Requirements

- **Python 3.9+** for workspace discovery and summaries
- **macOS** (uses `stat -f`, `~/Library` paths, macOS `date` flags)
- **gh CLI** (optional, for worktree/branch cleanup)

## License

MIT
