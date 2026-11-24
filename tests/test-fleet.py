"""Public CLI, isolated homes and projects, with no live system cleanup."""
import fcntl
import json
import os
import pty
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "cleanup.sh"


class Fleet(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.project = self.root / "work spaces" / "first"
        self.other = self.project.parent / "sibling"
        self.nested = self.project / "nested"
        for p in (self.project, self.other, self.nested):
            p.mkdir(parents=True)
            f = p / "old file.pyc"
            f.write_bytes(b"stale" * 4000)
            os.utime(f, (1, 1))
        (self.nested / ".git").mkdir()
        self.env = dict(HOME=str(self.root), TMPDIR=str(self.root),
                        CLEAN_PROJECT_DIR=str(self.project.parent),
                        PATH=f"{self.bin}:/usr/bin:/bin", LC_ALL="C")
        self.link = self.bin / "clean"
        self.link.symlink_to(SCRIPT)
        self.logs = self.root / ".clean/logs"

    def cli(self, *args, **env):
        return subprocess.run([self.link, *args], env=dict(self.env, **env),
                              capture_output=True, text=True)

    def stub(self, cmd, body):
        script = self.bin / cmd
        script.write_text("#!/bin/sh\n" + body + "\n")
        script.chmod(0o755)

    def test_scoped_json_check_then_clean(self):
        args = ["--projects-only", "--json", "--project", str(self.project)]
        preview = self.cli(*args, "--check")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        data = json.loads(preview.stdout)
        self.assertEqual(data["bytes_freed"], 0)
        self.assertGreater(data["bytes_reclaimable"], 0)
        self.assertEqual(len(data["items"]), 1)
        self.assertTrue((self.project / "old file.pyc").exists())
        run = self.cli(*args)
        self.assertEqual(run.returncode, 0, run.stderr)
        actual = json.loads(run.stdout)
        self.assertEqual(actual["bytes_freed"], data["bytes_reclaimable"])
        self.assertEqual(actual["items"][0]["status"], "removed")
        self.assertFalse((self.project / "old file.pyc").exists())
        for p in (self.other, self.nested):
            self.assertTrue((p / "old file.pyc").exists())

    def test_environment_selection_and_invalid_inputs(self):
        run = self.cli("--projects-only", "--json", "--check",
                       CLEAN_PROJECTS=f"{self.other}:{self.project}")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(len(json.loads(run.stdout)["items"]), 2)
        for args, env in [
            (["--unknown"], {}), (["--project"], {}),
            (["--projects-only", "--worktrees-only"], {}),
            (["--projects-only"], {"CLEAN_PROJECTS": ""}),
            (["--projects-only"], {"CLEAN_TARGET_AGE_DAYS": "-1"}),
        ]:
            self.assertNotEqual(self.cli(*args, **env).returncode, 0)
        self.stub("uname", "echo Linux")
        refused = self.cli("--projects-only")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("requires macOS", refused.stderr)
        self.assertTrue((self.project / "old file.pyc").exists())

    def test_artifact_refreshed_after_discovery(self):
        item = self.project / "old file.pyc"
        self.stub("python3", 'case "$2" in */project-candidates.py)\n'
                  '    /usr/bin/python3 "$@" || exit 1\n'
                  '    exec touch "$REFRESH_ITEM";;\nesac\nexec /usr/bin/python3 "$@"')
        result = self.cli("--projects-only", "--project", self.project, "--json", REFRESH_ITEM=str(item))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(item.exists())
        event, = json.loads(result.stdout)["items"]
        self.assertEqual((event["status"], event["reason"]), ("kept", "recent stray .pyc"))

    def test_open_handles_checked_only_for_gradle_candidates(self):
        for project in (self.project, self.other):
            (project / "build").mkdir()
        (self.other / "build.gradle.kts").touch()
        self.stub("lsof", 'for arg do last=$arg; done\necho "$last" >> "$HOME/lsof-calls"\nexit 0')
        result = self.cli("--projects-only", "--check", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "lsof-calls").read_text().splitlines(), [str(self.other)])
        items = json.loads(result.stdout)["items"]
        self.assertTrue(any(event["path"] == str(self.other) and event["reason"] == "open handles"
                            for event in items))

    def test_native_cache_previews(self):
        cache = self.root / "brew-cache"
        cache.mkdir()
        self.stub("brew", '[ "$1" = --cache ] || exit 1\necho "$HOME/brew-cache"')
        self.stub("du", 'for arg do [ "$arg" != "$HOME/brew-cache" ] || exit 1; done\nexec /usr/bin/du "$@"')
        self.stub("pnpm", 'touch "$HOME/pnpm-was-invoked"')
        # The scratch scanner has its own fixture suite. Keep this system
        # preview from traversing the host's /private/tmp clones or sessions.
        self.stub("python3", 'case "$*" in *system_sweep.py*|*system_report.py*) exit 0;; esac\n'
                  'exec /usr/bin/python3 "$@"')
        self.stub("git", 'case "$*" in *"/private/tmp/"*) exit 1;; esac\nexec /usr/bin/git "$@"')
        preview = self.cli("--system-only", "--check", "--json")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        items = json.loads(preview.stdout)["items"]
        self.assertTrue(any(item["status"] == "would_remove" and item["path"] == str(cache)
                            for item in items))
        self.assertTrue(any(item["status"] == "would_remove" and "pnpm store prune" in item["reason"]
                            for item in items))
        self.assertFalse((self.root / "pnpm-was-invoked").exists())

    def test_terminal_banner_and_color(self):
        self.stub("gh", "exit 1")
        version = self.cli("--version").stdout.strip()
        command = [self.link, "--worktrees-only", "--project", self.project]
        plain = self.cli(*command[1:], TERM="xterm-256color")
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(plain.stdout.splitlines()[0], f"{version}  ·  Disk cleanup")
        self.assertNotIn("\x1b", plain.stdout)

        for env, color in [({"TERM": "xterm-256color"}, True),
                           ({"TERM": "xterm-256color", "NO_COLOR": "1"}, False),
                           ({"TERM": "dumb"}, False)]:
            with self.subTest(env=env):
                master, slave = pty.openpty()
                with os.fdopen(master, "rb") as screen, os.fdopen(slave, "wb") as tty:
                    # JSON goes to stdout. Send progress to a real terminal.
                    result = subprocess.run([*command, "--json", "--check", "--aggressive"],
                                            env=dict(self.env, **env), stdout=subprocess.PIPE,
                                            stderr=tty, check=True)
                    self.assertTrue(json.loads(result.stdout)["check"])
                    line = screen.readline().decode().strip()
                    self.assertIn(version, line)
                    self.assertIn("Dry run (aggressive)", line)
                    self.assertEqual("\x1b[" in line, color)

    def test_step_failure(self):
        self.stub("rm", 'case "$*" in *".pyc") echo "fixture rm failure" >&2; exit 1;; esac\n'
                  'exec /bin/rm "$@"')
        result = self.cli("--projects-only", "--project", str(self.project), "--json")
        self.assertNotEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertEqual(data["items"][0]["status"], "failed")
        self.assertEqual(data["bytes_freed"], 0)
        log, = self.logs.glob("*.log")
        self.assertIn("fixture rm failure", result.stderr)
        transcript = log.read_text()
        self.assertIn(result.stdout, transcript)
        self.assertEqual(transcript.replace(result.stdout, "", 1), result.stderr)

    def test_recent_run_logs(self):
        self.cli("--help")
        self.cli("--version")
        self.assertFalse(self.logs.exists())
        self.stub("gh", "exit 1")
        args = ("--worktrees-only", "--project", str(self.project), "--check")
        first = self.cli(*args)
        self.assertEqual(first.returncode, 0, first.stderr)
        oldest, = self.logs.glob("*.log")
        self.assertEqual(oldest.read_text(), first.stdout)
        self.assertEqual(oldest.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.logs.stat().st_mode & 0o777, 0o700)
        note = self.logs / "notes.log"
        note.write_text("keep")
        # An older run is still writing while several short runs finish.
        with oldest.open("ab") as active:
            fcntl.flock(active, fcntl.LOCK_EX)
            for _ in range(5):
                self.assertEqual(self.cli(*args).returncode, 0)
            self.assertEqual(len(list(self.logs.glob("run-*.log"))), 6)
            self.assertTrue(oldest.exists())
        self.assertEqual(self.cli(*args).returncode, 0)
        self.assertEqual(len(list(self.logs.glob("run-*.log"))), 5)
        self.assertFalse(oldest.exists())
        self.assertEqual(note.read_text(), "keep")

    def test_scheduled_run_has_one_complete_log(self):
        # Stop the scheduled cleanup at preflight, before it touches the host.
        self.stub("uname", "echo Linux")
        run = self.cli("schedule", "run")
        self.assertEqual(run.returncode, 2)
        log, = self.logs.glob("*.log")
        lines = log.read_text().splitlines()
        self.assertIn("Scheduled clean started", lines[0])
        self.assertIn("requires macOS", lines[1])
        self.assertIn("Scheduled clean finished", lines[2])
        self.assertIn("exit 2", lines[2])

    def test_cleanup_when_log_directory_is_unavailable(self):
        (self.root / ".clean").touch()
        self.stub("gh", "exit 1")
        result = self.cli("--worktrees-only", "--check", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not create run log", result.stderr)
        self.assertTrue(json.loads(result.stdout)["check"])

    def test_full_clone_other_refs_and_external_worktree_root(self):
        def git(repo, *args):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture",
                                   "-c", "user.email=f@example.test", *args],
                                  check=True, capture_output=True)
        self.stub("gh", 'case "$1" in issue) echo CLOSED;; esac')
        self.stub("lsof", "exit 1")
        git(self.project, "init", "-b", "main")
        managed = self.project / ".loom/worktrees"
        clone = managed / "issue-123"
        clone.mkdir(parents=True)
        git(clone, "init", "-b", "main")
        git(clone, "commit", "--allow-empty", "-m", "base")
        git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(clone, "switch", "-c", "private")
        git(clone, "commit", "--allow-empty", "-m", "unpublished")
        git(clone, "switch", "main")
        (self.project / ".claude").symlink_to(self.project / ".loom")
        # Simulate running in a Git hook for a different repository.
        result = self.cli("--worktrees-only", "--project", str(self.project), "--json",
                          GIT_DIR=str(self.project / ".git"), GIT_WORK_TREE=str(self.project))
        self.assertEqual(result.returncode, 0, result.stderr)
        reasons = {item["reason"] for item in json.loads(result.stdout)["items"]}
        self.assertIn("uncommitted/unpushed", reasons)
        self.assertIn("symlinked worktree root", reasons)
        git(clone, "rev-parse", "--verify", "private")

    def test_offline_worktree_phase(self):
        self.stub("gh", "exit 1")
        result = self.cli("--worktrees-only", "--json", "--project", str(self.project))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["items"][0]["status"], "skipped")
        self.assertIn("GitHub", data["items"][0]["reason"])
        self.assertTrue((self.project / "old file.pyc").exists())


if __name__ == "__main__":
    unittest.main()
