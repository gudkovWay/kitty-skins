"""Dependency-free lifecycle regressions; run with Python's native unittest."""
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import playback


class VideoPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.live = {}
        self.next_pid = 50000
        self.fail_source = None
        self.env = {"XDG_RUNTIME_DIR": str(self.root / "run"), "WAYLAND_DISPLAY": "test-0",
                    "NOCTALIA_STATE_HOME": str(self.root / "state")}
        runtime = Path(self.env["XDG_RUNTIME_DIR"])
        runtime.mkdir()
        display = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        display.bind(str(runtime / self.env["WAYLAND_DISPLAY"]))
        self.addCleanup(display.close)
        patches = [
            patch.dict(os.environ, self.env),
            patch.object(playback, "_proc_start_time", side_effect=lambda pid: self.live.get(pid, {}).get("start_time")),
            patch.object(playback, "_proc_cmdline", side_effect=lambda pid: self.live.get(pid, {}).get("argv")),
            patch.object(playback, "_proc_environ", side_effect=lambda pid: self.live.get(pid, {}).get("env")),
            patch.object(playback, "_renderers", side_effect=self.renderers),
            patch.object(playback, "_launch", side_effect=self.launch),
            patch.object(playback, "_terminate", side_effect=self.terminate),
            patch.object(playback, "_video_executable", return_value="/usr/bin/mpvpaper"),
            patch.object(playback, "_sync_palette", return_value=None),
        ]
        for mocked in patches:
            mocked.start()
            self.addCleanup(mocked.stop)

    def item(self, name, kind="video"):
        project = self.root / name
        project.mkdir(exist_ok=True)
        (project / "movie.mp4").write_bytes(b"unit fixture: decoder is outside this test")
        (project / "project.json").write_text(json.dumps({"type": kind, "file": "movie.mp4"}))
        return {"id": name, "path": str(project), "kind": kind, "source_id": name}

    def launch(self, record, output):
        if record["content_id"] == self.fail_source:
            raise playback.PlaybackError("decoder rejected source")
        self.next_pid += 1
        started = dict(record, pid=self.next_pid, pgid=self.next_pid,
                       start_time=str(self.next_pid), status="playing", error=None, bg_id=None)
        self.live[self.next_pid] = dict(started, argv=playback._expected_argv(started, output), env=dict(os.environ))
        return started

    def terminate(self, record, output):
        if record.get("pid") not in self.live:
            return True
        self.assertTrue(playback._verify(record, output), "never stop an unverified process")
        self.live.pop(record["pid"])
        return True

    def renderers(self):
        rows = []
        for pid, record in self.live.items():
            outputs = record.get("outputs") or [record["argv"][2]]
            for output in outputs:
                rows.append({"pid": pid, "output": output, "bg": record.get("media_path") or record["path"]})
        return rows

    def test_identical_videos_share_player_and_reapplying_does_not_restart(self):
        item = self.item("first")
        playback.apply(item, "DP-2")
        playback.apply(item, "DP-3")
        state = playback.read_state()
        self.assertEqual(state["current"]["DP-2"]["pid"], state["current"]["DP-3"]["pid"])
        self.assertEqual(set(state["current"]["DP-2"]["outputs"]), {"DP-2", "DP-3"})
        pid = state["current"]["DP-2"]["pid"]
        playback.apply(item, "DP-2")
        self.assertEqual(playback.read_state()["current"]["DP-2"]["pid"], pid)
        self.assertEqual(set(self.live), {pid})

    def test_stopping_one_shared_output_keeps_the_other_playing(self):
        item = self.item("first")
        playback.apply(item, "DP-2")
        playback.apply(item, "DP-3")
        playback.stop("DP-2")
        state = playback.read_state()
        self.assertNotIn("DP-2", state["current"])
        self.assertEqual(state["current"]["DP-3"]["outputs"], ["DP-3"])
        self.assertTrue(playback._verify(state["current"]["DP-3"], "DP-3"))

    def test_changing_one_shared_output_to_scene_preserves_the_other_video(self):
        video = self.item("first")
        playback.apply(video, "DP-2")
        playback.apply(video, "DP-3")
        playback.apply(self.item("scene", "scene"), "DP-2")
        state = playback.read_state()
        self.assertEqual(state["current"]["DP-2"]["backend"], playback.RENDERER_NAME)
        self.assertEqual(state["current"]["DP-3"]["content_id"], "first")
        self.assertEqual(state["current"]["DP-3"]["outputs"], ["DP-3"])
        for output, record in state["current"].items():
            self.assertTrue(playback._verify(record, output))

    def test_failed_change_restores_entire_shared_selection(self):
        item = self.item("first")
        playback.apply(item, "DP-2")
        playback.apply(item, "DP-3")
        self.fail_source = "bad"
        with self.assertRaises(playback.PlaybackError):
            playback.apply(self.item("bad"), "DP-2")
        state = playback.read_state()
        self.assertEqual({record["content_id"] for record in state["current"].values()}, {"first"})
        self.assertEqual(state["current"]["DP-2"]["pid"], state["current"]["DP-3"]["pid"])
        for output, record in state["current"].items():
            self.assertTrue(playback._verify(record, output))

    def test_reconcile_restarts_dead_shared_player_once(self):
        item = self.item("first")
        playback.apply(item, "DP-2")
        playback.apply(item, "DP-3")
        self.live.clear()
        summary = playback.reconcile()
        state = playback.read_state()
        self.assertEqual(summary["playing"], 2)
        self.assertEqual(summary["restarted"], 1)
        self.assertEqual(state["current"]["DP-2"]["pid"], state["current"]["DP-3"]["pid"])
        self.assertFalse(summary["errors"])

    def test_options_change_splits_group_instead_of_lying_about_live_options(self):
        item = self.item("first")
        playback.apply(item, "DP-2")
        playback.apply(item, "DP-3")
        state = playback.read_state()
        state["defaults"]["engine"]["fps"] = 120
        playback._write_state(state)
        playback.apply(item, "DP-2")
        current = playback.read_state()["current"]
        self.assertNotEqual(current["DP-2"]["pid"], current["DP-3"]["pid"])
        self.assertEqual(current["DP-2"]["engine"]["fps"], 120)
        self.assertEqual(current["DP-3"]["engine"]["fps"], 60)

    def test_foreign_session_is_never_stopped_or_replaced(self):
        item = self.item("first")
        playback.apply(item, "DP-2")
        state = playback.read_state()
        pid = state["current"]["DP-2"]["pid"]
        self.live[pid]["env"]["WAYLAND_DISPLAY"] = "foreign-0"
        before = copy.deepcopy(self.live)
        with self.assertRaises(playback.PlaybackError):
            playback.apply(self.item("second"), "DP-2")
        self.assertEqual(self.live, before)
        self.assertEqual(playback.read_state(), state)

    def test_missing_wayland_environment_keeps_previous_player_running(self):
        playback.apply(self.item("first"), "DP-2")
        before = copy.deepcopy(self.live)
        state = playback.read_state()
        with patch.dict(os.environ, {"WAYLAND_DISPLAY": ""}):
            with self.assertRaisesRegex(playback.PlaybackError, "WAYLAND_DISPLAY"):
                playback.apply(self.item("second"), "DP-2")
        self.assertEqual(self.live, before)
        self.assertEqual(playback.read_state(), state)

    def test_missing_wayland_socket_keeps_previous_player_running(self):
        playback.apply(self.item("first"), "DP-2")
        before = copy.deepcopy(self.live)
        state = playback.read_state()
        (Path(self.env["XDG_RUNTIME_DIR"]) / self.env["WAYLAND_DISPLAY"]).unlink()
        with self.assertRaisesRegex(playback.PlaybackError, "socket is unavailable"):
            playback.apply(self.item("second"), "DP-2")
        self.assertEqual(self.live, before)
        self.assertEqual(playback.read_state(), state)

    def test_shared_output_membership_tampering_is_unverifiable(self):
        item = self.item("first")
        playback.apply(item, "DP-2")
        record = playback.read_state()["current"]["DP-2"]
        record["outputs"] = ["DP-2", "DP-3"]
        self.assertFalse(playback._verify(record, "DP-3"))
        self.assertFalse(playback._verify(record, "DP-2"))

    def test_default_video_scaling_fills_screen_after_player_recovery(self):
        item = self.item("first")
        for scaling in (None, "default"):
            with self.subTest(scaling=scaling):
                state = playback.read_state()
                if scaling is None:
                    state["defaults"]["engine"].pop("scaling", None)
                else:
                    state["defaults"]["engine"]["scaling"] = scaling
                playback._write_state(state)
                playback.apply(item, "DP-2")
                self.live.clear()
                playback.reconcile()
                record = playback.read_state()["current"]["DP-2"]
                argv = self.live[record["pid"]]["argv"]
                settings = dict(line.split("=", 1) for line in argv[argv.index("--mpv-options") + 1].splitlines())
                self.assertEqual(settings["keepaspect"], "yes")
                self.assertEqual(settings["panscan"], "1",
                                 "default video must cover an ultrawide output, not leave side bars")

    def test_explicit_video_scaling_preserves_fit_fill_and_stretch(self):
        item = self.item("first")
        for scaling, keepaspect, panscan in (("fit", "yes", "0"),
                                            ("fill", "yes", "1"),
                                            ("stretch", "no", "0")):
            with self.subTest(scaling=scaling):
                state = playback.read_state()
                state["defaults"]["engine"]["scaling"] = scaling
                playback._write_state(state)
                playback.apply(item, "DP-2")
                record = playback.read_state()["current"]["DP-2"]
                argv = self.live[record["pid"]]["argv"]
                settings = dict(line.split("=", 1) for line in argv[argv.index("--mpv-options") + 1].splitlines())
                self.assertEqual(settings["keepaspect"], keepaspect)
                self.assertEqual(settings["panscan"], panscan)



class OwnedChildTerminationTests(unittest.TestCase):
    def test_stopping_own_renderer_reaps_child_and_process_group(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 start_new_session=True)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        record = {"pid": child.pid, "pgid": child.pid,
                  "start_time": playback._proc_start_time(child.pid)}
        with patch.object(playback, "_verify", return_value=True):
            self.assertTrue(playback._terminate(record, "DP-2"))
        self.assertIsNone(playback._proc_start_time(child.pid))
        self.assertFalse(playback._group_alive(child.pid))


if __name__ == "__main__":
    unittest.main()
