"""Record both output streams from one cleanup invocation."""
import datetime
import errno
import fcntl
import os
from contextlib import ExitStack
from pathlib import Path
import pty
import re
import selectors
import signal
import subprocess
import sys
import termios


KEEP = 5
LOG_NAME = re.compile(r"run-\d{8}T\d{6}\.\d{6}Z-\d+\.log")


def start_log(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = directory / f"run-{stamp}-{os.getpid()}.log"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    # On macOS O_EXLOCK acquires the writer's lock at file creation.
    fd = os.open(path, flags | getattr(os, "O_EXLOCK", 0), 0o600)
    log = os.fdopen(fd, "wb", buffering=0)
    fcntl.flock(log, fcntl.LOCK_EX)
    return log


def prune_logs(directory):
    completed = 0
    for path in sorted(directory.iterdir(), reverse=True):
        if not LOG_NAME.fullmatch(path.name) or path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open("rb") as candidate:
                fcntl.flock(candidate, fcntl.LOCK_SH | fcntl.LOCK_NB)
                completed += 1
                if completed > KEEP:
                    path.unlink()
        except (BlockingIOError, FileNotFoundError):
            continue


def output_pipe(destination, readers, writers, selector):
    if os.isatty(destination):
        # Preserve color and terminal width. The real terminal translates
        # newlines when we replay them, so the capture terminal must not.
        reader, writer = pty.openpty()
        attrs = termios.tcgetattr(destination)
        attrs[1] &= ~termios.OPOST
        termios.tcsetattr(writer, termios.TCSANOW, attrs)
        size = fcntl.ioctl(destination, termios.TIOCGWINSZ, bytes(8))
        fcntl.ioctl(writer, termios.TIOCSWINSZ, size)
    else:
        reader, writer = os.pipe()
    reader = readers.enter_context(os.fdopen(reader, "rb", buffering=0))
    writers.callback(os.close, writer)
    selector.register(reader, selectors.EVENT_READ, destination)
    return writer


def write_all(fd, data):
    remaining = memoryview(data)
    while remaining:
        remaining = remaining[os.write(fd, remaining):]


def record(command, env, log):
    with ExitStack() as readers, ExitStack() as writers, selectors.DefaultSelector() as streams:
        stdout = output_pipe(1, readers, writers, streams)
        if os.path.samestat(os.fstat(1), os.fstat(2)):
            stderr = stdout
        else:
            stderr = output_pipe(2, readers, writers, streams)
        child = subprocess.Popen(command, stdout=stdout, stderr=stderr, env=env)
        writers.close()
        for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
            signal.signal(sig, lambda signum, frame: child.send_signal(signum))
        while streams.get_map():
            for key, _ in streams.select():
                try:
                    data = os.read(key.fd, 65536)
                except OSError as err:
                    if err.errno != errno.EIO or not os.isatty(key.data):
                        raise
                    data = b""  # PTY slave closed.
                if not data:
                    streams.unregister(key.fd)
                    continue
                if log is not None:
                    try:
                        write_all(log.fileno(), data)
                    except OSError as err:
                        print(f"clean: could not continue run log: {err}", file=sys.stderr)
                        log = None
                try:
                    write_all(key.data, data)
                except BrokenPipeError:
                    streams.unregister(key.fileobj)
                    key.fileobj.close()
        status = child.wait()
        return status if status >= 0 else 128 - status


def main(command):
    directory = Path.home() / ".clean" / "logs"
    env = dict(os.environ, _CLEAN_RECORDING="1")
    try:
        log = start_log(directory)
    except OSError as err:
        print(f"clean: could not create run log: {err}", file=sys.stderr, flush=True)
        os.execve(command[0], command, env)
    try:
        with log:
            return record(command, env, log)
    finally:
        try:
            prune_logs(directory)
        except OSError as err:
            print(f"clean: could not prune run logs: {err}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main(["/bin/bash", *sys.argv[1:]]))
