"""Read-only discovery for project activity, Lake builds, and shared Cargo targets."""

import os
import pathlib
import subprocess
import sys
import time

Path = pathlib.Path
BUILD_DIRS = {".git", ".venv", ".dev-venv", "venv", ".gradle", ".lake", "target",
              "node_modules", ".next", "__pycache__", ".mypy_cache", ".pytest_cache",
              ".ruff_cache"}
TOOL_DIRS = {".loom-local", ".loom", ".claude/worktrees"}


def raise_error(error):
    raise error


def activity(project, days):
    days = int(days)
    if days < 1:
        raise ValueError("the project activity threshold must be positive")
    cutoff = time.time() - days * 86400
    if not os.path.lexists(project / ".git"):
        # Outside git even a recent empty directory can represent ongoing work.
        for root, dirs, files in os.walk(project, onerror=raise_error):
            dirs[:] = [d for d in dirs if d not in BUILD_DIRS]
            for path in [Path(root)] + [Path(root, f) for f in files]:
                if path.lstat().st_mtime >= cutoff:
                    return "path " + str(path.relative_to(project))
        return ""

    def git(*args):
        return subprocess.check_output(["git", "-C", project, *args])

    # log is indifferent to a background fetch or a refreshed index. For an
    # unborn HEAD we keep the tree until its first commit gives us a baseline.
    commit = git("log", "-1", "--format=%ct %h").decode().split()
    if int(commit[0]) >= cutoff:
        return "commit " + commit[1]

    # Read tracked plus nonignored, untracked paths in one call. Exclusions
    # keep git out of generated directories even in projects without ignores.
    excludes = ["--exclude=" + d + "/" for d in sorted(BUILD_DIRS | TOOL_DIRS)]
    paths = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", *excludes)
    for raw in paths.split(b"\0"):
        if not raw:
            continue
        rel = Path(os.fsdecode(raw))
        if set(rel.parts[:-1]) & (BUILD_DIRS | {".loom-local"}):
            continue
        path = project / rel
        try:
            mtime = path.lstat().st_mtime
        except FileNotFoundError:
            # Git still knows a deleted file. Its directory records the unlink.
            prev = path.parent
            while not prev.exists() and prev != project:
                prev = prev.parent
            mtime = prev.stat().st_mtime
        if mtime >= cutoff:
            return "file " + str(rel)
    return ""


def emit_paths(*paths):
    for path in paths:
        sys.stdout.buffer.write(os.fsencode(path) + b"\0")


def lake_builds(project, explicit):
    for root, dirs, files in os.walk(project, onerror=raise_error):
        root = Path(root)
        if explicit and root != project and (".git" in dirs or ".git" in files):
            dirs.clear()
            continue
        if ".lake" in dirs:
            lake = root / ".lake"
            build = lake / "build"
            if not lake.is_symlink() and not build.is_symlink() and build.is_dir():
                emit_paths(build)
        dirs[:] = [d for d in dirs if d not in BUILD_DIRS]


def shared_target():
    # Follow Cargo's precedence and config-relative path rules.
    value = os.environ.get("CARGO_TARGET_DIR") or os.environ.get("CARGO_BUILD_TARGET_DIR")
    if value:
        return Path(value).absolute()
    cargo_home = Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo").absolute()
    for config in (cargo_home / "config", cargo_home / "config.toml"):
        if not config.is_file():
            continue
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        with config.open("rb") as f:
            value = tomllib.load(f).get("build", {}).get("target-dir")
        if value:
            return cargo_home.parent / value
        break
    return ""


def tagged_target(path):
    tag = path / "CACHEDIR.TAG"
    if tag.is_file() and not tag.is_symlink():
        with tag.open("rb") as f:
            header = f.read(256)
        return (header.startswith(b"Signature: 8a477f597d28d172789f06886806bc55")
                and b"created by cargo" in header)
    return False


def cargo_profiles(target):
    # A shared target may group several target roots by task, or put profiles
    # below a target triple. Only traverse the layout above profiles. A Cargo
    # cache tag and normal profile markers distinguish them from user folders.
    def visit(directory, depth, tagged):
        if (directory / ".git").exists() or (directory / "Cargo.toml").exists():
            return
        tagged = tagged or tagged_target(directory)
        if tagged and all((directory / d).is_dir() and not (directory / d).is_symlink()
                          for d in (".fingerprint", "deps")):
            if directory != target:
                emit_paths(directory)
            return
        if depth < 3:
            for child in sorted(directory.iterdir()):
                if not child.name.startswith(".") and not child.is_symlink() and child.is_dir():
                    visit(child, depth + 1, tagged)
    visit(target, 0, False)


def profile_keep_reason(profile, days):
    """A stale profile must contain only idle paths on its own filesystem."""
    if profile.is_symlink() or not profile.is_dir() or os.path.ismount(profile):
        return "profile moved or is a mount point"
    cutoff = time.time() - int(days) * 86400
    fs = profile.stat().st_dev
    for root, dirs, files in os.walk(profile, onerror=raise_error):
        if ".git" in dirs or ".git" in files:
            return "contains a git checkout"
        for path in [Path(root)] + [Path(root, f) for f in files + dirs]:
            st = path.lstat()
            if st.st_dev != fs:
                return "contains a mount point: " + str(path)
            if st.st_mtime >= cutoff:
                return "recent activity: " + str(path)
    return ""


if __name__ == "__main__":
    mode = sys.argv[1]
    try:
        if mode == "activity":
            print(activity(Path(sys.argv[2]), sys.argv[3]))
        elif mode == "lake":
            lake_builds(Path(sys.argv[2]), sys.argv[3] == "true")
        elif mode == "target":
            path = str(shared_target())
            if "\n" in path:
                raise ValueError("the target path contains a newline")
            print(path)
        elif mode == "profile-activity":
            print(profile_keep_reason(Path(sys.argv[2]), sys.argv[3]))
        elif mode == "profiles":
            cargo_profiles(Path(sys.argv[2]))
        else:
            sys.exit(2)
    except (OSError, ValueError, subprocess.CalledProcessError, ImportError, TypeError) as error:
        sys.exit(f"build artifact discovery: {error}")
