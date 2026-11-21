"""Inspect and sweep the user's scratch areas without following links into other trees."""

import bisect
from collections import defaultdict
from functools import lru_cache
import fnmatch
import pwd
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
import uuid


MKTEMP = re.compile(r"(.+)[.]([a-zA-Z0-9]{6,16})$")
HEX_HASH = re.compile(r"[0-9a-f]{8,64}$")


def human(size):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:g} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def event(status, path, reason, size=0, *, quiet=False):
    if not quiet:
        print(f"  [{status}] {path} ({reason}, {human(size)})")
    log = os.environ.get("CLEAN_EVENT_FILE")
    if log:
        with open(log, "ab") as out:
            out.write(b"".join(os.fsencode(str(f)) + b"\0" for f in (status, path, reason, size)))


def option(name, default):
    value = os.environ.get(name, str(default))
    if not value.isascii() or not value.isdigit() or not 0 < int(value) <= 1000000:
        raise ValueError(f"{name} must be an integer between 1 and 1000000")
    return int(value)


class ProcessSnapshot:
    """Index open paths once for all candidates, including process working directories."""

    def __init__(self):
        self.updated = 0
        self.paths = []
        self.commands = []
        self.directory_commands = {}
        self.error = ""

    def busy(self, path):
        if time.monotonic() - self.updated > 30 or not self.updated:
            self.updated = time.monotonic()
            self.directory_commands.clear()
            try:
                handles = subprocess.run(["lsof", "-nP", "-F0n"], capture_output=True, check=True, timeout=30)
                if handles.stderr:
                    raise OSError("incomplete lsof scan")
                names = (f.lstrip(b"\n") for f in handles.stdout.split(b"\0"))
                self.paths = sorted({os.fsdecode(f[1:]) for f in names if f.startswith(b"n/")})
                commands = subprocess.check_output(["ps", "-axww", "-o", "command="], timeout=15)
                self.commands = os.fsdecode(commands).splitlines()
                if not self.paths or not self.commands:
                    raise OSError("empty process snapshot")
                self.error = ""
            except (OSError, subprocess.SubprocessError) as exc:
                self.error = str(exc)
        if self.error:
            raise OSError(self.error)
        name = str(path)
        for alias in (name, name[8:] if name.startswith("/private/") else name):
            for prefix in (alias, alias + "/"):
                pos = bisect.bisect_left(self.paths, prefix)
                if pos < len(self.paths) and (self.paths[pos] == alias or self.paths[pos].startswith(alias + "/")):
                    return "open handles"
            # ps output can be megabytes long. Restrict the search once per
            # root instead of rescanning every process for every temp entry.
            parent = os.path.dirname(alias) + "/"
            if parent not in self.directory_commands:
                self.directory_commands[parent] = "\n".join(line for line in self.commands if parent in line)
            if alias in self.directory_commands[parent]:
                return "running process references path"
        return ""


def inspect(path, cutoff, *, size_recent=False):
    # lstat/scandir propagate errors and never follow a symlink. Dir mtimes alone
    # cannot reveal recent writes to existing files deep inside a session.
    size = 0
    recent = False
    repos = []
    reason = ""
    device = path.lstat().st_dev
    stack = [path]
    while stack:
        item = stack.pop()
        st = item.lstat()
        size += st.st_blocks * 512
        recent |= st.st_mtime >= cutoff
        if recent and not size_recent:
            return 0, "recent activity", repos
        if st.st_uid != os.getuid() or st.st_dev != device:
            return size, "other owner or mounted filesystem", repos
        if item.name == ".git":
            if not stat.S_ISDIR(st.st_mode):
                return size, "linked worktree or external git directory", repos
            repos.append(item.parent)
        if stat.S_ISDIR(st.st_mode):
            with os.scandir(item) as entries:
                children = [Path(entry.path) for entry in entries]
            names = {child.name for child in children}
            if item.name != ".git" and {"HEAD", "objects", "refs"} <= names:
                return size, "bare Git repository", repos
            stack.extend(children)
        elif stat.S_ISSOCK(st.st_mode):
            reason = "socket"
        elif not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
            reason = "special file"
    return size, reason or ("recent activity" if recent else ""), repos


