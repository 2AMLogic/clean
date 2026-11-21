# Sourced by cleanup.sh. Python discovery only reads configuration and files.
# Status 0 means keep. An inspection failure must never authorize deletion.
has_recent_project_activity() {
    local project="$1" days="$2"
    if ! PROJECT_ACTIVITY_REASON=$(python3 -B "$SCRIPT_DIR/scripts/build-artifacts.py" activity "$project" "$days"); then
        PROJECT_ACTIVITY_REASON="could not verify project activity"
    fi
    [ -n "$PROJECT_ACTIVITY_REASON" ]
}

clean_lake_builds() {
    local project="$1" builds="$2" build owner why
    if ! python3 -B "$SCRIPT_DIR/scripts/build-artifacts.py" lake "$project" "$EXPLICIT_PROJECTS" > "$builds"; then
        step_failed "$project" "could not enumerate Lake builds"
        return
    fi
    while IFS= read -r -d '' build; do
        owner=$(git -C "${build%/.lake/build}" rev-parse --show-toplevel 2>/dev/null) || owner="$project"
        if why=$(runtime_project_reason "$build"); then
            keep_item "$build" "$why"
        elif has_recent_project_activity "$owner" "$LAKE_AGE_DAYS"; then
            keep_item "$build" "recent project activity: $PROJECT_ACTIVITY_REASON"
        elif has_open_handles "$owner"; then
            keep_item "$build" "open handles"
        else
            remove_item "$build" "Lake build"
        fi
    done < "$builds"
}

clean_project_lake_builds() {
    local project builds="$CLEAN_EVENT_FILE.candidates"
    for project in "${PROJECTS[@]}"; do
        [ -d "$project" ] || continue
        clean_lake_builds "$project" "$builds"
    done
}

# pgrep exits 1 for no matches, and >=2 for an unavailable inspection.
cargo_may_be_running() {
    local cmd status
    for cmd in cargo rustc; do
        if pgrep -x "$cmd" >/dev/null; then return 0; else status=$?; fi
        [ "$status" -eq 1 ] || return 0
    done
    return 1
}

clean_shared_target() {
    local target candidates profile evidence before after size_delta
    local cargo_home="${CARGO_HOME:-$HOME/.cargo}"
    [ -n "${CARGO_TARGET_DIR:-}${CARGO_BUILD_TARGET_DIR:-}" ] || [ -f "$cargo_home/config" ] || [ -f "$cargo_home/config.toml" ] || return 0
    if ! target=$(python3 -B "$SCRIPT_DIR/scripts/build-artifacts.py" target); then
        step_failed "Cargo configuration" "could not resolve the shared target directory"
        return
    fi
    [ -n "$target" ] && [ -d "$target" ] || return 0
    target=$(cd "$target" && pwd -P) || return 0
    before=$(get_size_bytes "$target")
    SHARED_CARGO_SUMMARY="Shared Cargo target: $target ($(bytes_to_human "${before:-0}"))"
    echo "$SHARED_CARGO_SUMMARY"
    case "$(cd "$HOME" && pwd -P)/" in
        "$target"/|"$target"/*) keep_item "$target" "target contains the home directory"; return ;;
    esac
    if [ "$target" = / ]; then keep_item "$target" "target is the filesystem root"; return; fi
    if is_in_active_worktree "$target"; then
        keep_item "$target" "inside a registered worktree"
        return
    fi
    if cargo_may_be_running; then
        keep_item "$target" "cargo or rustc active, or process list unavailable"
        return
    fi
    candidates="$CLEAN_EVENT_FILE.candidates"
    if ! python3 -B "$SCRIPT_DIR/scripts/build-artifacts.py" profiles "$target" > "$candidates"; then
        step_failed "$target" "could not enumerate Cargo profiles"
        return
    fi
    if [ ! -s "$candidates" ]; then
        keep_item "$target" "no recognized Cargo profiles"
    fi
    while IFS= read -r -d '' profile; do
        # Recheck each profile just before removal. Preserve source checkouts
        # accidentally placed inside a cache, and mounted filesystems.
        if ! evidence=$(python3 -B "$SCRIPT_DIR/scripts/build-artifacts.py" profile-activity "$profile" "$TARGET_AGE_DAYS"); then
            keep_item "$profile" "could not verify profile contents"
        elif [ -n "$evidence" ]; then
            keep_item "$profile" "$evidence"
        elif has_open_handles "$profile"; then
            keep_item "$profile" "open handles"
        elif cargo_may_be_running; then
            keep_item "$profile" "cargo or rustc active, or process list unavailable"
        else
            remove_item "$profile" "stale shared Cargo profile"
        fi
    done < "$candidates"
    if [ "$CHECK_MODE" = false ]; then
        after=$(get_size_bytes "$target")
        size_delta=$((before - after))
        [ "$size_delta" -ge 0 ] || size_delta=0
        SHARED_CARGO_SUMMARY="Shared Cargo target: $target ($(bytes_to_human "${after:-0}"), $((size_delta * 1024)) bytes freed)"
    fi
}
