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
from terminal import styled


def allocated(path):
    if not path.exists():
        return 0
    return int(subprocess.check_output(["du", "-sk", str(path)]).split()[0]) * 1024


def tool_cache(path, command, preview, env=None):
    if not path.is_dir() or path.is_symlink():
        return
    before = allocated(path)
    print(f"  {path} ({human(before)} before)", flush=True)
    if preview:
        event("would_remove", path, " ".join(command), before)
    else:
        subprocess.run(command, cwd="/", check=True, env=env)
        after = allocated(path)
        freed = max(0, before - after)
        event("removed", path, " ".join(command), freed, quiet=True)
        print(f"  {styled('Freed', 'removed')} {human(freed)} ({human(after)} after)")


def npm_cache_root():
    # npm creates its effective cache even for 'npm config get cache'. Read
    # the path settings directly so previewing an empty machine is read-only.
    configured = os.environ.get("npm_config_cache") or os.environ.get("NPM_CONFIG_CACHE")
    if configured:
        return Path(configured).expanduser()
    prefix = os.environ.get("npm_config_prefix") or os.environ.get("NPM_CONFIG_PREFIX")
    prefix = Path(prefix) if prefix else Path(shutil.which("npm")).parent.parent
    root = Path.home() / ".npm"
    for name, default in (("globalconfig", prefix / "etc/npmrc"), ("userconfig", Path.home() / ".npmrc")):
        key = "npm_config_" + name
        config = Path(os.environ.get(key) or os.environ.get(key.upper()) or default)
        if config.is_file():
            for line in config.read_text().splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip().lower() == "cache":
                    root = Path(os.path.expandvars(value.strip().strip('"').strip("'"))).expanduser()
    return root


def npm_caches(preview, processes, days):
    if not shutil.which("npm"):
        return
    root = npm_cache_root()
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
    # Even 'go env GOCACHE' writes telemetry in a pristine HOME. The macOS
    # default and the go env -w user file are sufficient for a read-only probe.
    configured = os.environ.get("GOCACHE")
    config = Path(os.environ.get("GOENV") or Path.home() / "Library/Application Support/go/env")
    if not configured and str(config) != "off" and config.is_file():
        for line in config.read_text().splitlines():
            if line.startswith("GOCACHE="):
                configured = line[len("GOCACHE="):]
    root = Path(configured or Path.home() / "Library/Caches/go-build")
    if root.is_absolute():
        tool_cache(root, ["go", "clean", "-cache"], preview, dict(os.environ, GOCACHE=str(root)))


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
        print(styled(name, "step"), flush=True)
        try:
            run(*arguments)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            event("failed", name, str(exc))
            failures += 1
    return int(bool(failures))


if __name__ == "__main__":
    sys.exit(main())