@lru_cache(maxsize=1)
def repo_environment_keys():
    return os.fsdecode(subprocess.check_output(["git", "rev-parse", "--local-env-vars"], cwd="/")).splitlines()


def has_work(repo):
    # Repo-local environment inherited from a git hook can silently redirect
    # git -C to an unrelated index, worktree or object store.
    env = os.environ.copy()
    for key in repo_environment_keys():
        env.pop(key, None)

    def git(*args):
        return subprocess.check_output(["git", "--no-optional-locks", "-C", str(repo), *args],
                                       stderr=subprocess.DEVNULL, env=env)
    actual = Path(os.fsdecode(git("rev-parse", "--show-toplevel").rstrip(b"\n")))
    if actual.resolve() != repo.resolve():
        return True
    # Include detached HEAD, stashes and local refs. External worktrees
    # depend on the object store even when every commit is on a remote.
    return (git("status", "--porcelain", "--untracked-files=all")
            or git("rev-list", "-1", "HEAD", "--all", "--not", "--remotes")
            or git("worktree", "list", "--porcelain").count(b"worktree ") != 1)


def prefix(name):
    match = MKTEMP.fullmatch(name)
    if match:
        return match[1] + ".*"
    return re.sub(r"[-.][a-zA-Z0-9_]+$", "-*", name)


def mktemp_name(name):
    match = MKTEMP.fullmatch(name)
    if not match:
        return False
    suffix = match[2]
    # A plain extension such as ".backup" is also six letters long.
    return ((not suffix.isalpha() and not suffix.isdigit())
            or (suffix != suffix.lower() and suffix != suffix.upper()))


def exclusion(name):
    return (name.startswith((".", "com.apple.")) or name.endswith((".pid", ".lock"))
            or name in ("TemporaryItems", "node-compile-cache"))


def candidates(root, kind):
    pytest_root = "pytest-of-" + pwd.getpwuid(os.getuid()).pw_name
    for path in sorted(root.iterdir()):
        if kind == "tmp" and path.name == pytest_root:
            # Use the same recency sanity check for the entire system temp
            # sweep, including separately aged pytest children.
            if (not path.is_symlink() and path.is_dir() and path.stat().st_uid == os.getuid()
                    and path.stat().st_dev == root.stat().st_dev):
                yield from (child for child in sorted(path.iterdir()) if re.fullmatch(r"pytest-\d+", child.name))
        else:
            yield path


