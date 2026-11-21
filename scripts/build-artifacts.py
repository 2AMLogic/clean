"""Read-only discovery for project activity, Lake builds, and shared Cargo targets."""

import os
from pathlib import Path
import subprocess
import sys
import time

BUILD_DIRS = {".git", ".venv", ".dev-venv", "venv", ".gradle", ".lake", "target",
              "node_modules", ".next", "__pycache__", ".mypy_cache", ".pytest_cache",
              ".ruff_cache"}
TOOL_DIRS = {".loom-local", ".loom", ".claude/worktrees"}


def raise_error(error):
    raise error


def git(project, *args):
    # A caller's index or worktree override must not redirect a safety check.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.check_output(["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
                                    "-C", project, *args], env=env)


def owning_checkout(path):
    for parent in (path, *path.parents):
        if os.path.lexists(parent / ".git"):
            return parent
    return None


def identity(path):
    st = path.lstat()
    return f"{st.st_dev}:{st.st_ino}"


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

    # log is indifferent to a background fetch or a refreshed index. For an
    # unborn HEAD we keep the tree until its first commit gives us a baseline.
    commit = git(project, "log", "-1", "--format=%ct %h").decode().split()
    if int(commit[0]) >= cutoff:
        return "commit " + commit[1]

    # Read tracked plus nonignored, untracked paths in one call. Exclusions
    # keep git out of generated directories even in projects without ignores.
    excludes = ["--exclude=" + d + "/" for d in sorted(BUILD_DIRS | TOOL_DIRS)]
    paths = git(project, "ls-files", "-z", "--cached", "--others", "--exclude-standard", *excludes)
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
                emit_paths(build, identity(build), owning_checkout(root) or project)
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
            try:
                import tomli as tomllib
            except ImportError:
                # The Python 3.9 shipped with macOS developer tools includes this copy.
                from pip._vendor import tomli as tomllib
        value = tomllib.loads(config.read_text(encoding="utf-8")).get("build", {}).get("target-dir")
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
                emit_paths(directory, identity(directory))
            return
        if depth < 3:
            for child in sorted(directory.iterdir()):
                if not child.name.startswith(".") and not child.is_symlink() and child.is_dir():
                    visit(child, depth + 1, tagged)
    visit(target, 0, False)


def build_keep_reason(path, expected, kind, days=None):
    """Revalidate a candidate after sizing, immediately before removal."""
    if path.resolve() != path or identity(path) != expected or not path.is_dir():
        return "build path changed"
    if path.is_mount():
        return "build is a mount point"
    owner = owning_checkout(path.parent)
    if owner:
        if git(owner, "--literal-pathspecs", "ls-files", "-z", "--", str(path)):
            return "contains tracked files"
        if git(owner, "--literal-pathspecs", "ls-files", "-z", "--others",
               "--exclude-standard", "--", str(path)):
            return "contains untracked non-ignored work"
        if kind == "cargo" and not tagged_target(path):
            return "no Cargo cache tag"
        if kind == "venv" and not (path / "pyvenv.cfg").is_file():
            return "no Python virtual environment marker"
    cutoff = None if days is None else time.time() - int(days) * 86400
    fs = path.stat().st_dev
    for root, dirs, files in os.walk(path, onerror=raise_error):
        if ".git" in dirs or ".git" in files:
            return "contains git metadata"
        # A bare repository also owns history even though it has no .git entry.
        if "HEAD" in files and "objects" in dirs and "refs" in dirs:
            return "contains a bare repository"
        entries = [Path(root)] + [Path(root, d) for d in dirs]
        if cutoff is not None:
            entries.extend(Path(root, f) for f in files)
        for entry in entries:
            st = entry.lstat()
            if st.st_dev != fs:
                return "contains a mount point: " + str(entry)
            if cutoff is not None and st.st_mtime >= cutoff:
                return "recent activity: " + str(entry)
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
        elif mode == "verify":
            print(build_keep_reason(Path(sys.argv[2]), sys.argv[3], sys.argv[4], *sys.argv[5:]))
        elif mode == "profiles":
            cargo_profiles(Path(sys.argv[2]))
        else:
            sys.exit(2)
    except (OSError, ValueError, subprocess.CalledProcessError, ImportError, TypeError) as error:
        sys.exit(f"build artifact discovery: {error}")
