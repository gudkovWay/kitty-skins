"""Native unittest regressions for Noctalia instance state and binary routing."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import noctalia_bundle
import playback


class NoctaliaSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"HOME": str(self.root),
                                                    "XDG_DATA_HOME": str(self.root / "data")}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_two_instances_do_not_read_or_overwrite_each_others_playback(self):
        for name, enabled in [("primary", True), ("secondary", False)]:
            with patch.dict(os.environ, {"NOCTALIA_SESSION_STATE_DIR": str(self.root / name)}):
                state = playback.default_state()
                state["enabled"] = enabled
                with playback.playback_lock():
                    playback._write_state(state)
        for name, enabled in [("primary", True), ("secondary", False)]:
            with patch.dict(os.environ, {"NOCTALIA_SESSION_STATE_DIR": str(self.root / name)}):
                self.assertEqual(playback.read_state()["enabled"], enabled)

    def test_final_session_directory_wins_without_an_extra_noctalia_suffix(self):
        final = self.root / "state" / "noctalia" / "sessions" / "instance"
        with patch.dict(os.environ, {"NOCTALIA_SESSION_STATE_DIR": str(final),
                                    "NOCTALIA_STATE_HOME": str(self.root / "wrong")}):
            self.assertEqual(playback.plugin_dir(), final / "plugins/data/q/wallpaper-studio")

    def test_relative_session_directory_fails_instead_of_using_global_state(self):
        with patch.dict(os.environ, {"NOCTALIA_SESSION_STATE_DIR": "sessions/instance"}):
            with self.assertRaises(playback.PlaybackError):
                playback.read_state()

    def test_native_base_override_and_xdg_default_have_matching_suffix_semantics(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "xdg")}):
            self.assertEqual(playback._state_root(), self.root / "xdg/noctalia")
            with patch.dict(os.environ, {"NOCTALIA_STATE_HOME": str(self.root / "native")}):
                self.assertEqual(playback._state_root(), self.root / "native/noctalia")

    def test_instance_binary_wins_over_an_incompatible_global_runtime_record(self):
        record = self.root / "data/wallpaper-studio/native-runtime.json"
        record.parent.mkdir(parents=True)
        record.write_text('{"schema_version": 1, "binary": "/missing/global/noctalia"}')
        with patch.dict(os.environ, {"NOCTALIA_BINARY": "/instance/bin/noctalia"}):
            self.assertEqual(noctalia_bundle._binary(), "/instance/bin/noctalia")
            with patch.dict(os.environ, {"NOCTALIA_SKIN_BINARY": "/explicit/bin/noctalia"}):
                self.assertEqual(noctalia_bundle._binary(), "/explicit/bin/noctalia")
        with self.assertRaises(noctalia_bundle.NoctaliaError):
            noctalia_bundle._binary()


if __name__ == "__main__":
    unittest.main()
