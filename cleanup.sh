#!/bin/bash

# clean — aggressive but safe disk cleanup for macOS dev machines
#
# Designed for developers who work with lots of git worktrees.
# Cleans system caches, Docker resources, per-project build artifacts,
# and stale worktrees/branches for closed GitHub issues.
#
# Safety nets:
#   1. Age thresholds — never delete files modified within N days
#      (recursive for git trees: activity anywhere in the tree counts)
#   2. Open-handle check (lsof) — never delete dirs with active processes
#   3. Active worktree check (symlink-safe) — never delete inside live worktrees
#   4. WIP check — never delete uncommitted or unpushed git work, including
#      commits on branches that have no upstream configured
#   5. GitHub issue check — only clean issue worktrees for CLOSED issues
#   6. Tool-native commands (cargo clean, uv cache prune) over rm -rf;
#      registered worktrees removed via `git worktree remove` (no --force,
#      no rm -rf fallback) so git can veto if state changed under us
#   7. Pattern matching — only known cache/temp patterns, never source
#   8. Runtime projects — a project an MCP config, a linked CLI or Loom's
#      clean.keepBuildArtifacts says is run in place keeps all its artifacts

set -e

VERSION="0.5.0"

# Auto-detect project directory: use CLEAN_PROJECT_DIR env var, or fall back
# to ~/GitHub, ~/Projects, ~/repos, ~/src (first that exists).
if [ -n "$CLEAN_PROJECT_DIR" ]; then
    PROJECT_DIR="$CLEAN_PROJECT_DIR"
else
    PROJECT_DIR=""
    for candidate in "$HOME/GitHub" "$HOME/Projects" "$HOME/repos" "$HOME/src"; do
        if [ -d "$candidate" ]; then
            PROJECT_DIR="$candidate"
            break
        fi
    done
fi

