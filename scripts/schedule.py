"""Manage and run clean's optional per-user macOS LaunchAgent."""
import argparse
import datetime
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile

from system_report import disk_floor, human

LABEL = "io.github.2amlogic.clean"
SCRIPT = Path(__file__).resolve().parents[1] / "cleanup.sh"


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        name = out.name
        try:
            out.write(data)
            out.flush()
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def launchctl(*args, check=True):
    return subprocess.run(["launchctl", *args], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, check=check)


def installed(domain):
    return launchctl("print", domain + "/" + LABEL, check=False).returncode == 0


def install(plist, log, domain, interval):
    disk_floor()  # Validate the floor before installing the job.
    log.parent.mkdir(parents=True, exist_ok=True)
    paths = [p for p in os.get_exec_path() if os.path.isabs(p)]
    paths += ["/usr/bin", "/bin", "/usr/sbin", "/sbin", "/opt/homebrew/bin", "/usr/local/bin"]
    env = {k: v for k, v in os.environ.items() if k in (
        "CLEAN_LOW_DISK_GIB", "CLEAN_PROJECT_DIR", "CLEAN_PROJECTS", "CLEAN_KEEP_PROJECTS",
        "CLEAN_SCRATCH_ROOTS", "CLEAN_DOCKER_SEARCH_ROOTS", "CARGO_TARGET_DIR"
    ) or (k.startswith("CLEAN_") and k.endswith(("_DAYS", "_HOURS")))}
    env.update(PATH=os.pathsep.join(dict.fromkeys(paths)), GH_PROMPT_DISABLED="1",
               GIT_TERMINAL_PROMPT="0", HOMEBREW_NO_AUTO_UPDATE="1")
    desired = plistlib.dumps({
        "Label": LABEL, "ProgramArguments": [str(SCRIPT), "schedule", "run"],
        "StartInterval": interval * 3600, "StandardOutPath": str(log),
        "StandardErrorPath": str(log), "ProcessType": "Background",
        "EnvironmentVariables": env,
    })
    previous = plist.read_bytes() if plist.exists() else None
    loaded = installed(domain)
    if loaded and desired == previous:
        print(f"Schedule already loaded (every {interval}h)")
        return
    if loaded:
        launchctl("bootout", domain + "/" + LABEL)
    try:
        atomic_write(plist, desired)
        launchctl("bootstrap", domain, str(plist))
    except (OSError, subprocess.CalledProcessError):
        if previous is not None:
            atomic_write(plist, previous)
            if loaded:
                launchctl("bootstrap", domain, str(plist))
        elif plist.exists():
            plist.unlink()
        raise
    print(f"Scheduled every {interval}h. Log: {log}")


def scheduled_run(home, state):
    floor = disk_floor()
    started = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    before = shutil.disk_usage(home).free
    print(f"Scheduled clean started {started}; {human(before)} free on {home}", flush=True)
    try:
        status = subprocess.run([str(SCRIPT)], stdin=subprocess.DEVNULL).returncode
    except OSError as err:
        print(f"Scheduled clean failed: {err}", file=sys.stderr)
        status = 1
    if status < 0:
        status = 128 - status
    after = shutil.disk_usage(home).free
    ended = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    result = dict(started=started, finished=ended, exit_code=status,
                  free_before_bytes=before, free_after_bytes=after)
    print(f"Scheduled clean finished {ended}; exit {status}; {human(after)} free on {home}")
    if after < floor:
        print(f"LOW DISK: {human(after)} free on {home}, below {human(floor)}")
    atomic_write(state, json.dumps(result).encode() + b"\n")
    return status


def main(args):
    parser = argparse.ArgumentParser(prog="clean schedule")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("install")
    setup.add_argument("--interval-hours", default=24, type=int)
    commands.add_parser("status")
    commands.add_parser("uninstall")
    commands.add_parser("run")
    options = parser.parse_args(args)
    if sys.platform != "darwin":
        parser.error("launchd schedules require macOS")
    domain = f"gui/{os.getuid()}"
    home = Path.home()
    plist = home / f"Library/LaunchAgents/{LABEL}.plist"
    log = home / "Library/Logs/clean.log"
    state = home / "Library/Application Support/clean/last-run.json"
    if options.command == "install":
        if not 1 <= options.interval_hours <= (2 ** 31 - 1) // 3600:
            parser.error("interval-hours must fit in a positive launchd interval")
        install(plist, log, domain, options.interval_hours)
    elif options.command == "uninstall":
        if installed(domain):
            launchctl("bootout", domain + "/" + LABEL)
        if plist.exists():
            plist.unlink()
        print("Schedule uninstalled")
    elif options.command == "status":
        print(f"Schedule: {'loaded' if installed(domain) else 'unloaded'}")
        if state.exists():
            result = json.loads(state.read_text())
            print(f"Last run: {result['finished']}, exit {result['exit_code']}, "
                  f"{human(result['free_after_bytes'])} free")
        else:
            print("Last run: none recorded")
        print(f"Log: {log}")
    else:
        return scheduled_run(home, state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except subprocess.CalledProcessError as err:
        sys.exit(f"launchctl failed ({err.returncode}): {err.stderr.strip()}")
    except (ValueError, OSError) as err:
        sys.exit(str(err))
