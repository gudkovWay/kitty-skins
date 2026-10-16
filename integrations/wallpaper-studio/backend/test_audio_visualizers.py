"""Native unittest coverage of real Noctalia frozen audio-reference delivery."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import noctalia_generator
import theme_guidance

RESOURCES = Path(__file__).resolve().parent.parent / "resources/theme-library"
AUDIO_JSON = "design-rules/noctalia/audio-visualizers.json"
AUDIO_DOC = "design-rules/noctalia/audio-visualizers.md"
MODES = {"ribbons", "contour", "bars", "wave", "mist", "ornament"}


class AudioReferencesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "theme"
        theme_guidance.seed(self.base, RESOURCES)
        self.skill = self.root / "keyboard-skill.md"
        self.skill.write_text("Authored keyboard guidance for the isolated test fixture.\n")
        self.env = patch.dict(os.environ, {
            "HOME": str(self.root),
            "WALLPAPER_STUDIO_DATA_DIR": str(self.base),
            "THEME_HOME": str(self.base),
            noctalia_generator.SKILL_PATH_ENV: str(self.skill),
            noctalia_generator.POLICY_PATH_ENV: str(self.base / "design-rules/noctalia/generation-policy.json"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = self.root / "job"

    def subject(self, output=None):
        output = output or self.output
        guidance = noctalia_generator.freeze_guidance(output)
        return noctalia_generator._subject(
            item={"title": "Isolated wallpaper"}, family="stagecraft",
            variant_id="b" * 32, content_id="a" * 64,
            reference={}, preferences={}, notes="", feedback=[],
            parent_recipe=None, parent_variant_id=None, batch_id=None,
            ordinal=1, count=1, guidance=guidance,
            guidance_dir=output / "pipeline/noctalia-guidance", image_role="scene",
        )

    def test_real_noctalia_image_subject_contains_complete_six_mode_knowledge(self):
        subject = self.subject()
        text = (self.base / AUDIO_JSON).read_text()
        catalog = json.loads(text)
        self.assertEqual({p["mode"] for p in catalog["presets"]}, MODES)
        for preset in catalog["presets"]:
            self.assertEqual(preset["settings"]["visualization_mode"], preset["mode"])
            self.assertGreater(preset["box_width"], 0)
            self.assertGreater(preset["box_height"], 0)
        self.assertIn(text, subject)
        self.assertIn((self.base / AUDIO_DOC).read_text(), subject)
        self.assertIn(hashlib.sha256(text.encode()).hexdigest(), subject)

    def test_existing_job_keeps_exact_reference_after_source_edit(self):
        before = self.subject()
        source = self.base / AUDIO_JSON
        catalog = json.loads(source.read_text())
        catalog["presets"][0]["structure"] = "A revised authored ribbon reference"
        source.write_text(json.dumps(catalog))
        self.assertEqual(before, self.subject())
        self.assertNotEqual(before, self.subject(self.root / "next-job"))

    def test_tampered_frozen_audio_knowledge_fails_before_model_request(self):
        self.subject()
        frozen = self.output / "pipeline/theme-guidance" / AUDIO_JSON
        frozen.write_text(frozen.read_text() + "\nchanged")
        with self.assertRaises(ValueError):
            self.subject()

    def test_missing_required_audio_knowledge_is_not_silently_omitted(self):
        (self.base / AUDIO_JSON).unlink()
        with self.assertRaises(OSError):
            self.subject()

    def test_frame_prompt_does_not_receive_desktop_visualizer_knowledge(self):
        contract = theme_guidance.contract(self.root / "frame-job", "frame", base=self.base)
        self.assertNotIn(AUDIO_JSON, contract)
        self.assertNotIn(AUDIO_DOC, contract)
        self.assertNotIn((self.base / AUDIO_JSON).read_text(), contract)

    def test_system_design_inherits_complete_desktop_reference(self):
        contract = theme_guidance.contract(self.root / "system-job", "system-design", base=self.base)
        self.assertIn((self.base / AUDIO_JSON).read_text(), contract)
        self.assertIn((self.base / AUDIO_DOC).read_text(), contract)


if __name__ == "__main__":
    unittest.main()