# Resolve install.sh's symlink before loading repository helpers.
script_path=${BASH_SOURCE[0]}
while [ -L "$script_path" ]; do
    link=$(readlink "$script_path")
    case "$link" in /*) script_path=$link ;; *) script_path="$(dirname "$script_path")/$link" ;; esac
done
SCRIPT_DIR="$(cd "$(dirname "$script_path")" && pwd -P)"

VENV_AGE_DAYS=${CLEAN_VENV_AGE_DAYS:-7}
TMP_AGE_HOURS=${CLEAN_TMP_AGE_HOURS:-1}
PYCACHE_AGE_DAYS=${CLEAN_PYCACHE_AGE_DAYS:-14}
TARGET_AGE_DAYS=${CLEAN_TARGET_AGE_DAYS:-7}
NODE_MODULES_AGE_DAYS=${CLEAN_NODE_MODULES_AGE_DAYS:-7}
WORKTREE_AGE_DAYS=${CLEAN_WORKTREE_AGE_DAYS:-14}
LAKE_AGE_DAYS=${CLEAN_LAKE_AGE_DAYS:-7}
SHARED_CARGO_SUMMARY=""
CHECK_MODE=false
AGGRESSIVE_MODE=false
JSON_MODE=false
RUN_SYSTEM=true
RUN_PROJECTS=true
RUN_WORKTREES=true
EXPLICIT_PROJECTS=false
PROJECTS=()
ACTIVE_WORKTREES=""
PHASE=""
# Parse arguments
if [ "${1:-}" = schedule ]; then
    shift
    exec python3 -B "$SCRIPT_DIR/scripts/schedule.py" "$@"
fi

if [ "$1" = "--version" ] || [ "$1" = "-V" ]; then
    echo "clean $VERSION"
    exit 0
fi

if [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    echo "clean $VERSION — aggressive but safe disk cleanup for macOS dev machines"
    echo ""
    echo "Usage: clean [options]"
    echo "       clean schedule install [--interval-hours N] | status | uninstall"
    echo ""
    echo "Designed for developers working with lots of git worktrees."
    echo ""
    echo "System cleanup:"
    echo "  • Homebrew, npm, pnpm, pip, bun, uv caches"
    echo "  • HuggingFace cache (info only — interactive cleanup recommended)"
    echo "  • Docker (containers >24h old, dangling images, unused volumes, build cache)"
    echo "  • Stale /tmp clones (>$TMP_AGE_HOURS h idle, no open handles, no uncommitted/unpushed work)"
    echo "  • VS Code extension caches"
    echo "  • System logs >30 days old"
    echo ""
    echo "Per-project cleanup:"
    echo "  • node_modules, .next (projects idle >$NODE_MODULES_AGE_DAYS days)"
    echo "  • Python caches (__pycache__, .pytest_cache, .ruff_cache) >$PYCACHE_AGE_DAYS days"
    echo "  • Python venvs (root + subdirs) >$VENV_AGE_DAYS days"
    echo "  • Rust target/ dirs (root + monorepo subdirs) >$TARGET_AGE_DAYS days"
    echo "  • Java/Kotlin build/, .gradle/"
    echo "  • git gc --auto + git worktree prune"
    echo ""
    echo "Worktree cleanup (requires gh CLI):"
    echo "  • Worktrees for closed GitHub issues (.loom/worktrees/issue-*, .claude/worktrees/issue-*)"
    echo "  • Stale agent-workflow worktrees (.loom/worktrees/*, .claude/worktrees/*)"
    echo "    — only if idle >$WORKTREE_AGE_DAYS days (recursive — activity anywhere in the"
    echo "    tree counts), clean, fully on a remote (unpushed commits kept even with"
    echo "    no upstream configured), and no live process"
    echo "  • Feature branches for closed issues (feature/issue-*)"
    echo "  • Loom tmux sessions (loom-*)"
    echo ""
    echo "Safety nets:"
    echo "  • Age thresholds (recursive for git trees; configurable at top of script)"
    echo "  • Open-handle check via lsof (skips dirs with active processes)"
    echo "  • Active worktree check, symlink-safe (never touches files inside live worktrees)"
    echo "  • WIP check (never deletes uncommitted or unpushed git work)"
    echo "  • GitHub issue status check (only cleans issue worktrees for CLOSED issues)"
    echo "  • Tool-native commands when possible (cargo clean, uv cache prune);"
    echo "    registered worktrees removed via git worktree remove, never rm -rf"
    echo "  • Runtime projects (MCP servers in Claude/Codex/Cursor configs, CLIs"
    echo "    linked onto PATH or npm-linked, Loom keepBuildArtifacts) keep their"
    echo "    node_modules, target/, venvs and build/ regardless of age"
    echo ""
    echo "Options:"
    echo "  -h, --help         Show this help message"
    echo "  -V, --version      Print version and exit"
    echo "  -c, --check        Check how much space would be freed (dry run)"
    echo "  -a, --aggressive   Also run 'docker system prune -af --volumes'"
    echo "  --system-only | --projects-only | --worktrees-only    Select one phase"
    echo "  --project DIR      Select a workspace (repeatable, or set CLEAN_PROJECTS)"
    echo "  --json             Write a JSON summary to stdout and progress to stderr"
    echo ""
    echo "Environment:"
    echo "  CLEAN_PROJECT_DIR  Override project directory (default: auto-detect"
    echo "                     ~/GitHub, ~/Projects, ~/repos, ~/src)"
    echo "  CLEAN_KEEP_PROJECTS  Colon-separated project names whose artifacts"
    echo "                     are never cleaned (e.g. squad:my-mcp-server)"
    echo ""
    echo "Examples:"
    echo "  clean                              # Run cleanup"
    echo "  clean --check                      # Preview what would be cleaned"
    echo "  clean --aggressive                 # Include destructive Docker prune"
    echo "  CLEAN_PROJECT_DIR=~/code clean     # Custom project directory"
    exit 0
fi

die() { echo "clean: $*" >&2; exit 2; }

[ "$(uname -s)" = Darwin ] || die "cleanup requires macOS; no cleanup phases were run"
for value in "$VENV_AGE_DAYS" "$TMP_AGE_HOURS" "$PYCACHE_AGE_DAYS" "$TARGET_AGE_DAYS" "$NODE_MODULES_AGE_DAYS" "$WORKTREE_AGE_DAYS" "$LAKE_AGE_DAYS"; do
    [[ "$value" =~ ^[1-9][0-9]{0,4}$ ]] || die "age thresholds must be positive decimal integers (1–99999)"
done

add_project() {
    local path existing
    [ -n "$1" ] && path=$(cd -- "$1" && pwd -P) || die "invalid project directory '$1'"
    [ "$path" != / ] || die "the filesystem root cannot be a project"
    for existing in "${PROJECTS[@]}"; do
        [ "$path" != "$existing" ] || return 0
    done
    PROJECTS+=("$path")
}
if [ "${CLEAN_PROJECTS+x}" ]; then
    EXPLICIT_PROJECTS=true
    case ":$CLEAN_PROJECTS:" in *::*) die "CLEAN_PROJECTS contains an empty path" ;; esac
    IFS=: read -r -a paths <<< "$CLEAN_PROJECTS"
    for project in "${paths[@]}"; do add_project "$project"; done
fi
while [ "$#" -gt 0 ]; do
    case "$1" in
        --check|-c) CHECK_MODE=true ;;
        --aggressive|-a) AGGRESSIVE_MODE=true ;;
        --json) JSON_MODE=true ;;
        --project)
            [ "$#" -ge 2 ] || die "--project requires a directory"
            EXPLICIT_PROJECTS=true; add_project "$2"; shift ;;
        --system-only|--projects-only|--worktrees-only)
            [ -z "$PHASE" ] || [ "$PHASE" = "$1" ] || die "select only one cleanup phase"
            PHASE=$1 ;;
        *) die "unknown option '$1'" ;;
    esac
    shift
done
case "$PHASE" in
    --system-only) RUN_PROJECTS=false; RUN_WORKTREES=false ;;
    --projects-only) RUN_SYSTEM=false; RUN_WORKTREES=false ;;
    --worktrees-only) RUN_SYSTEM=false; RUN_PROJECTS=false ;;
esac

if [ "$EXPLICIT_PROJECTS" = false ]; then
    if [ -n "$PROJECT_DIR" ]; then
        [ -d "$PROJECT_DIR" ] || die "invalid CLEAN_PROJECT_DIR '$PROJECT_DIR'"
        for project in "$PROJECT_DIR"/*; do
            [ -d "$project" ] && [ ! -L "$project" ] || continue
            add_project "$project"
        done
    elif [ -n "$PHASE" ] && [ "$RUN_SYSTEM" = false ]; then
        die "select a workspace with --project or CLEAN_PROJECT_DIR"
    fi
fi

# Git hooks export paths which would override every subsequent git -C.
if command -v git >/dev/null; then
    git_env=$(git rev-parse --local-env-vars) || die "cannot identify repository-local Git environment"
    for key in $git_env; do unset "$key"; done
    unset GIT_CEILING_DIRECTORIES GIT_DISCOVERY_ACROSS_FILESYSTEM
fi
python3 -B -c 'pass' >/dev/null 2>&1 || die "Python 3 is required for cleanup"
# One private, ephemeral journal also captures events in piped shell loops.
run_dir=$(mktemp -d -t clean-events)
CLEAN_EVENT_FILE="$run_dir/events"
: > "$CLEAN_EVENT_FILE"
export CLEAN_EVENT_FILE
exec 3>&1
[ "$JSON_MODE" = false ] || exec 1>&2
finish() {
    local result=$?
    trap - EXIT
    python3 -B "$SCRIPT_DIR/scripts/summary.py" "$CLEAN_EVENT_FILE" "$JSON_MODE" "$CHECK_MODE" "$result" >&3 || result=$?
    rm -f "$CLEAN_EVENT_FILE" "$CLEAN_EVENT_FILE.candidates" "$run_dir/activity.json" "$run_dir/activity.json.dirs"
    rm -rf -- "$run_dir/npm"
    rmdir "$run_dir"
    exit "$result"
}
trap finish EXIT
source "$SCRIPT_DIR/scripts/terminal.sh"
record_item() { printf '%s\0' "$1" "$2" "$3" "${4:-0}" >> "$CLEAN_EVENT_FILE"; }
step_failed() { term_line failed "  Failed" "  $1 ($2)" >&2; record_item failed "$1" "$2"; }
keep_item() { term_line kept "  Kept" "  $1 ($2)"; record_item kept "$1" "$2"; }

if [ "$CHECK_MODE" = true ]; then
    export GIT_OPTIONAL_LOCKS=0  # git status may otherwise refresh a worktree index
    run_mode="Dry run"
else
    run_mode="Disk cleanup"
fi
[ "$AGGRESSIVE_MODE" = false ] || run_mode="$run_mode (aggressive)"
term_line heading "clean $VERSION" "  ·  $run_mode"
echo ""

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

if ! command -v timeout >/dev/null; then
    timeout() {
        python3 -B -c 'import subprocess, sys
try:
    sys.exit(subprocess.run(sys.argv[2:], timeout=int(sys.argv[1])).returncode)
except subprocess.TimeoutExpired:
    sys.exit(124)' "$@"
    }
fi

get_size() {
    du -sh "$1" 2>/dev/null | cut -f1
}

get_size_kib() {
    local usage
    usage=$(du -sk "$1") || return 1
    echo "${usage%%[[:space:]]*}"
}

bytes_to_human() {
    local bytes=$1
    if [ $bytes -lt 1024 ]; then
        echo "${bytes}K"
    elif [ $bytes -lt 1048576 ]; then
        echo "$((bytes / 1024))M"
    else
        echo "$((bytes / 1024 / 1024))G"
    fi
}

# Check if directory is older than N days (precise to seconds via stat -f %m, macOS only)
is_older_than_days() {
    local dir="$1"
    local days="$2"
    local current_time=$(date +%s)
    local dir_time=$(stat -f %m "$dir" 2>/dev/null || echo "")
    # Unstatable ⇒ err on the side of "not old enough to delete"
    [ -n "$dir_time" ] || return 1
    local threshold_seconds=$(( days * 86400 ))
    local age_seconds=$(( current_time - dir_time ))
    [ $age_seconds -gt $threshold_seconds ]
}

# Hours-based variant for short-lived scratch dirs
is_older_than_hours() {
    local dir="$1"
    local hours="$2"
    local current_time=$(date +%s)
    local dir_time=$(stat -f %m "$dir" 2>/dev/null || echo "")
    [ -n "$dir_time" ] || return 1
    local threshold_seconds=$(( hours * 3600 ))
    local age_seconds=$(( current_time - dir_time ))
    [ $age_seconds -gt $threshold_seconds ]
}

# Recursive activity check. A directory's own mtime only changes when a DIRECT
# child is added/removed/renamed — editing files deeper in the tree does not
# bump it, so root-mtime-only staleness flags actively-used worktrees as
# "stale". These return 0 (true) if ANYTHING in the tree (including .git —
# commits and fetches count as activity) was modified within the window.
# -print -quit exits on the first recent path found, so live trees are cheap.
has_recent_activity_days() {
    local dir="$1"
    local days="$2"
    local found
    found=$(/usr/bin/find "$dir" -mtime -"$days" -print -quit) || return 0
    [ -n "$found" ]
}

has_recent_activity_hours() {
    local dir="$1"
    local hours="$2"
    local found
    found=$(/usr/bin/find "$dir" -mmin -"$(( hours * 60 ))" -print -quit) || return 0
    [ -n "$found" ]
}

source "$SCRIPT_DIR/scripts/build-artifacts.sh"

# Returns 0 (true) if the git repo/worktree at $1 holds work that exists
# nowhere else: uncommitted/untracked files, commits ahead of upstream, or —
# when no upstream is configured (the norm for agent scratch worktrees) — a
# HEAD that no remote ref contains. Non-git dirs report "has WIP" so callers
# preserve them.
has_unpushed_work() {
    local wt="$1"

    if ! git -C "$wt" rev-parse --git-dir >/dev/null 2>&1; then
        return 0
    fi

    local changes
    if [ -d "$wt/.git" ]; then
        # Removing a full clone also removes refs and other worktrees' metadata.
        local refs worktrees nested
        refs=$(git -C "$wt" rev-list -1 --all --not --remotes) || return 0
        [ -z "$refs" ] && ! git -C "$wt" rev-parse --verify -q refs/stash >/dev/null || return 0
        worktrees=$(git -C "$wt" worktree list --porcelain) || return 0
        [ "$(awk '/^worktree / {n++} END {print n+0}' <<< "$worktrees")" = 1 ] || return 0
        nested=$(/usr/bin/find "$wt" -mindepth 1 -path "$wt/.git" -prune -o -name .git -print -quit) || return 0
        [ -z "$nested" ] || return 0
    fi
    if ! changes=$(git -C "$wt" status --porcelain --untracked-files=all) || [ -n "$changes" ]; then
        return 0
    fi

    if git -C "$wt" rev-parse --verify --quiet '@{u}' >/dev/null 2>&1; then
        local unpushed
        unpushed=$(git -C "$wt" rev-list --count '@{u}..HEAD' 2>/dev/null || echo 1)
        [ "${unpushed:-1}" -gt 0 ] && return 0
    else
        # No upstream: HEAD must be reachable from SOME remote ref, else the
        # commits only exist locally. (grep-free: head -1 of the containment
        # list; `|| true` guards set -e when HEAD is on no remote ref.)
        local on_remote
        on_remote=$(git -C "$wt" branch -r --contains HEAD 2>/dev/null | head -1) || true
        [ -z "$on_remote" ] && return 0
    fi

    return 1
}

# Safety: check if any process has open file handles in this directory.
# Returns 0 (true) if there ARE open handles — caller should skip.
has_open_handles() {
    local dir="$1"
    local result rc
    # lsof exits 1 with no output for a successful search with no matches.
    # Warnings, permission errors and an unavailable lsof all preserve the item.
    if result=$(lsof -nP -Fp +D "$dir" 2>&1); then return 0; else rc=$?; fi
    [ "$rc" -ne 1 ] || [ -n "$result" ]
}

# Build a global list of all active git worktrees across all repos.
# Used as a safety guard so we never delete files inside a live worktree.
# Paths are normalized to physical form (pwd -P) so symlink aliases like
# /tmp vs /private/tmp can't defeat the guard.
discover_active_worktrees() {
    local repo_dir
    for repo_dir in "${PROJECTS[@]}"; do
        [ -e "$repo_dir/.git" ] || continue
        local wts=$(cd "$repo_dir" && git worktree list --porcelain 2>/dev/null \
                    | awk '/^worktree / {print substr($0,10)}')
        local wt
        while IFS= read -r wt; do
            [ -n "$wt" ] || continue
            wt="$(cd "$wt" 2>/dev/null && pwd -P || echo "$wt")"
            ACTIVE_WORKTREES+="$wt"$'\n'
        done <<< "$wts"
    done
}

# Returns 0 (true) if the supplied physical path is inside any active worktree.
# With a second arg "allow-self", an exact match with a worktree root does NOT
# count — used when evaluating a worktree itself for removal, where only
# containment in a DIFFERENT worktree should block.
is_in_active_worktree() {
    local path="$1"
    local allow_self="${2:-}"
    local wt
    [ -n "$ACTIVE_WORKTREES" ] || return 1
    while IFS= read -r wt; do
        [ -z "$wt" ] && continue
        case "$path" in
            "$wt") [ "$allow_self" = "allow-self" ] || return 0 ;;
            "$wt"/*) return 0 ;;
        esac
    done <<< "$ACTIVE_WORKTREES"
    return 1
}

# Projects whose build output another program RUNS IN PLACE: an MCP server an
# agent config launches by path, a CLI linked onto PATH or into the global
# node_modules (`npm link`), or a workspace that sets Loom's
# `clean.keepBuildArtifacts`. Their node_modules/, target/, venvs and build/
# are runtime, not cache, and neither the age test nor lsof can see that: an
# MCP server holds no open handle on a module it has already loaded, and the
# project it runs from may not be edited for weeks. Deleting them broke the
# squad MCP for every agent on the machine (2026-10-15 and 2026-10-19), so
# each per-project artifact pass skips these projects outright.
#
# One line per project: "<project-name><TAB><why>".
RUNTIME_PROJECTS=""

# Resolve a symlink one hop to an absolute physical path (macOS readlink has no
# portable -f before 12.3, so do it by hand).
resolve_link() {
    local link="$1" target
    target="$(readlink "$link" 2>/dev/null)" || return 1
    case "$target" in
        /*) ;;
        *) target="$(dirname "$link")/$target" ;;
    esac
    local dir
    dir="$(cd "$(dirname "$target")" 2>/dev/null && pwd -P)" || { echo "$target"; return 0; }
    echo "$dir/$(basename "$target")"
}

# Print the MCP server paths (command, args, env values) from every
# `mcpServers` object in a JSON file, one per line. Relative paths are resolved
# against $2 (the directory the config applies to); for ~/.claude.json a
# per-project server resolves against that project's key instead. Needs
# python3; without it the caller falls back to a plain string scan.
mcp_json_paths() {
    local file="$1" base="$2"
    python3 - "$file" "$base" <<'PY' 2>/dev/null
import json, os, sys
path, base = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path))
except Exception:
    sys.exit(1)
def strings(server):
    vals = [server.get("command")] + list(server.get("args") or [])
    vals += list((server.get("env") or {}).values())
    return [v for v in vals if isinstance(v, str)]
def walk(node, here):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "mcpServers" and isinstance(v, dict):
                for server in v.values():
                    if isinstance(server, dict):
                        for s in strings(server):
                            if s.startswith(("./", "../")):
                                print(os.path.normpath(os.path.join(here, s)))
                            elif s.startswith("/"):
                                print(s)
            else:
                walk(v, k if k.startswith("/") and isinstance(v, dict) else here)
    elif isinstance(node, list):
        for v in node:
            walk(v, here)
walk(data, base)
PY
}

# Make each stdin path absolute against $1 (relative paths are resolved
# physically, so "../squad/x" lands in squad, not in $1). A helper rather than
# an inline loop: bash 3.2 cannot parse an unbalanced case pattern inside $(...).
absolutize_paths() {
    local base="$1" p d
    while IFS= read -r p; do
        case "$p" in
            (/*) echo "$p" ;;
            (*) d="$(cd "$base/$(dirname "$p")" 2>/dev/null && pwd -P)" || continue
                echo "$d/$(basename "$p")" ;;
        esac
    done
    return 0
}

# Prefix each non-empty stdin line with "$1<TAB>" (BSD sed has no \t).
tag_lines() {
    local p
    while IFS= read -r p; do
        [ -n "$p" ] && printf '%s\t%s\n' "$1" "$p"
    done
    return 0
}

discover_runtime_projects() {
    [ "${#PROJECTS[@]}" -gt 0 ] || return 0
    local have_py=false
    # /usr/bin/python3 is a stub that pops the Command Line Tools installer
    # when they are missing; only probe it when they are present.
    if [ "$(command -v python3 2>/dev/null)" != /usr/bin/python3 ] \
        || xcode-select -p >/dev/null 2>&1; then
        python3 -c 'pass' >/dev/null 2>&1 && have_py=true
    fi

    local candidates=""
    local cfg proj
    # MCP configs: Claude Code (user + per-project), Claude Desktop, Cursor, and
    # every project's own .mcp.json.
    for cfg in "$HOME/.claude.json" \
               "$HOME/Library/Application Support/Claude/claude_desktop_config.json" \
               "$HOME/.cursor/mcp.json"; do
        [ -f "$cfg" ] || continue
        if [ "$have_py" = true ]; then
            candidates+="$(mcp_json_paths "$cfg" "$HOME" | tag_lines mcp)"$'\n'
        elif [ "$cfg" != "$HOME/.claude.json" ]; then
            # Whole-file scan is only safe where the file holds nothing but MCP
            # config; ~/.claude.json also lists every project ever opened.
            candidates+="$(grep -oE '"/[^"]+"' "$cfg" | tr -d '"' | tag_lines mcp)"$'\n'
        fi
    done
    local parents=() parent prior
    for proj in "${PROJECTS[@]}"; do
        parent=${proj%/*}
        [ -n "$parent" ] || parent=/
        for prior in ${parents[@]+"${parents[@]}"}; do [ "$prior" != "$parent" ] || continue 2; done
        parents+=("$parent")
    done
    # A sibling's MCP config may explicitly run one of the selected repos.
    for parent in "${parents[@]}"; do
      for cfg in "$parent"/*/.mcp.json; do
        [ -f "$cfg" ] || continue
        proj="$(cd "${cfg%/*}" && pwd -P)"
        if [ "$have_py" = true ]; then
            candidates+="$(mcp_json_paths "$cfg" "$proj" | tag_lines mcp)"$'\n'
        else
            candidates+="$(grep -oE '"(/|\.\.?/)[^"]+"' "$cfg" | tr -d '"' \
                | absolutize_paths "$proj" | tag_lines mcp)"$'\n'
        fi
      done
    done
    # Codex: only lines inside [mcp_servers.*] tables (the rest of config.toml
    # names trusted projects, which are not runtimes).
    if [ -f "$HOME/.codex/config.toml" ]; then
        candidates+="$(awk '/^\[/ { in_mcp = ($0 ~ /^\[mcp_servers[].]/) } in_mcp' "$HOME/.codex/config.toml" \
            | grep -oE '"/[^"]+"' | tr -d '"' | tag_lines mcp)"$'\n'
    fi
    # CLIs run in place: symlinks on PATH-like dirs and in the global
    # node_modules (`npm link`) that point into a project.
    local bindir link target npm_root=""
    if command -v npm >/dev/null 2>&1; then
        npm_root="$(npm_config_cache="${run_dir:-${TMPDIR:-/tmp}}/npm" npm_config_logs_max=0 npm_config_update_notifier=false npm root -g 2>/dev/null)" || npm_root=""
    fi
    for bindir in "$HOME/.local/bin" "$HOME/bin" /opt/homebrew/bin /usr/local/bin \
                  ${npm_root:+"$npm_root"} ${npm_root:+"$npm_root"/@*}; do
        [ -d "$bindir" ] || continue
        if [ "$have_py" = true ]; then
            candidates+="$(python3 -B - "$bindir" "${PROJECTS[@]}" <<'PYLINKS'
import os, sys
projects = set(sys.argv[2:])
with os.scandir(sys.argv[1]) as entries:
    for link in entries:
        if link.is_symlink():
            target = os.path.realpath(link.path)
            parent = target
            while parent != "/":
                if parent in projects:
                    print("linked\t" + target)
                    break
                parent = os.path.dirname(parent)
PYLINKS
)"$'\n'
        else
            while IFS=$'\t' read -r link target; do
                case "$target" in /*) candidates+="linked"$'\t'"$target"$'\n' ;; esac
            done < <(find "$bindir" -maxdepth 1 -type l -exec stat -f '%N%t%Y' {} + 2>/dev/null)
        fi
    done
    # Loom's per-workspace opt-out, honoured here too so both cleaners agree.
    for proj in "${PROJECTS[@]}"; do
        for cfg in "$proj/.loom/config.json" "$proj"/.loom-local/*.json; do
            [ -f "$cfg" ] || continue
            if grep -qE '"keepBuildArtifacts"[[:space:]]*:[[:space:]]*true' "$cfg"; then
                candidates+="keepBuildArtifacts"$'\t'"$proj"$'\n'
                break
            fi
        done
    done
    # Explicit keeps: CLEAN_KEEP_PROJECTS=name1:name2
    for proj in "${PROJECTS[@]}"; do
        case ":${CLEAN_KEEP_PROJECTS:-}:" in
            *":${proj##*/}:"*) candidates+="CLEAN_KEEP_PROJECTS"$'\t'"$proj"$'\n' ;;
        esac
    done
    local why path
    while IFS=$'\t' read -r why path; do
        [ -n "$path" ] || continue
        for proj in "${PROJECTS[@]}"; do
            case "$path" in "$proj"|"$proj"/*) ;; *) continue ;; esac
            case $'\n'"$RUNTIME_PROJECTS" in *$'\n'"$proj"$'\t'*) continue ;; esac
            RUNTIME_PROJECTS+="$proj"$'\t'"$why"$'\n'
        done
    done <<< "$candidates"
}

runtime_project_reason() {
    local proj why
    while IFS=$'\t' read -r proj why; do
        [ -n "$proj" ] || continue
        case "$1" in "$proj"|"$proj"/*) echo "$why"; return 0 ;; esac
    done <<< "$RUNTIME_PROJECTS"
    return 1
}

# Remove or report removal of a file/directory
remove_item() {
    local item="$1"
    local reason="$2" parent before="" after

    [ -e "$item" ] || return 0
    local kib
    kib=$(du -sk "$item") || { step_failed "$item" "measure $reason"; return; }
    kib=${kib%%[[:space:]]*}
    # Optional eligibility check runs after the (possibly long) size scan.
    if [ "$#" -gt 2 ]; then
        shift 2
        "$@" || return 0
    fi
    local detail="  [$reason] $item ($(bytes_to_human "$kib"))"
    if [ "$CHECK_MODE" = true ]; then
        term_line would_remove "  Would remove" "$detail"
        record_item would_remove "$item" "$reason" "$((kib * 1024))"
        return 0
    fi
    printf '  Removing%s\n' "$detail"
    if [ -n "${CLEAN_ACTIVITY_SNAPSHOT:-}" ] && [ ! -e "$project/.git" ]; then
        parent=${item%/*}
        before=$(/usr/bin/stat -f '%d %i %.9Fm' "$parent") || before=""
    fi
    if rm -rf -- "$item"; then
        if [ -n "$before" ] && after=$(/usr/bin/stat -f '%d %i %.9Fm' "$parent") && [ "${before% *}" = "${after% *}" ]; then
            printf '%s %s\n' "$before" "${after##* }" >> "$CLEAN_ACTIVITY_SNAPSHOT.dirs"
        fi
        record_item removed "$item" "$reason" "$((kib * 1024))"
        term_line removed "    ✓ Removed"
    else
        step_failed "$item" "$reason"
    fi
}

# =============================================================================
# SYSTEM CLEANUP
# =============================================================================

unset CLEAN_ACTIVITY_SNAPSHOT
if [ "${#PROJECTS[@]}" -gt 0 ]; then
    term_line step "Checking active worktrees"
    discover_active_worktrees
fi
if [ "$RUN_SYSTEM" = true ]; then
term_line heading "System cleanup"
echo ""


# Measure tool-managed caches before and after a successful native prune.
clean_cache() {
    local path=$1 label=$2 before after
    shift 2
    [ -d "$path" ] || { record_item skipped "$path" "cache absent"; return; }
    if [ "$CHECK_MODE" = true ]; then
        echo "  Would run: $*"
        record_item would_remove "$path" "$label"
        return
    fi
    before=$(get_size_kib "$path") || { step_failed "$path" "measure cache"; return; }
    if "$@"; then
        after=0
        if [ -e "$path" ]; then after=$(get_size_kib "$path") || { step_failed "$path" "measure cache"; return; }; fi
        record_item removed "$path" "$label" "$(( before > after ? (before - after) * 1024 : 0 ))"
    else
        step_failed "$path" "$label"
    fi
}

if command -v brew >/dev/null; then
    term_line step "Homebrew cache"
    if cache=$(brew --cache); then
        clean_cache "$cache" "brew cleanup" timeout 60 brew cleanup --prune=all -s
    else
        step_failed brew "locate Homebrew cache"
    fi
fi

# Compiler caches, npm cache, and idle scratch directories
sweep_mode=clean
[ "$CHECK_MODE" = false ] || sweep_mode=check
sweep_args=(--mode "$sweep_mode")
[ "$AGGRESSIVE_MODE" = false ] || sweep_args+=(--aggressive)
python3 -B "$SCRIPT_DIR/scripts/system_sweep.py" "${sweep_args[@]}" || step_failed "system caches" "cache or scratch pass failed"
echo ""

if command -v pnpm >/dev/null; then
    term_line step "pnpm store"
    if [ "$CHECK_MODE" = true ]; then
        # Even 'pnpm store path' can create a new store. Prune itself knows
        # which objects are reclaimable only when it runs.
        echo "  Would run: pnpm store prune (reclaimable size unknown)"
        record_item would_remove pnpm "pnpm store prune; reclaimable size unknown"
    elif cache=$(pnpm store path); then clean_cache "$cache" "pnpm store prune" pnpm store prune
    else step_failed pnpm "locate store"; fi
fi
if command -v pip >/dev/null; then
    term_line step "pip cache"
    if cache=$(pip cache dir); then clean_cache "$cache" "pip cache purge" pip cache purge
    else step_failed pip "locate cache"; fi
fi
if command -v uv >/dev/null; then
    term_line step "uv cache"
    if cache=$(uv cache dir); then clean_cache "$cache" "uv cache prune" uv cache prune
    else step_failed uv "locate cache"; fi
fi

# HuggingFace cache (info-only — interactive cleanup recommended)
if [ -d ~/.cache/huggingface ]; then
    SIZE=$(get_size ~/.cache/huggingface)
    if [ "$SIZE" != "0B" ] && [ -n "$SIZE" ]; then
        echo "🤗 HuggingFace cache: $SIZE"
        if command -v huggingface-cli &> /dev/null; then
            echo "  ℹ️  Run 'huggingface-cli delete-cache' interactively to prune models"
        fi
        echo ""
    fi
fi

if command -v bun >/dev/null; then
    for item in "$HOME/Library/Caches/bun"/*; do remove_item "$item" "bun cache"; done
fi

clean_shared_target

# Docker cleanup
if command -v docker &> /dev/null; then
    term_line step "Docker resources"

    docker_prune() {
        local output allocated
        if output=$(timeout "$@"); then
            echo "$output"
            allocated=$(awk '/^Total reclaimed space:|^Total:/ {
                n=$NF+0; u=$NF; sub(/^[0-9.]+/,"",u)
                m=(u=="kB"?1e3:u=="MB"?1e6:u=="GB"?1e9:u=="TB"?1e12:1)
                total=n*m
            } END {printf "%.0f", total}' <<< "$output")
            record_item removed docker "$*" "$allocated"
        else
            step_failed docker "$*"
        fi
    }
    if ! timeout 5 docker info &>/dev/null; then
        record_item skipped docker "engine unavailable"
        term_line skipped "  Docker engine unreachable." " Check free space on its data volume below before restarting Docker."
        echo ""
    else
        if [ "$CHECK_MODE" = false ]; then
            echo "  Removing containers unused for >24h..."
            if ! containers=$(timeout 10 docker ps -a --filter status=exited --filter status=created --format '{{.ID}} {{.CreatedAt}}'); then
                step_failed docker "list old containers"
            else
                while read -r id; do
                    [ -n "$id" ] || continue
                    if timeout 30 docker rm "$id"; then record_item removed "$id" "Docker container"
                    else step_failed "$id" "remove Docker container"; fi
                done < <(awk -v cutoff="$(date -u -v-1d '+%Y-%m-%d')" '$2 < cutoff {print $1}' <<< "$containers")
            fi

            echo "  Removing dangling images..."
            docker_prune 30 docker image prune -f

            echo "  Removing unused volumes..."
            docker_prune 30 docker volume prune -f

            echo "  Removing build cache >24h old..."
            docker_prune 30 docker builder prune -f --filter "until=24h"

            if docker buildx version &>/dev/null; then
                echo "  Pruning buildx cache >24h old..."
                docker_prune 30 docker buildx prune -f --filter "until=24h"
            fi

            if [ "$AGGRESSIVE_MODE" = true ]; then
                echo "  💥 [aggressive] docker system prune -af --volumes"
                docker_prune 120 docker system prune -af --volumes
                echo "  ℹ️  Note: on macOS, Docker.raw does not auto-shrink. Use Docker"
                echo "     Desktop → Settings → Resources → 'Clean / Purge data' to reclaim."
            fi
        else
            record_item would_remove docker "Docker resource prune"
            echo "  Would remove: containers >24h, dangling images, unused volumes, build cache"
            [ "$AGGRESSIVE_MODE" = true ] && echo "  Would also run: docker system prune -af --volumes"
        fi

        echo ""
    fi
fi

# VS Code extension caches
term_line step "VS Code caches"
if [ -d ~/Library/Caches/vscode-cpptools ]; then
    SIZE=$(get_size ~/Library/Caches/vscode-cpptools)
    remove_item ~/Library/Caches/vscode-cpptools "vscode-cpptools ($SIZE)"
fi

if [ -d ~/Library/Caches/ms-playwright ]; then
    SIZE=$(get_size ~/Library/Caches/ms-playwright)
    remove_item ~/Library/Caches/ms-playwright "ms-playwright ($SIZE)"
fi
echo ""

if [ -d "$HOME/Library/Logs" ]; then
    if /usr/bin/find "$HOME/Library/Logs" -type f ! -path "$HOME/Library/Logs/clean.log" -mtime +30 -print0 > "$CLEAN_EVENT_FILE.candidates"; then
      while IFS= read -r -d '' item; do
        remove_item "$item" "log older than 30 days"
      done < "$CLEAN_EVENT_FILE.candidates"
    else
        step_failed "$HOME/Library/Logs" "enumerate logs"
    fi
fi

# Stale /tmp clones from dev tool sessions (PR reviews, issue work, etc.)
# Detected structurally: any /private/tmp/<dir>/ that is a git repo.
# `.git` may be a directory (regular clone) OR a file (`git worktree add` writes
# `.git` as a file containing `gitdir: <path>`), so check existence — not type.
# Safety: RECURSIVE idle check + lsof + worktree containment + never delete
# uncommitted or unpushed work (a 1-hour threshold on the root mtime alone
# deleted live agent worktrees whose deep files were being edited).
term_line step "Temporary Git clones (idle >$TMP_AGE_HOURS hours)"
for d in /private/tmp/*/; do
    d="${d%/}"
    [ -d "$d" ] && [ ! -L "$d" ] || continue
    [ -e "$d/.git" ] || continue
    if [ "$EXPLICIT_PROJECTS" = true ]; then
        keep_item "$d" "temporary clone outside workspace cleanup"
        continue
    fi
    git -C "$d" rev-parse --git-dir >/dev/null 2>&1 || continue

    if has_recent_activity_hours "$d" $TMP_AGE_HOURS; then
        keep_item "$d" "recent activity"
        continue
    fi

    # A dir nested inside a DIFFERENT live worktree is not ours to touch; an
    # exact match means $d is itself a registered worktree — handled below.
    if ! physical_d=$(cd "$d" && pwd -P); then
        keep_item "$d" "could not resolve worktree path"
        continue
    fi
    if is_in_active_worktree "$physical_d" allow-self; then
        keep_item "$d" "inside active worktree"
        continue
    fi

    if has_open_handles "$d"; then
        keep_item "$d" "open handles"
        continue
    fi

    # Never delete work that exists nowhere else.
    if has_unpushed_work "$d"; then
        keep_item "$d" "uncommitted/unpushed"
        continue
    fi

    if [ -f "$d/.git" ]; then
        # Linked worktree (`.git` is a gitdir pointer file): remove via git so
        # the parent repo's metadata stays consistent. No --force and no rm -rf
        # fallback — if git refuses (state changed since our checks), keep it.
        size_kib=$(get_size_kib "$d") || { step_failed "$d" "measure worktree"; continue; }
        size=$(bytes_to_human "$size_kib")
        echo "  [stale tmp worktree] $d ($size)"
        if [ "$CHECK_MODE" = false ]; then
            main_wt=$(git -C "$d" worktree list --porcelain 2>/dev/null \
                      | awk '/^worktree / {print substr($0,10); exit}')
            if [ -n "$main_wt" ] && (cd "$main_wt" 2>/dev/null && git worktree remove "$(cd "$d" && pwd -P)" 2>/dev/null); then
                record_item removed "$d" "stale tmp worktree" "$((size_kib * 1024))"
            else
                step_failed "$d" "git worktree remove refused"
            fi
        else
            record_item would_remove "$d" "stale tmp worktree" "$((size_kib * 1024))"
        fi
    else
        remove_item "$d" "stale tmp clone"
    fi
