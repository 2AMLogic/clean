"""Read-only diagnostics for the macOS system phase."""
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from collections import defaultdict

GIB = 1024 ** 3
DAY = 86400


def human(size):
    if size >= GIB:
        return f"{size / GIB:.1f} GiB"
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.1f} MiB"
    return f"{size / 1024:.1f} KiB"


def disk_floor():
    value = os.environ.get("CLEAN_LOW_DISK_GIB", "30")
    if not value.isascii() or not value.isdigit() or len(value) > 9:
        raise ValueError("CLEAN_LOW_DISK_GIB must be a nonnegative integer")
    return int(value) * GIB


def query(argv, timeout=10):
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=timeout, check=True)
        return os.fsdecode(result.stdout).rstrip("\n")
    except (OSError, subprocess.SubprocessError):
        return None


def elapsed(value):
    days, _, clock = value.rpartition("-")
    seconds = 0
    for part in clock.split(":"):
        seconds = seconds * 60 + int(part)
    return int(days or 0) * DAY + seconds


def memory_report():
    print("\nMemory")
    swap = query(["sysctl", "-n", "vm.swapusage"])
    print(f"  Swap: {swap or 'unavailable'}")
    pressure = query(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
    levels = {"1": "normal", "2": "warning", "4": "critical"}
    print(f"  Memory pressure: {levels.get(pressure, pressure or 'unavailable')}")
    snapshot = query(["ps", "-axww", "-o", "pid=,etime=,rss=,comm="])
    if snapshot is None:
        print("  Process snapshot unavailable")
        return
    processes = []
    groups = defaultdict(list)
    for line in snapshot.splitlines():
        try:
            pid, age, rss, exe = line.strip().split(None, 3)
            proc = (int(rss) * 1024, elapsed(age), age, pid, exe)
        except ValueError:
            continue
        groups[exe].append(proc)
        processes.append(proc)
    print("  Executables with more than 50 processes (combined RSS and oldest elapsed time)")
    for exe, group in sorted(groups.items(), key=lambda entry: -len(entry[1])):
        if len(group) > 50:
            oldest = max(group, key=lambda proc: proc[1])[2]
            print(f"    {len(group)} × {exe}  {human(sum(p[0] for p in group))}  {oldest}")
    print("  Processes running temporary executables for over 6h (PID, RSS, elapsed, executable)")
    for rss, seconds, age, pid, exe in processes:
        if seconds > 6 * 3600 and exe.startswith(("/tmp/", "/private/tmp/")):
            print(f"    {pid}  {human(rss)}  {age}  {exe}")
    print("  Top five processes by RSS (PID, RSS, executable)")
    for rss, _, _, pid, exe in sorted(processes, reverse=True)[:5]:
        print(f"    {pid}  {human(rss)}  {exe}")


def footprint(path, now):
    """Count allocated bytes once per inode, and age regular files by mtime."""
    size = old30 = old90 = 0
    newest = None
    incomplete = False
    hardlinks = set()
    todo = [path]
    while todo:
        entry = todo.pop()
        try:
            st = entry.lstat()
            newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
            if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                key = (st.st_dev, st.st_ino)
                if key in hardlinks:
                    continue
                hardlinks.add(key)
            size += st.st_blocks * 512
            if stat.S_ISDIR(st.st_mode):
                with os.scandir(entry) as children:
                    todo.extend(Path(child.path) for child in children)
            elif stat.S_ISREG(st.st_mode):
                age = now - st.st_mtime
                old30 += age > 30 * DAY
                old90 += age > 90 * DAY
        except OSError:
            incomplete = True
    return size, newest, old30, old90, incomplete


def references(home, projects):
    """Only inspect configuration, binary symlinks and runtime overrides."""
    configs = [home / ".loom/config.json", home / ".loom/fleet.json",
               home / ".config/loom/config.json"]
    defaults = os.environ.get("LOOM_CONFIG_DEFAULTS_FILE", home / ".local/share/loom/config/defaults.json")
    if defaults:
        configs.append(Path(defaults))
    for project in projects:
        configs.extend(project / name for name in (
            ".loom/config.json", ".loom-project/project.json", ".loom-local/local.json",
            ".loom-local/fleet-repo.json",
            "loom.toml", ".loom.toml"))
    parts = []
    for p in configs:
        if p.is_file():
            try:
                parts.append(p.read_text(errors="replace"))
            except OSError:
                pass
    parts.extend(f"{key}={value}" for key, value in os.environ.items()
                 if key.startswith("LOOM_") and key.endswith(("_BIN", "_VERSION")))
    return "\n".join(parts)


def runtime_referenced(directory, configs):
    tool, _, version = directory.name.rpartition("-")
    binary = shutil.which(tool)
    if binary and directory in Path(binary).resolve().parents:
        return True
    # Require a full version to avoid opencode-1.2 matching opencode-1.20.
    if re.search(r"(?<![\w.-])" + re.escape(directory.name) + r"(?![\w.-])", configs):
        return True
    return bool(re.search(r'(?i)["\']?' + re.escape(tool) +
                          r'[_-]?version["\']?\s*[:=]\s*["\']?' +
                          re.escape(version) + r'(?![\w.-])', configs))


def date_text(timestamp):
    if timestamp is None:
        return "unknown"
    return datetime.datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def tool_homes(home, projects):
    now = time.time()
    elan = Path(os.environ.get("ELAN_HOME", home / ".elan"))
    rust = Path(os.environ.get("RUSTUP_HOME", home / ".rustup")) / "toolchains"
    paths = [home / ".codex/sessions", home / ".claude/projects", elan, rust]
    paths.extend((home / ".codex").glob("thread_history_*.sqlite"))
    paths.extend((home / ".local/state").glob("*"))
    runtimes = list((home / ".loom/opt").glob("*-*/xdg"))
    paths.extend(runtimes)
    configs = references(home, projects) if runtimes else ""
    rows = [(footprint(p, now), p) for p in paths if p.exists()]
    print("\nLarge tool homes (allocated size, newest mtime, files older than 30 / 90 days)")
    for (size, newest, old30, old90, incomplete), path in sorted(
            rows, key=lambda row: row[0][0], reverse=True):
        suffix = " (partial scan)" if incomplete else ""
        if path in runtimes and not runtime_referenced(path.parent, configs):
            suffix += " (no reference found in scanned Loom configs or active binary path)"
        print(f"  {human(size):>11}  {date_text(newest)}  {old30}/{old90}  {path}{suffix}")
        if path == elan or path == rust:
            if path == elan:
                cmd = "elan toolchain gc"
                result = query(["elan", "toolchain", "gc", "--json"])
                try:
                    names = json.loads(result or "{}")["unused_toolchains"]
                    candidates = [elan / "toolchains" / name.replace("/", "--").replace(":", "---")
                                  for name in names if isinstance(name, str) and ".." not in name]
                except (KeyError, TypeError, ValueError):
                    candidates = None
                label = "estimated reclaimable"
            else:
                cmd = "rustup toolchain list -v"
                result = query(["rustup", "toolchain", "list", "-v"])
                # Local overrides outside this workspace may still use these.
                candidates = None if result is None else [
                    rust / line.split()[0] for line in result.splitlines()
                    if line.strip() and "(active" not in line and "(default" not in line
                    and "/" not in line.split()[0] and line.split()[0] not in (".", "..")]
                label = "outside the active/default toolchains; verify project pins"
            estimate = "estimate unavailable" if candidates is None else (
                f"{human(sum(footprint(p, now)[0] for p in candidates))} {label}")
            print(f"    Review with `{cmd}` ({estimate})")
    if not rows:
        print("  No known tool homes found")


def known_images(folder):
    for pattern in ("Docker.raw", "data/Docker.raw", "*/data/Docker.raw", "vms/*/data/Docker.raw"):
        yield from folder.glob(pattern)


def docker_report(home, floor):
    default = home / "Library/Containers/com.docker.docker/Data/vms"
    settings = home / "Library/Group Containers/group.com.docker/settings-store.json"
    if not default.exists() and not settings.exists() and not shutil.which("docker"):
        return
    folder = default
    certain = True
    if settings.exists():
        try:
            config = json.loads(settings.read_text())
            value = config.get("DataFolder") or config.get("dataFolder")
            if value:
                folder = Path(value).expanduser()
                if not folder.is_absolute():
                    raise ValueError("relative DataFolder")
        except (OSError, ValueError, AttributeError, TypeError):
            print("  Docker Desktop settings unreadable; configured data folder unknown")
            folder = default
            certain = False
    folder = folder.resolve()
    current = set(path.resolve() for path in known_images(folder) if path.is_file())
    found = {path.resolve() for path in known_images(default) if path.is_file()}
    indexed = query(["mdfind", "-0", 'kMDItemFSName == "Docker.raw"'])
    if indexed:
        found.update(Path(name).resolve() for name in indexed.split("\0") if name)
    for root in os.environ.get("CLEAN_DOCKER_SEARCH_ROOTS", "").split(os.pathsep):
        if root and Path(root).is_absolute():
            found.update(p.resolve() for p in Path(root).rglob("Docker.raw"))
    print("\nDocker Desktop storage")
    print(f"  Data folder: {folder}")
    if not shutil.which("docker"):
        print("  Docker CLI unavailable. Check free space here before starting Docker Desktop.")
    for path in sorted(current | found):
        try:
            st = path.stat()
        except OSError:
            continue
        if path.name != "Docker.raw" or not stat.S_ISREG(st.st_mode):
            continue
        inside = path in current or folder in path.parents
        kind = "Current" if inside and certain else "Outside current data folder" if certain else "Found"
        print(f"  {kind}: {path}  {human(st.st_blocks * 512)} allocated  modified {date_text(st.st_mtime)}")
    if not current:
        print("  No disk image found in the usual Docker Desktop layouts in the data folder")
    # If a custom volume disappeared, measuring the nearest existing ancestor
    # could instead report free space on the boot volume.
    try:
        free = shutil.disk_usage(folder).free
        print(f"  Data volume free: {human(free)}")
        if free < floor:
            print(f"  LOW DISK: below {human(floor)} on {folder}")
    except OSError:
        print(f"  Volume free space unavailable for {folder}")


if __name__ == "__main__":
    home = Path.home()
    try:
        floor = disk_floor()
    except ValueError as err:
        sys.exit(str(err))
    memory_report()
    tool_homes(home, [Path(p) for p in sys.argv[1:]])
    docker_report(home, floor)
