#!/usr/bin/env python3
"""Run the system scratch and package cache passes on disposable fixtures."""
import contextlib
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import scratch
import system_sweep


class SweepTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name).resolve()
        self.root = self.temp / "scratch volume"
        self.root.mkdir()
        self.processes = mock.Mock()
        self.processes.busy.return_value = ""
        self.events = self.temp / "events"
        env = mock.patch.dict(os.environ, {"CLEAN_EVENT_FILE": str(self.events),
                                           "HOME": str(self.temp / "home")})
        env.start()
        self.addCleanup(env.stop)
        self.capture = io.StringIO()
        output = contextlib.redirect_stdout(self.capture)
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def age(self, path):
        for root, dirs, files in os.walk(path, topdown=False):
            for name in dirs + files:
                os.utime(Path(root) / name, (1, 1), follow_symlinks=False)
        os.utime(path, (1, 1))

    def folder(self, name):
        path = self.root / name
        path.mkdir()
        (path / "data").write_text("fixture")
        self.age(path)
        return path

    def sweep(self, kind="scratch", preview=False):
        scratch.sweep(self.root, kind, 7, preview, self.processes)

    def test_scratch_preview_groups_and_clean_preserves_boundaries(self):
        old = self.folder("loom-ui.aB3dE6")
        old2 = self.folder("loom-ui.abc123")
        durable = self.folder("archive.backup")
        recent = self.folder("scratch.AbC123")
        open_path = self.folder("busy.AbC123")
        socket_path = self.folder("socket.AbC123")
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(socket_path / "s"))
        self.age(socket_path)
        live_link = old / "symlink"
        live_link.symlink_to(durable, target_is_directory=True)
        self.age(old)
        (recent / "data").touch()
        self.processes.busy.side_effect = lambda path: "open handles" if path == open_path else ""
        self.sweep(preview=True)
        self.assertTrue(old.exists())
        self.assertTrue(old2.exists())
        self.assertIn("2 × loom-ui.*", self.capture.getvalue())
        self.sweep()
        self.assertFalse(old.exists())
        self.assertFalse(old2.exists())
        for path in (durable, recent, open_path, socket_path):
            self.assertTrue((path / "data").exists())
        self.assertIn(b"open handles", self.events.read_bytes())
        self.assertIn(b"recent activity", self.events.read_bytes())

    def test_nested_unpushed_repo_and_stash_are_kept(self):
        stale = self.folder("repo.aB3dE6")
        nested = stale / "subproject"
        def git(*args):
            subprocess.run(["git", "-C", str(nested), *args], check=True, capture_output=True)
        nested.mkdir()
        git("init", "-q")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Fixture")
        git("commit", "-q", "--allow-empty", "-m", "initial")
        self.age(stale)
        self.sweep()
        self.assertTrue(nested.exists())
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        (nested / "untracked").write_text("keep")
        self.age(stale)
        self.sweep()
        self.assertTrue(nested.exists())
        git("stash", "push", "-u", "-m", "local stash")
        self.age(stale)
        self.sweep()
        self.assertTrue(nested.exists())
        git("stash", "clear")
        self.age(stale)
        self.sweep()
        self.assertFalse(stale.exists())

    def test_git_environment_cannot_redirect_wip_inspection(self):
        other = self.temp / "pushed"
        dirty = self.folder("repo.aB3dE6")
        for repo in (other, dirty):
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=test@example.com",
                            "commit", "-qm", "baseline", "--allow-empty"], check=True)
            subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
        self.age(dirty)
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
                                          "GIT_INDEX_FILE": str(other / ".git/index")}):
            self.sweep()
        self.assertTrue((dirty / "data").exists())

    def test_launchd_temp_fallback(self):
        process = subprocess.CompletedProcess([], 0, os.fsencode(self.root), b"")
        with mock.patch.dict(os.environ, {"TMPDIR": "", "CLEAN_SCRATCH_ROOTS": ""}), \
             mock.patch("scratch.subprocess.run", return_value=process), mock.patch("scratch.sweep") as sweep:
            scratch.scratch_areas(True, self.processes, 7, 3, 7)
            sweep.assert_any_call(self.root, "tmp", 7, True, self.processes)

    def test_empty_cache_preview_never_invokes_npm_or_go(self):
        with mock.patch.dict(os.environ, {"GOCACHE": "", "NPM_CONFIG_CACHE": "", "npm_config_cache": ""}), \
             mock.patch("system_sweep.shutil.which", return_value="/usr/local/bin/installed"), \
             mock.patch("system_sweep.subprocess.check_output", side_effect=AssertionError("unexpected command")), \
             mock.patch("system_sweep.subprocess.run", side_effect=AssertionError("unexpected command")):
            system_sweep.go_cache(True)
            system_sweep.npm_caches(True, self.processes, 7)
        self.assertFalse(Path.home().exists())

    def test_unknown_handles_or_recency_keep_candidate(self):
        path = self.folder("unknown.aB3dE6")
        self.processes.busy.side_effect = OSError("lsof unavailable")
        self.sweep()
        self.assertTrue(path.exists())
        with mock.patch("scratch.inspect", side_effect=PermissionError()):
            self.sweep()
        self.assertTrue(path.exists())

    def test_temp_sanity_and_session_names(self):
        old = self.folder("test-suite-0001")
        self.sweep("tmp")
        self.assertTrue(old.exists())
        self.folder("test-suite-0002").touch()
        self.sweep("tmp")
        self.assertFalse(old.exists())
        session = self.folder("9fd34ad6-8752-48c5-ab04-7f9820213b36")
        pid = self.folder("process.pid")
        self.sweep("session", True)
        self.assertIn(str(session), self.capture.getvalue())
        self.sweep("session")
        self.assertFalse(session.exists())
        self.assertTrue(pid.exists())

    def test_snapshots_include_file_and_argv(self):
        scan = scratch.ProcessSnapshot()
        names = b"p123\0\nn" + os.fsencode(self.root / "held" / "file") + b"\0\n"
        with mock.patch("scratch.subprocess.run", return_value=subprocess.CompletedProcess([], 0, names, b"")), \
             mock.patch("scratch.subprocess.check_output", return_value=os.fsencode(self.root / "in argv")):
            self.assertEqual(scan.busy(self.root / "held"), "open handles")
            self.assertIn("process", scan.busy(self.root / "in argv"))
            self.assertFalse(scan.busy(self.root / "idle"))

    def test_npm_and_go_commands_and_sizes(self):
        npx = self.root / "_npx"
        npx.mkdir()
        old = npx / "a1234bc56789def0"
        old.mkdir()
        self.age(old)
        self.folder("_cacache")
        active = npx / "b1234bc56789def0"
        active.mkdir()
        self.age(active)
        self.processes.busy.side_effect = lambda p: "argv" if p == active else ""
        bin_dir = self.temp / "bin"
        bin_dir.mkdir()
        # Stub both tools. They may act only on paths created in this test.
        npm = bin_dir / "npm"
        npm.write_text('#!/bin/sh\ncase "$1" in\nconfig) echo "$CACHE_FIXTURE";;\n'
                       'cache) rm -f "$CACHE_FIXTURE/_cacache/data";;\nesac\n')
        go = bin_dir / "go"
        go.write_text('#!/bin/sh\ncase "$1" in\nenv) echo "$CACHE_FIXTURE/go";;\n'
                      'clean) rm -f "$CACHE_FIXTURE/go/data";;\nesac\n')
        npm.chmod(0o755)
        go.chmod(0o755)
        go_data = self.folder("go") / "data"
        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:/bin:/usr/bin", "CACHE_FIXTURE": str(self.root),
                                        "npm_config_cache": str(self.root), "GOCACHE": str(go_data.parent)}):
            system_sweep.npm_caches(True, self.processes, 7)
            system_sweep.go_cache(True)
            self.assertTrue(go_data.exists())
            self.assertTrue(old.exists())
            system_sweep.npm_caches(False, self.processes, 7)
            system_sweep.go_cache(False)
        self.assertTrue(active.exists())
        self.assertFalse(old.exists())
        self.assertFalse(go_data.exists())
        self.assertFalse((self.root / "_cacache/data").exists())
        self.assertIn("after", self.capture.getvalue())

    def test_sccache_limit_stop_and_shard_layout(self):
        key = "abcdef0123456789" * 4
        shard = self.root / "a/b"
        shard.mkdir(parents=True)
        obj = shard / key
        obj.write_bytes(b"compiled")
        unrelated = shard / "myfile"
        unrelated.touch()
        with mock.patch.dict(os.environ, {"SCCACHE_DIR": str(self.root)}), \
             mock.patch("system_sweep.shutil.which", return_value="/fake/sccache"), \
             mock.patch("system_sweep.subprocess.run") as run:
            system_sweep.compiler_cache(False, False, 1)
            system_sweep.compiler_cache(True, True, 1)
            run.assert_not_called()
            self.assertTrue(obj.exists())
            run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
            system_sweep.compiler_cache(False, True, 1)
            run.assert_called_once()
            self.assertFalse(obj.exists())
            self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
