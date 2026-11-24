# Changelog

All notable changes to `clean` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Keep complete output from the five most recent cleanup runs in `~/.clean/logs/`, including previews and scheduled runs.

## [0.5.0] - 2026-11-21

### Added

- Cleanup starts by showing the installed version and run mode. Interactive terminals use color for progress and outcomes. Set `NO_COLOR=1` for plain text.

### Changed

- Project cleanup reuses file metadata collected during discovery and rechecks older artifacts before removal. On a 24-workspace benchmark, removing 72 stale files took 5.42 seconds, down from 10.87 seconds.
- Gradle cleanup skips recursive open-file checks when a workspace has no Gradle build or cache to clean. Homebrew, pip, and uv previews skip measuring caches whose sizes do not contribute to the estimate.
- Scratch cleanup and disk usage reports scan large directory trees faster. On an 11,520-file benchmark, the scratch scan took 42 ms and the disk usage scan took 41 ms, down from 97 ms and 92 ms respectively.

## [0.4.0] - 2026-11-21

### Added

- Select workspaces with repeatable `--project` flags or `CLEAN_PROJECTS`, run individual cleanup phases, and use `--json` for per-item outcomes and byte counts. Age thresholds can be set through the environment.
- Schedule unattended cleanup on macOS with `clean schedule install`. The LaunchAgent logs available disk space and warns when it falls below the configured floor. Use `clean schedule status` to inspect the last run.
- Inspect memory pressure, swap usage, long-running temporary processes, and the size and age of developer tool homes. Docker Desktop storage reports show disk image sizes and volume headroom even when the engine is offline.
- Reclaim idle Lean `.lake/build` directories and individual profiles in shared Cargo target directories. Lean packages and active Cargo builds are preserved.
- Clean idle Claude scratchpads, macOS temporary entries, and scratch directories under `CLEAN_SCRATCH_ROOTS`. Preview groups candidates by name prefix. Cache cleanup now covers old npx entries, Go build caches, and sccache objects.

### Changed

- Cleanup requires macOS and Python 3.9 or later. Unattended or workspace-scoped runs skip global tmux cleanup, and worktree cleanup can skip GitHub when it is unavailable.

### Fixed

- `--check` preserves stray Python bytecode and includes it in potential savings. Preview and cleanup account for directory timestamps changed by earlier cleanup steps.
- Active projects retain their `node_modules` and `.next` directories. The activity check recognizes recent commits and authored files while ignoring background fetches and Loom bookkeeping.
- Projects used by MCP servers, linked CLIs, or Loom's `clean.keepBuildArtifacts` setting retain their runtime dependencies and build output. Use `CLEAN_KEEP_PROJECTS` to protect additional projects.
- Worktree and scratch cleanup preserve unpublished Git work, including additional local branches and stashes in full clones.

## [0.3.0] - 2026-07-08

Stops every known path by which `clean` could delete active work-in-progress
(observed repeatedly against lean-genius agent worktrees, which drove agents to
park worktrees under `$HOME` where no janitor manages them).

### Fixed

- **Staleness is now recursive activity, not root-dir mtime** — a directory's
  own mtime only changes when a direct child is added/removed/renamed, so a
  worktree being actively edited deep in its tree looked "stale" once the root
  dir passed the age threshold. Both the worktree pass (`WORKTREE_AGE_DAYS`)
  and the `/tmp` clone pass (`TMP_AGE_HOURS`) now use `find -mtime/-mmin
  -print -quit` over the whole tree (including `.git`, so commits/fetches count
  as activity).
- **`/tmp` clone cleanup now runs WIP checks** — previously a 1-hour-old
  scratch clone/worktree was `rm -rf`'d with **no** uncommitted/unpushed check
  at all. It now keeps anything with uncommitted/untracked files or commits not
  on a remote, and removes registered worktrees via `git worktree remove`
  (parent repo metadata stays consistent) instead of `rm -rf`.
- **Unpushed detection no longer requires an upstream** — a branch with
  local-only commits and no upstream configured (the standard agent-worktree
  state) previously skipped the unpushed check entirely and was deleted. With
  no upstream, HEAD must now be reachable from some remote ref, else the
  worktree is preserved.
- **No more `git worktree remove --force … || rm -rf` fallback** — if git
  refuses the removal (e.g. files appeared between our safety checks and the
  removal), the worktree is left in place for a future run instead of being
  force-deleted. Plain `rm -rf` is reserved for dirs that are not registered
  worktrees and have already passed the WIP checks; dirs that are not git
  trees at all (residue of a lost worktree registration) are always preserved
  and reported.
