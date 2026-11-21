"""Walk one workspace without descending into nested repos or artifact trees."""
import os
import sys

CACHES = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
VENVS = {".venv", "venv", ".dev-venv", "env"}
PRUNE = {".git", ".loom", ".claude", ".loom-local", ".lake", "target", "node_modules",
         ".next", ".gradle", "site-packages", "vendor"}


def emit(kind, path):
    st = os.lstat(path)
    identity = f"{st.st_dev}:{st.st_ino}".encode()
    sys.stdout.buffer.write(b"\0".join((kind.encode(), os.fsencode(path), identity)) + b"\0")


def walk(project):
    def raise_error(error):
        raise error

    for root, dirs, files in os.walk(project, onerror=raise_error):
        if root != project and (".git" in dirs or ".git" in files):
            dirs.clear()
            continue
        for name in dirs[:]:
            path = os.path.join(root, name)
            if os.path.islink(path) or name in PRUNE:
                dirs.remove(name)
            elif name in CACHES:
                emit("cache", path)
                dirs.remove(name)
            elif name in VENVS and (name != "env" or os.path.isfile(os.path.join(path, "bin/python"))):
                emit("venv", path)
                dirs.remove(name)
        for name in files:
            path = os.path.join(root, name)
            if os.path.islink(path):
                continue
            if name.endswith(".pyc"):
                emit("bytecode", path)
            elif name == "Cargo.toml":
                target = os.path.join(root, "target")
                if os.path.isdir(target) and not os.path.islink(target):
                    emit("cargo", target)


if __name__ == "__main__":
    walk(sys.argv[1])
