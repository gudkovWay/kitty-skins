"""Behavioral coverage for explicit, non-activating bamboo bundle staging."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("bamboo_02", Path(__file__).with_name("bamboo-02.py"))
bamboo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bamboo)


class BundleStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        self.pack = self.repo / "assets/skins/bamboo-02"
        self.pack.mkdir(parents=True)
        self.shader = self.repo / "src/bamboo/shaders"
        self.shader.mkdir(parents=True)
        (self.shader / "scene.frag").write_text("source shader bytes\n")
        self.art = b"unchanged artwork bytes"
        (self.pack / "frame-atlas.png").write_bytes(self.art)
        (self.pack / "pack.json").write_text(json.dumps({
            "id": "bamboo-02", "renderer": "bamboo02", "assets": {
                "frame-atlas.png": {"sha256": hashlib.sha256(self.art).hexdigest()}
            }
        }))
        self.plugin = self.root / "libbamboo-02.so"
        self.plugin.write_bytes(b"compiled plugin fixture")
        self.target = self.root / "bundle with spaces"

    def test_stage_copies_exact_bytes_without_activating_any_live_configuration(self):
        live = self.root / "kitty.conf"
        live.write_text("existing owner configuration\n")
        result = bamboo.stage(self.repo, self.plugin, self.target, "header-hash")
        self.assertEqual((self.target / "pack/frame-atlas.png").read_bytes(), self.art)
        self.assertEqual((self.target / "libbamboo-02.so").read_bytes(), self.plugin.read_bytes())
        self.assertEqual((self.target / "pack/shaders/scene.frag").read_text(), "source shader bytes\n")
        self.assertEqual(live.read_text(), "existing owner configuration\n")
        self.assertEqual(result["activation"], "not-applied")
        self.assertEqual(result["hyprland_header_hash"], "header-hash")
        self.assertIn(str(self.target / "pack"), (self.target / "hyprland.lua").read_text())

    def test_existing_bundle_is_never_overwritten(self):
        self.target.mkdir()
        kept = self.target / "owner-file"
        kept.write_text("keep")
        with self.assertRaises(FileExistsError):
            bamboo.stage(self.repo, self.plugin, self.target, "hash")
        self.assertEqual(kept.read_text(), "keep")

    def test_corrupt_art_rejects_before_creating_destination(self):
        (self.pack / "frame-atlas.png").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "digest"):
            bamboo.stage(self.repo, self.plugin, self.target, "hash")
        self.assertFalse(self.target.exists())

    def test_manifest_path_escape_is_rejected(self):
        manifest = json.loads((self.pack / "pack.json").read_text())
        manifest["assets"]["../outside"] = {"sha256": "0" * 64}
        (self.pack / "pack.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "asset path"):
            bamboo.stage(self.repo, self.plugin, self.target, "hash")
        self.assertFalse(self.target.exists())

    def test_symlink_destination_is_not_followed(self):
        owner = self.root / "owner"
        owner.mkdir()
        self.target.symlink_to(owner, target_is_directory=True)
        with self.assertRaises(FileExistsError):
            bamboo.stage(self.repo, self.plugin, self.target, "hash")
        self.assertEqual(list(owner.iterdir()), [])

    def test_nested_control_art_is_verified_and_preserved(self):
        controls = self.pack / "controls"
        controls.mkdir()
        plate = controls / "plate.png"
        plate.write_bytes(b"authored control")
        manifest = json.loads((self.pack / "pack.json").read_text())
        manifest["assets"]["controls/plate.png"] = {
            "sha256": hashlib.sha256(plate.read_bytes()).hexdigest()
        }
        (self.pack / "pack.json").write_text(json.dumps(manifest))
        bamboo.stage(self.repo, self.plugin, self.target, "hash")
        self.assertEqual((self.target / "pack/controls/plate.png").read_bytes(), plate.read_bytes())

    def test_symlink_asset_directory_is_rejected_before_reading_external_bytes(self):
        outside = self.root / "external"
        outside.mkdir()
        (outside / "plate.png").write_bytes(b"external")
        (self.pack / "controls").symlink_to(outside, target_is_directory=True)
        manifest = json.loads((self.pack / "pack.json").read_text())
        manifest["assets"]["controls/plate.png"] = {
            "sha256": hashlib.sha256(b"external").hexdigest()
        }
        (self.pack / "pack.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "Symlink asset"):
            bamboo.stage(self.repo, self.plugin, self.target, "hash")
        self.assertFalse(self.target.exists())

    def test_destinations_under_inputs_and_symlink_aliases_are_rejected(self):
        alias = self.root / "pack-alias"
        alias.symlink_to(self.pack, target_is_directory=True)
        for parent in (self.pack, self.shader, alias):
            with self.subTest(parent=parent):
                with self.assertRaisesRegex(ValueError, "inside a copied input"):
                    bamboo.stage(self.repo, self.plugin, parent / "bundle", "hash")
                self.assertFalse((parent / "bundle").exists())
                self.assertEqual(list(parent.glob(".bundle-*")), [])

    def test_source_mutation_during_copy_never_publishes_inconsistent_assets(self):
        original = bamboo.shutil.copytree
        def mutate_then_copy(source, destination, *args, **kwargs):
            if Path(source) == self.pack:
                (self.pack / "frame-atlas.png").write_bytes(b"concurrent edit")
            return original(source, destination, *args, **kwargs)
        with patch.object(bamboo.shutil, "copytree", side_effect=mutate_then_copy):
            with self.assertRaisesRegex(ValueError, "Staged asset digest"):
                bamboo.stage(self.repo, self.plugin, self.target, "hash")
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.root.glob(".bundle with spaces-*")), [])

    def test_plugin_rebuild_after_copy_does_not_change_staged_binary_identity(self):
        original = bamboo.shutil.copy2
        expected = hashlib.sha256(self.plugin.read_bytes()).hexdigest()
        def copy_then_rebuild(source, destination, *args, **kwargs):
            result = original(source, destination, *args, **kwargs)
            if Path(source) == self.plugin:
                self.plugin.write_bytes(b"new independent build")
            return result
        with patch.object(bamboo.shutil, "copy2", side_effect=copy_then_rebuild):
            result = bamboo.stage(self.repo, self.plugin, self.target, "hash")
        copied = hashlib.sha256((self.target / "libbamboo-02.so").read_bytes()).hexdigest()
        self.assertEqual(result["source_plugin_sha256"], expected)
        self.assertEqual(copied, expected)

    def test_publication_collision_never_overwrites_or_removes_owner_bytes(self):
        original = bamboo._publish
        for use_symlink in (False, True):
            with self.subTest(symlink=use_symlink):
                owner = self.root / f"owner-{use_symlink}"
                owner.mkdir()
                protected = owner / "libbamboo-02.so"
                protected.write_bytes(b"owner binary")
                def collide(parent_fd, scratch_name, target_name):
                    if use_symlink:
                        self.target.symlink_to(owner, target_is_directory=True)
                    else:
                        owner.rename(self.target)
                    return original(parent_fd, scratch_name, target_name)
                with patch.object(bamboo, "_publish", side_effect=collide):
                    with self.assertRaises(FileExistsError):
                        bamboo.stage(self.repo, self.plugin, self.target, "hash")
                self.assertEqual((self.target / "libbamboo-02.so").read_bytes(), b"owner binary")
                self.assertEqual(list(self.root.glob(".bundle with spaces-*")), [])
                if use_symlink:
                    self.target.unlink()
                else:
                    self.target.rename(owner)

    def test_unbuilt_plugin_does_not_create_partial_bundle(self):
        self.plugin.unlink()
        with self.assertRaises(FileNotFoundError):
            bamboo.stage(self.repo, self.plugin, self.target, "hash")
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