- **Active-worktree guard is symlink-safe** — worktree paths and candidate
  paths are normalized with `pwd -P`, so `/tmp` vs `/private/tmp` aliasing can
  no longer defeat the guard.
- **Unstatable dirs are kept, not deleted** — `is_older_than_days/hours`
  treated a failed `stat` as "infinitely old" (eligible for deletion); they now
  err on the side of keeping.
- **In-repo worktree cleanup was inert** — the `is_in_active_worktree` gate
  matched the main checkout's path prefix for every `.loom/worktrees/*` /
  `.claude/worktrees/*` candidate (they live inside the main checkout), so the
  pass never removed anything. The gate is replaced by the real protections
  (recursive activity + open handles + WIP), and the closed-issue path gains
  the same open-handle and unpushed guards.

## [0.2.0] - 2026-06-16

### Added

- **Broader worktree cleanup** (#1) — `.loom/worktrees/` and `.claude/worktrees/` are now both walked, every subdir (not just `issue-*`). Non-issue agent worktrees (`audit-*`, `mechanic-*`, `researcher-*`, `agent-*`, …) are removed once they're older than `WORKTREE_AGE_DAYS` (default 14) and have no uncommitted changes, no unpushed commits, no live process, and aren't inside an active worktree. Existing `issue-N` CLOSED-issue logic is preserved.
- **Structural `/tmp` clone detection** (#1) — replaces the fixed `pr-*`/`issue-*` name list with "any `/private/tmp/<dir>/` that is a git repo", so `<repo-name>-<number>` / `<repo-name>-<descriptor>` clones from `gh pr checkout`-style tooling are cleaned without enumerating naming schemes. Handles `.git` as either a directory (regular clone) or a file (`git worktree add`).

### Fixed

- **Worktree safety check now catches untracked files** (#4) — both worktree safety gates (`worktree_is_stale_and_safe` and the issue-`N` CLOSED-issue branch) replaced their `git diff --quiet` + `git diff --cached --quiet` pair with a single `git status --porcelain` empty check. The previous gate inspected only tracked-file changes, so an orphan worktree whose only contents were untracked files (e.g. an agent's `notes.md`, a scratch `.py`, an unstaged `.env`) silently passed safety and was removed. The new gate preserves any worktree with any reportable state (untracked, modified, staged, renamed). Default `--porcelain` excludes `.gitignore`-matched paths, so `node_modules`-style caches still flow through normal aging.

## [0.1.0] - 2026-06-05

Initial tagged release.

### Added

- **System cleanup** — Homebrew, npm, pnpm, pip, bun, uv caches; HuggingFace cache info; Docker (containers >24h, dangling images, unused volumes, build cache, optional full prune via `-a`); VS Code extension caches; system logs >30 days; stale `/tmp` clones >1h with `lsof` open-handle check.
- **Per-project cleanup** — recursive sweep of `node_modules`, `.next`, Python caches (`__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`) >14 days, stray `.pyc` files, Python venvs (`venv`, `.venv`, `.dev-venv`, `env`) >7 days, Rust `target/` dirs >7 days (via `cargo clean` where possible), Java/Kotlin `build/` and `.gradle/`, plus per-repo `git worktree prune` + `git gc --auto`.
- **Worktree cleanup** — removes worktrees and `feature/issue-*` branches for GitHub issues in `CLOSED` state, plus Loom tmux sessions (`loom-*`). Requires `gh` CLI.
- **Safety nets** — age thresholds, `lsof` open-handle check, active-worktree guard so files inside live worktrees are never touched, GitHub issue-state check before removing worktrees, tool-native cleanup commands preferred over `rm -rf`.
- **Flags** — `--check`/`-c` (dry run), `--aggressive`/`-a` (Docker full prune), `--system-only` (skip project sweep), `--version`/`-V`, `--help`/`-h`.
- **Project-dir auto-detection** — `CLEAN_PROJECT_DIR` env var, else first existing of `~/GitHub`, `~/Projects`, `~/repos`, `~/src`.
- **Installer** — `install.sh` symlinks `clean` into `/usr/local/bin` (or `~/.local/bin` fallback), idempotent, with `--uninstall` and `--force` modes.
- **Release tooling** — `scripts/version.sh` for `show`/`check`/`bump`/`set` with `--tag` mode that commits and creates an annotated git tag.

### Fixed

- Project `find` walks now prune `.loom/worktrees` and `.claude/worktrees` and use `-name`-based prune patterns (vs. `-path "*/.../*"`, which only fired after descending one level). Previously, running `clean` against `~/GitHub` containing a repo with hundreds of agent/loom worktrees would appear to hang at the Python stage as `find` traversed every worktree copy four times.