def sweep(root, kind, days, preview, processes):
    if not root.is_dir() or root.is_symlink():
        return
    root = root.resolve()
    if root == Path.home().resolve() or root in Path.home().resolve().parents:
        event("skipped", root, "home or an ancestor")
        return
    if kind == "scratch":
        # A configured build volume can contain source trees. A scratch root
        # itself inside a checkout is ambiguous, even if its children look random.
        if any((d / ".git").exists() for d in (root, *root.parents)):
            event("skipped", root, "scratch root inside Git checkout")
            return
    device = root.stat().st_dev
    cutoff = time.time() - days * 86400
    pending = []
    groups = defaultdict(lambda: [0, 0])
    kept_recent = 0
    journal = os.environ.get("CLEAN_EVENT_FILE")
    journal_dir = Path(journal).resolve().parent if journal else None
    for path in candidates(root, kind):
        # Our own fresh journal cannot establish that an otherwise stale
        # system temp directory passes its recent-activity sanity check.
        if path == journal_dir:
            continue
        if exclusion(path.name) or path.is_symlink() or not (path.is_dir() or path.is_file()):
            event("kept", path, "protected name or file type", quiet=kind == "tmp")
            continue
        if kind == "scratch":
            pattern = os.environ.get("CLEAN_SCRATCH_PATTERN")
            matches = fnmatch.fnmatchcase(path.name, pattern) if pattern else mktemp_name(path.name)
            if not path.is_dir() or not matches:
                event("kept", path, "name outside scratch pattern")
                continue
        if kind == "session":
            try:
                if not path.is_dir() or str(uuid.UUID(path.name)) != path.name.lower():
                    continue
            except ValueError:
                continue
        if kind == "npx" and not (path.is_dir() and HEX_HASH.fullmatch(path.name)):
            event("kept", path, "outside npx hash pattern")
            continue
        try:
            identity = path.lstat()
            if identity.st_dev != device or path.is_mount():
                event("kept", path, "mounted filesystem")
                continue
            if identity.st_uid != os.getuid():
                event("kept", path, "different owner", quiet=kind == "tmp")
                continue
            size, reason, repos = inspect(path, cutoff, size_recent=kind == "session")
            if not reason and any(has_work(repo) for repo in repos):
                reason = "Git work or other worktrees"
            if not reason:
                reason = processes.busy(path)
        except (OSError, subprocess.SubprocessError) as exc:
            event("kept", path, f"inspection unavailable: {exc}")
            continue
        if reason:
            kept_recent += int(reason == "recent activity")
            event("kept", path, reason, size, quiet=kind == "tmp")
        else:
            pending.append((path, identity, size))
    # In the system temp dir, hundreds of unexpectedly "old" entries and no
    # recent activity indicate a bad clock or scan. Fail closed on that root.
    if not preview and pending:
        processes.updated = 0  # refresh the snapshot for the removal pass
    for path, identity, size in pending:
        if kind == "tmp" and not kept_recent:
            event("kept", path, "no recent entries in system temp scan")
            continue
        if not preview:
            try:
                if not os.path.samestat(identity, path.lstat()):
                    event("kept", path, "entry replaced during scan")
                    continue
                size, reason, repos = inspect(path, cutoff, size_recent=kind == "session")
                if not reason and any(has_work(repo) for repo in repos):
                    reason = "Git work or other worktrees"
                if not reason:
                    reason = processes.busy(path)
            except (OSError, subprocess.SubprocessError) as exc:
                event("kept", path, f"inspection unavailable: {exc}")
                continue
            if reason:
                event("kept", path, reason, size)
                continue
            try:
                if stat.S_ISDIR(identity.st_mode):
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except PermissionError:
                event("skipped", path, "protected by filesystem permissions")
                continue
        group = groups[prefix(path.name)]
        group[0] += 1
        group[1] += size
        event("would_remove" if preview else "removed", path, kind + " idle", size, quiet=kind == "tmp")
    for name, (count, size) in sorted(groups.items(), key=lambda group: -group[1][1]):
        print(f"  {count} × {name}  {human(size)} ({root})")


def scratch_areas(preview, processes, tmp_days, session_days, scratch_days):
    system_tmp = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True)
    system = os.fsdecode(system_tmp.stdout).strip() if system_tmp.returncode == 0 else ""
    temp = os.environ.get("TMPDIR") or system
    seen = set()
    def sweep_root(path, kind, days):
        try:
            root = path.resolve()
            if root in seen or root == Path("/tmp").resolve():
                return
            sweep(path, kind, days, preview, processes)
            seen.add(root)
        except PermissionError as exc:
            event("skipped", path, f"inspection denied: {exc}")

    if temp and os.path.isabs(temp):
        # Only the OS-designated per-user temporary directory is eligible for
        # broad sweeping. A custom TMPDIR gets the narrower configured-root rules.
        root = Path(temp).resolve()
        kind = "tmp" if system and root == Path(system).resolve() else "scratch"
        sweep_root(root, kind, tmp_days)

    claude = Path("/private/tmp") / f"claude-{os.getuid()}"
    try:
        if claude.is_dir() and not claude.is_symlink() and claude.stat().st_uid == os.getuid():
            for project in claude.iterdir():
                try:
                    if project.is_dir() and not project.is_symlink() and project.stat().st_uid == os.getuid():
                        sweep_root(project, "session", session_days)
                except PermissionError as exc:
                    event("skipped", project, f"inspection denied: {exc}")
    except PermissionError as exc:
        event("skipped", claude, f"inspection denied: {exc}")

    for value in os.environ.get("CLEAN_SCRATCH_ROOTS", "").split(os.pathsep):
        if not value:
            continue
        if not os.path.isabs(value):
            raise ValueError("CLEAN_SCRATCH_ROOTS requires absolute paths")
        sweep_root(Path(value), "scratch", scratch_days)
