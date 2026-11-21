#!/bin/bash
set -eu
SCRIPT_DIR=$(cd "$(dirname "$0")/.." && pwd -P)
source "$SCRIPT_DIR/scripts/build-artifacts.sh"
fixture=$(mktemp -d)
trap 'rm -rf "$fixture"' EXIT
fixture=$(cd "$fixture" && pwd -P)
project="$fixture/ws/main"
mkdir -p "$project"
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
export GIT_AUTHOR_NAME=test GIT_COMMITTER_NAME=test
export GIT_AUTHOR_EMAIL=test@example.invalid GIT_COMMITTER_EMAIL=test@example.invalid
git init -q "$project"
echo 'int x;' > "$project/source.c"
git -C "$project" add source.c
GIT_AUTHOR_DATE=2001-01-01T00:00:00Z GIT_COMMITTER_DATE=2001-01-01T00:00:00Z git -C "$project" commit -qm 'old commit'
mkdir -p "$project/.loom-local" "$project/.loom/locks" "$project/node_modules"
touch -t 200101010000 "$project/source.c"
touch "$project/.git/FETCH_HEAD" "$project/.loom-local/fleet-repo.bak" "$project/.loom/locks/state" "$project/node_modules/pkg"
if has_recent_project_activity "$project" 7; then echo "unexpected activity: $PROJECT_ACTIVITY_REASON"; exit 1; fi
touch "$project/authored file"
has_recent_project_activity "$project" 7
[ "$PROJECT_ACTIVITY_REASON" = 'file authored file' ]
touch -t 200101010000 "$project/authored file"
touch "$project/source.c"
has_recent_project_activity "$project" 7
[ "$PROJECT_ACTIVITY_REASON" = 'file source.c' ]
git -C "$project" commit -q --allow-empty -m 'fresh commit'
touch -t 200101010000 "$project/source.c"
has_recent_project_activity "$project" 7
[[ "$PROJECT_ACTIVITY_REASON" = 'commit '* ]]
# An old checkout stays idle even with recent git/agent bookkeeping.
git -C "$project" checkout -q --detach HEAD^
mkdir -p "$project/proofs/.lake/build" "$project/proofs/.lake/packages" "$project/.loom/worktrees/wt"
git init -q "$project/.loom/worktrees/wt"
mkdir -p "$project/.loom/worktrees/wt/proofs/.lake/build"
# Fake process/handle states. Use the shipped deletion helper with isolated paths.
has_open_handles() { [ "${busy_dir:-}" = "$1" ]; }
pgrep() { [ "${busy_process:-}" = "$2" ]; }
runtime_project_reason() { return 1; }
is_in_active_worktree() { return 1; }
record_item() { :; }
keep_item() { echo "  [KEEP $2] $1"; }
step_failed() { echo "unexpected error: $*" >&2; exit 1; }
get_size() { du -sh "$1" | cut -f1; }
get_size_bytes() { du -sk "$1" | cut -f1; }
bytes_to_human() { echo "${1}K"; }
eval "$(sed -n '/^remove_item() {/,/^}/p' "$SCRIPT_DIR/cleanup.sh")"

PROJECTS=("$project")
CLEAN_EVENT_FILE="$fixture/events"
EXPLICIT_PROJECTS=true
TARGET_AGE_DAYS=7
LAKE_AGE_DAYS=7
TOTAL_SIZE=0
CHECK_MODE=true
clean_project_lake_builds
[ -d "$project/proofs/.lake/build" ]
CHECK_MODE=false
busy_dir="$project"
clean_project_lake_builds
[ -d "$project/proofs/.lake/build" ]
busy_dir=""
clean_project_lake_builds
[ ! -e "$project/proofs/.lake/build" ]
[ -d "$project/proofs/.lake/packages" ]
[ -d "$project/.loom/worktrees/wt/proofs/.lake/build" ]

export CARGO_HOME="$fixture/.cargo"
unset CARGO_TARGET_DIR CARGO_BUILD_TARGET_DIR
shared="$fixture/shared targets"
mkdir -p "$CARGO_HOME"
echo 'build.target-dir = "shared targets"' > "$CARGO_HOME/config.toml"
[ "$(python3 "$SCRIPT_DIR/scripts/build-artifacts.py" target)" = "$shared" ]
echo "[build]" > "$CARGO_HOME/config"
echo "target-dir = 'legacy'" >> "$CARGO_HOME/config"
[ "$(python3 "$SCRIPT_DIR/scripts/build-artifacts.py" target)" = "$fixture/legacy" ]
export CARGO_TARGET_DIR="$shared"
[ "$(python3 "$SCRIPT_DIR/scripts/build-artifacts.py" target)" = "$shared" ]
mkdir -p "$shared"
printf 'Signature: 8a477f597d28d172789f06886806bc55\n# This file is a cache directory tag created by cargo.\n' > "$shared/CACHEDIR.TAG"
for profile in debug release x86_64-test/ci work/task/debug private; do
    mkdir -p "$shared/$profile/.fingerprint" "$shared/$profile/deps"
    echo artifact > "$shared/$profile/deps/build"
done
mkdir "$shared/unknown" "$shared/private/.git"
find "$shared" -exec touch -t 200101010000 {} +
touch "$shared/release/deps/build"
CHECK_MODE=true
clean_shared_target
[ -d "$shared/debug" ]
CHECK_MODE=false
busy_process=cargo
clean_shared_target
[ -d "$shared/debug" ]
busy_process=rustc
clean_shared_target
[ -d "$shared/debug" ]
busy_process=""
busy_dir="$shared/work/task/debug"
clean_shared_target
[ ! -d "$shared/debug" ]
[ ! -d "$shared/x86_64-test/ci" ]
[ -d "$shared/release" ]
[ -d "$busy_dir" ]
[ -d "$shared/private/.git" ]
[ -d "$shared/unknown" ]
[[ "$SHARED_CARGO_SUMMARY" =~ [1-9][0-9]*' bytes freed' ]]
echo 'Project activity, Lake builds, and shared Cargo target fixtures passed.'
