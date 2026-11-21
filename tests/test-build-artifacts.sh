#!/bin/bash
set -eu
SCRIPT_DIR=$(cd "$(dirname "$0")/.." && pwd -P)
source "$SCRIPT_DIR/scripts/build-artifacts.sh"
source "$SCRIPT_DIR/scripts/terminal.sh"
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
printf '.lake/\n.venv/\ntarget/\n' > "$project/.gitignore"
git -C "$project" add source.c .gitignore
GIT_AUTHOR_DATE=2001-01-01T00:00:00Z GIT_COMMITTER_DATE=2001-01-01T00:00:00Z git -C "$project" commit -qm 'old commit'
mkdir -p "$project/.loom-local" "$project/.loom/locks" "$project/node_modules"
touch -t 200101010000 "$project/source.c" "$project/.gitignore"
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
get_size_kib() { du -sk "$1" | cut -f1; }
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

# A Lake output directory can also hold authored files, checkout metadata or a
# user-owned clone. A clean, old project and a clear process snapshot cannot
# authorize removing those.
protected="$project/proofs/.lake/build"
mkdir -p "$protected"
echo tracked > "$protected/keep.lean"
git -C "$project" add -f "$protected/keep.lean"
GIT_AUTHOR_DATE=2001-01-02T00:00:00Z GIT_COMMITTER_DATE=2001-01-02T00:00:00Z git -C "$project" commit -qm 'tracked build contents'
clean_project_lake_builds
[ -f "$protected/keep.lean" ]
git -C "$project" rm --quiet --cached "$protected/keep.lean"
mkdir -p "$protected/nested"
echo 'gitdir: absent-worktree-metadata' > "$protected/nested/.git"
clean_project_lake_builds
[ -e "$protected/nested/.git" ]
unlink "$protected/nested/.git"
git init -q "$protected/nested"
echo 'in progress' > "$protected/nested/only-copy"
clean_project_lake_builds
[ -f "$protected/nested/only-copy" ]
mv "$protected/nested" "$fixture/kept-clone"
test_id=$(python3 -c 'import os,sys; s=os.stat(sys.argv[1]);print(f"{s.st_dev}:{s.st_ino}")' "$protected")
mv "$project/proofs/.lake" "$fixture/actual-lake"
ln -s "$fixture/actual-lake" "$project/proofs/.lake"
if verify_build "$protected" "$test_id" "$project" lake; then echo 'followed a replaced parent symlink'; exit 1; fi
unlink "$project/proofs/.lake"
mv "$fixture/actual-lake" "$project/proofs/.lake"

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
# Exercise the installed script entry point and its scheduler-facing events.
# The project phase scopes every mutation to this fixture. Mock lsof so an
# unrelated process inspecting the test tree cannot make the run nondeterministic.
mkdir -p "$fixture/bin" "$fixture/home" "$project/proofs/.lake/build"
ln -s "$SCRIPT_DIR/cleanup.sh" "$fixture/bin/clean"
printf '#!/bin/sh\nexit 1\n' > "$fixture/bin/lsof"
chmod +x "$fixture/bin/lsof"
printf 'compiled module' > "$project/proofs/.lake/build/example.olean"
run_project() {
    env HOME="$fixture/home" PATH="$fixture/bin:/usr/bin:/bin" \
        "$fixture/bin/clean" --project "$project" --projects-only --json "$@" \
        > "$fixture/run.json" 2> "$fixture/run.log" || { cat "$fixture/run.log" >&2; return 1; }
}
run_project --check
python3 - "$fixture/run.json" "$project/proofs/.lake/build" <<'PYTEST'
import json, sys
result = json.load(open(sys.argv[1]))
assert any(i["status"] == "would_remove" and i["path"] == sys.argv[2] and i["bytes"] > 0
           for i in result["items"]), result
PYTEST
[ -f "$project/proofs/.lake/build/example.olean" ]
touch "$project/source.c"
run_project
python3 - "$fixture/run.json" "$project/proofs/.lake/build" <<'PYTEST'
import json, sys
result = json.load(open(sys.argv[1]))
assert any(i["status"] == "kept" and i["path"] == sys.argv[2] and "source.c" in i["reason"]
           for i in result["items"]), result
