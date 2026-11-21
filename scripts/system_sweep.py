"""System cache and scratch passes for cleanup.sh (Python 3.8+)."""

import argparse
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

from scratch import ProcessSnapshot, event, human, option, scratch_areas, sweep


def allocated(path):
    if not path.exists():
        return 0
    return int(subprocess.check_output(["du", "-sk", str(path)]).split()[0]) * 1024


def tool_cache(path, command, preview):
    if not path.is_dir() or path.is_symlink():
        return
    before = allocated(path)
    print(f"  {path}: {human(before)} before")
    if preview:
        event("would_remove", path, " ".join(command), before)
    else:
        subprocess.run(command, cwd="/", check=True)
        after = allocated(path)
        print(f"  {path}: {human(after)} after")
        event("removed", path, " ".join(command), max(0, before - after))


def npm_caches(preview, processes, days):
    if not shutil.which("npm"):
        return
    # Suppress npm's own log-file rotation during discovery in --check.
    root = Path(os.fsdecode(subprocess.check_output(
        ["npm", "config", "get", "cache", "--logs-max=0", "--update-notifier=false"], cwd="/")).strip())
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("npm reported an invalid cache root")
    tool_cache(root / "_cacache", ["npm", "cache", "clean", "--force", "--cache", str(root)], preview)
    npx = root / "_npx"
    if npx.is_dir() and not npx.is_symlink():
        print(f"  {npx}: {human(allocated(npx))} before")
        sweep(npx, "npx", days, preview, processes)
        if not preview:
            print(f"  {npx}: {human(allocated(npx))} after")


def go_cache(preview):
    if not shutil.which("go"):
        return
    root = Path(os.fsdecode(subprocess.check_output(["go", "env", "GOCACHE"], cwd="/")).strip())
    if root.is_absolute():
        tool_cache(root, ["go", "clean", "-cache"], preview)


def cache_objects(root, prefix=""):
    # sccache uses hex shards, followed by the full digest. The preprocessor
    # cache uses an additional shard on some versions. Preserve unrelated
    # entries even if SCCACHE_DIR was pointed at a shared directory.
    for path in root.iterdir():
        if path.is_symlink():
            continue
        if len(prefix) < 3 and re.fullmatch("[a-f0-9]", path.name) and path.is_dir():
            yield from cache_objects(path, prefix + path.name)
        elif len(prefix) >= 2 and path.name.startswith(prefix) and re.fullmatch("[0-9a-f]{16,128}", path.name):
            st = path.lstat()
            if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
                yield path, st


def compiler_cache(preview, aggressive, threshold):
    root = Path(os.environ.get("SCCACHE_DIR", Path.home() / "Library/Caches/Mozilla.sccache"))
    if not shutil.which("sccache") or not root.is_dir() or root.is_symlink():
        return
    if not root.is_absolute() or root == Path.home() or root in Path.home().parents:
        raise ValueError("invalid SCCACHE_DIR")
    root = root.resolve()
    entries = list(cache_objects(root))
    preprocessor = root / "preprocessor"
    if preprocessor.is_dir() and not preprocessor.is_symlink():
        entries.extend(cache_objects(preprocessor))
    size = sum(st.st_blocks * 512 for _, st in entries)
    print(f"  sccache: {root} ({human(size)} in cached objects)")
    if not entries or (not aggressive and size < threshold * 1024 * 1024):
        event("kept", root, f"sccache below {threshold} MiB or empty", size)
        return
    if preview:
        event("would_remove", root, "sccache objects", size)
        return
    stop = subprocess.run(["sccache", "--stop-server"], capture_output=True)
    if stop.returncode and b"connection refused" not in (stop.stderr + stop.stdout).lower():
        raise RuntimeError("sccache server stop failed: " + os.fsdecode(stop.stderr).strip())
    removed = 0
    for path, st in entries:
        # A server restarted or a shard was replaced during our scan.
        if path.resolve() != path or not path.exists():
            continue
        now = path.lstat()
        if os.path.samestat(st, now) and st.st_mtime_ns == now.st_mtime_ns:
            path.unlink()
            removed += st.st_blocks * 512
    event("removed", root, "sccache objects", removed)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["check", "clean"], required=True)
    parser.add_argument("--aggressive", action="store_true")
    args = parser.parse_args()
    preview = args.mode == "check"
    processes = ProcessSnapshot()
    try:
        tmp_days = option("CLEAN_TMP_AGE_DAYS", 7)
        session_days = option("CLEAN_SESSION_AGE_DAYS", 3)
        scratch_days = option("CLEAN_SCRATCH_AGE_DAYS", 7)
        npx_days = option("CLEAN_NPX_AGE_DAYS", 7)
        threshold = option("CLEAN_SCCACHE_MIN_MIB", 1024)
    except ValueError as exc:
        event("failed", "configuration", str(exc))
        return 1

    failures = 0
    for name, run, arguments in (
        ("npm caches", npm_caches, (preview, processes, npx_days)),
        ("Go build cache", go_cache, (preview,)),
        ("sccache", compiler_cache, (preview, args.aggressive, threshold)),
        ("Scratch directories", scratch_areas, (preview, processes, tmp_days, session_days, scratch_days)),
    ):
        print(name, flush=True)
        try:
            run(*arguments)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            event("failed", name, str(exc))
            failures += 1
    return int(bool(failures))


if __name__ == "__main__":
    sys.exit(main())