done
echo ""

# =============================================================================
# PROJECT CLEANUP (skip if --system-only or no project dir)
# =============================================================================

fi  # system phase

if [ "$RUN_PROJECTS" = true ]; then
term_line heading "Project cleanup" "  (${#PROJECTS[@]} workspaces)"
echo ""

if [ "$CHECK_MODE" = false ]; then
    CLEAN_ACTIVITY_SNAPSHOT="$run_dir/activity.json"
    export CLEAN_ACTIVITY_SNAPSHOT
    python3 -B "$SCRIPT_DIR/scripts/build-artifacts.py" snapshot "${PROJECTS[@]}" > "$CLEAN_ACTIVITY_SNAPSHOT"
    : > "$CLEAN_ACTIVITY_SNAPSHOT.dirs"
fi

# Projects other programs run in place (MCP servers, linked CLIs): every
# artifact pass below leaves them alone.
# `|| true` also turns set -e off inside it: a detection failure means less
# protection reported, never an aborted cleanup.
discover_runtime_projects || true
term_line step "Protected runtime projects"
if [ -n "$RUNTIME_PROJECTS" ]; then
    printf '%s' "$RUNTIME_PROJECTS" | while IFS=$'\t' read -r name why; do
        keep_item "$name" "runtime: $why"
    done