PYTEST
[ -f "$project/proofs/.lake/build/example.olean" ]
touch -t 200101010000 "$project/source.c"
run_project
[ ! -d "$project/proofs/.lake/build" ]
[ -d "$project/proofs/.lake/packages" ]
# Git primaries can reclaim recognized, ignored Cargo and venv output. Linked
# checkouts and directories with the right name but no build metadata are kept.
echo '[package]' > "$project/Cargo.toml"
git -C "$project" add Cargo.toml
GIT_AUTHOR_DATE=2001-01-03T00:00:00Z GIT_COMMITTER_DATE=2001-01-03T00:00:00Z git -C "$project" commit -qm 'package'
touch -t 200101010000 "$project/Cargo.toml"
mkdir -p "$project/.venv" "$project/target/debug"
echo 'environment' > "$project/.venv/pyvenv.cfg"
echo 'compiled' > "$project/target/debug/package"
cp "$shared/CACHEDIR.TAG" "$project/target/CACHEDIR.TAG"
find "$project/.venv" "$project/target" -exec touch -t 200101010000 {} +
printf '#!/bin/sh\nexit 1\n' > "$fixture/bin/pgrep"
chmod +x "$fixture/bin/pgrep"
run_project --check
python3 - "$fixture/run.json" "$project" <<'PYTEST'
import json, pathlib, sys
data = json.load(open(sys.argv[1]))
candidates = {i["path"] for i in data["items"] if i["status"] == "would_remove"}
assert all(str(pathlib.Path(sys.argv[2], d)) in candidates for d in (".venv", "target")), data
PYTEST
orig_project="$project"
project="$fixture/linked"
git -C "$orig_project" worktree add --quiet --detach "$project" HEAD
mkdir -p "$project/.venv" "$project/target/debug"
cp "$orig_project/.venv/pyvenv.cfg" "$project/.venv/"
cp "$shared/CACHEDIR.TAG" "$project/target/"
find "$project" -exec touch -t 200101010000 {} +
run_project
[ -d "$project/.venv" ] && [ -d "$project/target" ]
project="$orig_project"
run_project
[ ! -d "$project/.venv" ] && [ ! -d "$project/target" ]
mkdir -p "$project/.venv" "$project/target"
find "$project/.venv" "$project/target" -exec touch -t 200101010000 {} +
run_project
[ -d "$project/.venv" ] && [ -d "$project/target" ]
# Unlinking an earlier item changes its parent directory's mtime. That
# bookkeeping must not make an idle non-git project active halfway through.
project="$fixture/nongit"
make_nongit() {
    mkdir -p "$project/proofs/.lake/build" "$project/node_modules" "$project/.venv"
    echo compiled > "$project/proofs/.lake/build/proof.olean"
    echo bytecode > "$project/old.pyc"
    find "$project" -exec touch -t 200101010000 {} +
}
make_nongit
run_project --check
cp "$fixture/run.json" "$fixture/preview.json"
run_project
python3 - "$fixture/preview.json" "$fixture/run.json" "$project" <<'PYTEST'
import json, sys
from pathlib import Path
def paths(report, status):
    return {Path(i["path"]) for i in json.load(open(report))["items"] if i["status"] == status}
expected = {Path(sys.argv[3], p) for p in ("old.pyc", "node_modules", ".venv", "proofs/.lake/build")}
assert paths(sys.argv[1], "would_remove") == paths(sys.argv[2], "removed") == expected
PYTEST
make_nongit
# Simulate a user writing source while another item is being removed.
cat > "$fixture/bin/rm" <<'RMSTUB'
#!/bin/bash
case ${!#} in */old.pyc) touch "${!#}.new-source" ;; esac
exec /bin/rm "$@"
RMSTUB
chmod +x "$fixture/bin/rm"
run_project
[ -f "$project/proofs/.lake/build/proof.olean" ]
grep -q 'new-source' "$fixture/run.json"
# Different thresholds can remove the only originally recent file before the
# Lake pass. Keep its original evidence during the apply run.
unlink "$fixture/bin/rm"
project="$fixture/mixed-thresholds"
make_nongit
touch -t "$(date -v-4d +%Y%m%d%H%M)" "$project/old.pyc"
export CLEAN_PYCACHE_AGE_DAYS=2
run_project --check
run_project
[ ! -e "$project/old.pyc" ]
[ -f "$project/proofs/.lake/build/proof.olean" ]
grep -q 'old.pyc' "$fixture/run.json"
echo 'Project activity, Lake builds, and shared Cargo target fixtures passed.'
