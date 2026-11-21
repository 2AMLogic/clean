#!/usr/bin/env python3
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import schedule
import system_report as report


def output(call, *args):
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        call(*args)
    return stream.getvalue()


class Reports(unittest.TestCase):
    def test_memory_snapshot(self):
        lines = [f"{n} 01-03:00:00 1024 /private/tmp/leak" for n in range(51)]
        lines += ["52 06:00:00 2048 /tmp/new", "53 00:20 8192 /some path/big"]
        data = {"sysctl": "1", "ps": "\n".join(lines)}
        with patch.object(report, "query", side_effect=lambda cmd: data[cmd[0]]):
            text = output(report.memory_report)
        self.assertIn("51 × /private/tmp/leak  51.0 MiB  01-03:00:00", text)
        old = text.split("over 6h")[1].split("Top five")[0]
        self.assertIn("/private/tmp/leak", old)
        self.assertNotIn("/tmp/new", old)
        self.assertEqual(len(text.split("Top five")[1].splitlines()), 6)

    def test_docker_images_and_sparse_accounting(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            current = home / "disk volume/vms/0/data/Docker.raw"
            old = home / "Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw"
            archived = home / "old nonindexed/data/Docker.raw"
            settings = home / "Library/Group Containers/group.com.docker/settings-store.json"
            for p in [current, old, archived, settings]:
                p.parent.mkdir(parents=True)
                p.touch()
            with current.open("wb") as handle:
                handle.truncate(report.GIB * 2)
            settings.write_text(json.dumps({"DataFolder": str(home / "disk volume")}))
            with patch.object(report, "query", return_value=str(old) + "\0"), patch.dict(
                    os.environ, CLEAN_DOCKER_SEARCH_ROOTS=str(archived.parents[1])):
                text = output(report.docker_report, home, 1000000 * report.GIB)
            self.assertIn("LOW DISK", text)
            self.assertIn(f"Current: {current.resolve()}  {report.human(current.stat().st_blocks * 512)} allocated", text)
            for p in [old, archived]:
                self.assertEqual(text.count(f"Outside current data folder: {p.resolve()}"), 1)
                self.assertTrue(p.exists())
            self.assertIn("Data volume free", text)

    def test_toolchain_inventory_cannot_write_user_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = Path(tmp) / ".elan"
            (manager / "toolchains/a").mkdir(parents=True)

            def fake_manager(cmd, env):
                mirror = Path(env["ELAN_HOME"])
                self.assertTrue((mirror / "toolchains/a").is_dir())
                (mirror / "settings.toml").write_text("created on inventory")
                return "{}"

            with patch.object(report.shutil, "which", return_value="/fake/elan"), \
                    patch.object(report, "query", side_effect=fake_manager):
                self.assertEqual(report.query_toolchains("elan", manager, ["toolchain", "gc", "--json"]), "{}")
            self.assertFalse((manager / "settings.toml").exists())

    def test_footprint_and_runtime_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            file = home / ".codex/sessions/old.log"
            recent = file.with_name("recent")
            file.parent.mkdir(parents=True)
            file.write_text("old")
            os.utime(file, (time.time() - 91 * report.DAY,) * 2)
            recent.write_text("recent")
            (file.parent / "outside").symlink_to(home)
            for version in ["1.2", "1.20"]:
                (home / f".loom/opt/opencode-{version}/xdg").mkdir(parents=True)
            config = home / "projects/a/.loom-local/local.json"
            config.parent.mkdir(parents=True)
            config.write_text('{"binary": "/tools/opencode-1.20/bin/opencode"}')
            name = home / ".elan/toolchains/leanprover--lean4---v4.5.0"
            name.mkdir(parents=True)
            (name / "bin").write_bytes(b"a" * 4096)
            gc = json.dumps({"unused_toolchains": ["leanprover/lean4:v4.5.0"]})
            with patch.object(report, "query_toolchains", return_value=gc), patch.dict(
                    os.environ, {"ELAN_HOME": str(home / ".elan"), "RUSTUP_HOME": str(home / ".rustup")}):
                text = output(report.tool_homes, home, [config.parents[1]])
            self.assertIn(f"1/1  {file.parent}", text)
            self.assertIn("opencode-1.2/xdg (no reference", text)
            self.assertNotIn("opencode-1.20/xdg (no reference", text)
            self.assertIn("estimated reclaimable", text)
            self.assertEqual(file.read_text(), "old")


class Schedule(unittest.TestCase):
    def test_lifecycle_with_fake_launchctl(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            mockdir = home / "tools"
            mockdir.mkdir()
            mock = mockdir / "launchctl"
            mock.write_text("""#!/bin/sh
case "$1" in
  print) test -f "$HOME/loaded" ;;
  bootstrap) test -z "$FAIL_BOOTSTRAP" || exit 1
             test -f "$3" && touch "$HOME/loaded" ;;
  bootout) rm "$HOME/loaded" ;;
  *) exit 3 ;;
esac
""")
            mock.chmod(0o700)
            runner = home / "clean script"
            runner.write_text('#!/bin/sh\n[ "$#" = 0 ] || exit 30\nexit 4\n')
            runner.chmod(0o700)
            with patch.dict(os.environ, HOME=tmp, PATH=str(mockdir) + ":/usr/bin:/bin",
                            CLEAN_LOW_DISK_GIB="999999"), patch.object(schedule, "SCRIPT", runner), \
                    patch.object(schedule.sys, "platform", "darwin"):
                output(schedule.main, ["install", "--interval-hours", "6"])
                plist = home / f"Library/LaunchAgents/{schedule.LABEL}.plist"
                agent = plistlib.loads(plist.read_bytes())
                self.assertEqual(agent["ProgramArguments"], [str(runner), "schedule", "run"])
                self.assertEqual(agent["StartInterval"], 21600)
                self.assertEqual(agent["StandardErrorPath"], str(home / "Library/Logs/clean.log"))
                self.assertIn("already loaded", output(schedule.main, ["install", "--interval-hours", "6"]))
                self.assertIn("none recorded", output(schedule.main, ["status"]))
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream):
                    self.assertEqual(schedule.main(["run"]), 4)
                self.assertIn("LOW DISK", stream.getvalue())
                self.assertIn("exit 4", output(schedule.main, ["status"]))
                output(schedule.main, ["uninstall"])
                self.assertFalse(plist.exists())
                self.assertFalse((home / "loaded").exists())
                with patch.dict(os.environ, FAIL_BOOTSTRAP="1"):
                    with self.assertRaises(subprocess.CalledProcessError):
                        schedule.main(["install"])
                    self.assertFalse(plist.exists())


if __name__ == "__main__":
    unittest.main()
