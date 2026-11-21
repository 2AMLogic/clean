"""Public CLI, isolated homes and projects, with no live system cleanup."""
import json
import os
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

    def test_step_failure(self):
        self.stub("rm", 'case "$*" in *".pyc") exit 1;; esac\nexec /bin/rm "$@"')
        result = self.cli("--projects-only", "--project", str(self.project), "--json")
        self.assertNotEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertEqual(data["items"][0]["status"], "failed")
        self.assertEqual(data["bytes_freed"], 0)

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