else
    echo "  (none found)"
fi
echo ""

# Node.js caches — only for projects with no recent activity. Deleting these
# from a live project is not a data-loss bug (they regenerate) but it silently
# breaks the next build, which is how this was found: three repos lost their
# deps mid-session while agents were working in them.
term_line step "Node.js caches (projects idle >$NODE_MODULES_AGE_DAYS days)"
for project in "${PROJECTS[@]}"; do
    [ -d "$project" ] || continue
    [ -d "$project/node_modules" ] || [ -d "$project/.next" ] || continue

    pname="$(basename "$project")"

    if runtime_project_reason "$project" >/dev/null; then
        continue
    fi

    if has_recent_project_activity "$project" $NODE_MODULES_AGE_DAYS; then
        keep_item "$project" "recent activity: ${PROJECT_ACTIVITY_REASON:-$pname}"
        continue
    fi

    if has_open_handles "$project"; then
        keep_item "$project" "open handles or incomplete lsof check"
        continue
    fi

    if [ -d "$project/node_modules" ]; then
        remove_item "$project/node_modules" "node_modules"
    fi

    if [ -d "$project/.next" ]; then
        remove_item "$project/.next" "Next.js cache"
    fi
done
echo ""

# Enumerate first so a discovery error cannot silently truncate a project pass.
candidate_file="$CLEAN_EVENT_FILE.candidates"
for project in "${PROJECTS[@]}"; do
    runtime_project_reason "$project" >/dev/null && continue
    if ! python3 -B "$SCRIPT_DIR/scripts/project-candidates.py" "$project" > "$candidate_file"; then
        step_failed "$project" "enumerate project artifacts"
        continue
    fi
    [ -s "$candidate_file" ] || continue
    scan_time=$(date +%s)
    project_in_worktree=false
    if is_in_active_worktree "$project"; then project_in_worktree=true; fi
    while IFS= read -r -d '' kind && IFS= read -r -d '' item &&
          IFS= read -r -d '' expected && IFS= read -r -d '' modified; do
        case "$kind" in
            cache) reason="Python cache"; days=$PYCACHE_AGE_DAYS ;;
            bytecode) reason="stray .pyc"; days=$PYCACHE_AGE_DAYS ;;
            venv) reason="venv"; days=$VENV_AGE_DAYS ;;
            cargo) reason="Rust target"; days=$TARGET_AGE_DAYS ;;
        esac
        if [ -L "$item" ] || [ "$(cd "${item%/*}" && pwd -P)" != "${item%/*}" ]; then
            keep_item "$item" "symlinked artifact path"
        elif [[ "$kind" != cargo && "$kind" != venv ]] &&
             { [ "$project_in_worktree" = true ] || is_in_active_worktree "$item"; }; then
            keep_item "$item" "active worktree"
        # Discovery can tell us to keep a recent item. Re-stat older items
        # before considering removal in case they changed since discovery.
        elif (( scan_time - modified <= days * 86400 )) || ! is_older_than_days "$item" "$days"; then
            keep_item "$item" "recent $reason"
        elif [[ "$kind" = cargo || "$kind" = venv ]]; then
            if has_recent_project_activity "$project" "$days"; then
                keep_item "$item" "recent activity: $PROJECT_ACTIVITY_REASON"
            else
                remove_item "$item" "$reason" verify_build "$item" "$expected" "$project" "$kind" "$days"
            fi
        else
            remove_item "$item" "$reason"
        fi
    done < "$candidate_file"
done

clean_project_lake_builds

# Java/Kotlin build directories
term_line step "Gradle build caches"
has_gradle_build() {
    [ -d "$1/build" ] && { [ -f "$1/build.gradle" ] || [ -f "$1/build.gradle.kts" ]; }
}
for project in "${PROJECTS[@]}"; do
    [ -d "$project" ] || continue
    has_gradle_build "$project" || [ -d "$project/.gradle" ] || continue
    runtime_project_reason "$project" >/dev/null && continue

    if has_open_handles "$project"; then
        keep_item "$project" "open handles"
        continue
    fi
    if has_gradle_build "$project"; then
        remove_item "$project/build" "Gradle build"
    fi

    if [ -d "$project/.gradle" ]; then
        remove_item "$project/.gradle" "Gradle cache"
    fi
done
echo ""

# Per-repo git maintenance
term_line step "Git maintenance"
for project in "${PROJECTS[@]}"; do
    [ -d "$project/.git" ] || continue
    if [ "$CHECK_MODE" = false ]; then
        git -C "$project" worktree prune || step_failed "$project" "git worktree prune"
        git -C "$project" gc --auto || step_failed "$project" "git gc --auto"
    fi
done
echo ""

# =============================================================================
# WORKTREE CLEANUP — clean worktrees and branches for closed GitHub issues,
# plus stale agent-workflow worktrees (audit-*, mechanic-*, researcher-*, …)
# under .loom/worktrees/ and .claude/worktrees/.
# =============================================================================

unset CLEAN_ACTIVITY_SNAPSHOT
fi  # project phase

if [ "$RUN_WORKTREES" = true ]; then
term_line heading "Worktree cleanup"
echo ""

if ! command -v gh >/dev/null || ! GH_PROMPT_DISABLED=1 timeout 10 gh api user --silent </dev/null >/dev/null 2>&1; then
    term_line skipped "  Skipped" "  GitHub unavailable or unauthenticated"
    record_item skipped worktrees "GitHub unavailable or unauthenticated"
    echo ""
else

wt_cleaned=0
wt_skipped=0
br_cleaned=0
tmux_cleaned=0

# Decide whether a non-issue worktree is safe to remove.
# Returns 0 (true) if all safety checks pass; prints the reason and returns 1 otherwise.
# Args: $1 = project_name, $2 = worktree_dir, $3 = display_label
#
# Note: no is_in_active_worktree gate here — these dirs live INSIDE the main
# checkout by path, so that check matched the main worktree's prefix and made
# this whole pass inert. The real protection is activity + WIP below.
worktree_is_stale_and_safe() {
    local pname="$1"
    local wt="$2"
    local label="$3"

    # Recursive: editing files deep in the tree doesn't bump the root mtime,
    # so root-mtime-only staleness would flag actively-used worktrees.
    if has_recent_activity_days "$wt" $WORKTREE_AGE_DAYS; then
        keep_item "$wt" "recent activity"
        return 1
    fi

    if has_open_handles "$wt"; then
        keep_item "$wt" "open handles"
        return 1
    fi

    # Never delete work that exists nowhere else: uncommitted/untracked files,
    # or commits (with or without an upstream) that no remote ref contains.
    if has_unpushed_work "$wt"; then
        keep_item "$wt" "uncommitted/unpushed"
        return 1
    fi

    return 0
}

for project in "${PROJECTS[@]}"; do
    [ -d "$project/.git" ] || continue

    project_name=$(basename "$project")
    has_worktrees=false
    has_branches=false

    # Walk every subdir under .loom/worktrees/ and .claude/worktrees/.
    # issue-N dirs use the closed-issue check; everything else uses age-based safety.
    for worktree_root in "$project/.loom/worktrees" "$project/.claude/worktrees"; do
        [ -d "$worktree_root" ] || continue
        if [ "$(cd "$worktree_root" && pwd -P)" != "$worktree_root" ]; then
            keep_item "$worktree_root" "symlinked worktree root"
            continue
        fi
        for worktree_dir in "$worktree_root"/*; do
            [ -d "$worktree_dir" ] || continue
            if [ -L "$worktree_dir" ]; then keep_item "$worktree_dir" "symlink"; continue; fi
            has_worktrees=true

            wt_name=$(basename "$worktree_dir")
            root_tag=$(basename "$(dirname "$worktree_root")")/$(basename "$worktree_root")
            label="$root_tag/$wt_name"

            # A dir here that is not a git tree at all is likely the residue
            # of a lost worktree registration — its files may be the only copy
            # of someone's work. Report, never delete.
            if [ ! -e "$worktree_dir/.git" ] || ! git -C "$worktree_dir" rev-parse --git-dir >/dev/null 2>&1; then
                keep_item "$worktree_dir" "not a git tree"
                wt_skipped=$((wt_skipped + 1))
                continue
            fi

            if echo "$wt_name" | grep -qE '^issue-[0-9]+$'; then
                # Issue worktree: only remove if the corresponding issue is CLOSED,
                # and never with open handles or work that exists nowhere else.
                issue_num="${wt_name#issue-}"
                issue_state=$(cd "$project" && timeout 10 gh issue view "$issue_num" --json state --jq '.state' 2>/dev/null) || { record_item skipped "$project#$issue_num" "issue lookup unavailable"; continue; }

                if [ "$issue_state" != "CLOSED" ]; then
                    keep_item "$worktree_dir" "issue $issue_state"
                    wt_skipped=$((wt_skipped + 1))
                    continue
                fi

                if has_open_handles "$worktree_dir"; then
                    keep_item "$worktree_dir" "open handles"
                    wt_skipped=$((wt_skipped + 1))
                    continue
                fi

                if has_unpushed_work "$worktree_dir"; then
                    keep_item "$worktree_dir" "uncommitted/unpushed"
                    wt_skipped=$((wt_skipped + 1))
                    continue
                fi

                echo "  [CLOSED] $project_name $label"
            else
                if ! worktree_is_stale_and_safe "$project_name" "$worktree_dir" "$label"; then
                    wt_skipped=$((wt_skipped + 1))
                    continue
                fi
                echo "  [STALE] $project_name $label"
            fi

            size_kib=$(get_size_kib "$worktree_dir") || { step_failed "$worktree_dir" "measure worktree"; continue; }
            if [ "$CHECK_MODE" = false ]; then
                worktree_abs="$(cd "$worktree_dir" && pwd -P)"
                if [ -f "$worktree_dir/.git" ]; then
                    # Registered worktree: no --force and no rm -rf fallback —
                    # if git refuses (e.g. files appeared between our checks
                    # and now), leave it for a future run.
                    if (cd "$project" && git worktree remove "$worktree_abs" 2>/dev/null); then
                        record_item removed "$worktree_dir" "worktree" "$((size_kib * 1024))"
                    else
                        step_failed "$worktree_dir" "git worktree remove refused"
                        wt_skipped=$((wt_skipped + 1))
                        continue
                    fi
                else
                    # Stray full clone stuffed under the worktree root — it
                    # passed the WIP checks above, so plain delete is safe.
                    remove_item "$worktree_dir" "stray clone"
                fi
            else
                record_item would_remove "$worktree_dir" "worktree" "$((size_kib * 1024))"
            fi
            wt_cleaned=$((wt_cleaned + 1))
        done
    done

    # Clean feature branches for closed issues (feature/issue-*)
    branches=$(cd "$project" && git branch 2>/dev/null | grep "feature/issue-" | sed 's/^[*+ ]*//' || true)
    if [ -n "$branches" ]; then
        has_branches=true
        for branch in $branches; do
            issue_num=$(echo "$branch" | sed 's/feature\/issue-//' | sed 's/-.*//' | sed 's/[^0-9].*//')

            if ! echo "$issue_num" | grep -q '^[0-9]\+$'; then
                continue
            fi

            issue_state=$(cd "$project" && timeout 10 gh issue view "$issue_num" --json state --jq '.state' 2>/dev/null) || { record_item skipped "$project#$issue_num" "issue lookup unavailable"; continue; }

            if [ "$issue_state" = "CLOSED" ]; then
                if [ -z "$(git -C "$project" branch -r --contains "$branch")" ]; then
                    keep_item "$project:$branch" "commits unavailable on a remote"
                elif [ "$CHECK_MODE" = true ]; then
                    record_item would_remove "$project:$branch" "closed issue"
                elif git -C "$project" branch -D "$branch"; then
                    record_item removed "$project:$branch" "closed issue"
                    br_cleaned=$((br_cleaned + 1))
                else
                    step_failed "$project:$branch" "delete closed-issue branch"
                fi
            else
                keep_item "$project:$branch" "issue $issue_state"
            fi
        done
    fi
done

echo ""

# Clean Loom tmux sessions (loom-*)
term_line step "Loom tmux sessions"
if [ "$EXPLICIT_PROJECTS" = true ] || [ "$PHASE" = "--worktrees-only" ] || [ ! -t 0 ]; then
    record_item skipped tmux "global sessions omitted from scoped or unattended runs"
elif command -v tmux >/dev/null; then
    while IFS= read -r session; do
        case "$session" in loom-*) ;; *) continue ;; esac
        if [ "$CHECK_MODE" = true ]; then record_item would_remove "$session" "Loom tmux session"
        elif tmux kill-session -t "$session"; then record_item removed "$session" "Loom tmux session"
        else step_failed "$session" "stop tmux session"; fi
    done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null)
fi

echo "  Worktrees removed: $wt_cleaned  Branches deleted: $br_cleaned"
[ $wt_skipped -gt 0 ] && echo "  Worktrees kept (open/uncommitted): $wt_skipped"
echo ""

fi  # gh CLI check

fi  # worktree phase

if [ "$RUN_SYSTEM" = true ]; then
    if command -v python3 >/dev/null; then
        python3 -B "$SCRIPT_DIR/scripts/system_report.py" "${PROJECTS[@]}" || step_failed "system report" "diagnostics failed"
    else
        echo "  System diagnostics skipped: python3 unavailable"
        record_item skipped "system report" "python3 unavailable"
    fi
fi

# =============================================================================
# SUMMARY
# =============================================================================

[ -z "$SHARED_CARGO_SUMMARY" ] || echo "$SHARED_CARGO_SUMMARY"
term_line heading "Disk usage"
df -h / | tail -1 | awk '{print "  Available: " $4 " (" $5 " used)"}'
