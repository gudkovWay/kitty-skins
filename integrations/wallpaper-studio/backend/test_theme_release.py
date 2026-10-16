import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import theme_guidance
import theme_release
import theme_store

CONTENT = 'c' * 64
RESOURCES = Path(__file__).resolve().parents[1] / 'resources/theme-library'


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = theme_store.Store(self.base / 'theme')
        theme_guidance.seed(self.store.root, RESOURCES)
        self.wp = self.store.wallpaper(CONTENT, 'Quiet night')
        theme_store.atomic(self.wp / 'analysis.json', theme_store.encoded({'content_sha256': CONTENT, 'visual': {'summary': 'Night'}}))
        self.chrome = self.base / 'profile/chrome'
        self.chrome.mkdir(parents=True)
        (self.chrome / 'userChrome.css').write_text('@import url("custom.css");\n/* User settings */\n')
        theme_store.atomic(self.store.root / '.state/adapters.json', theme_store.encoded({'zen-browser': {'chrome': str(self.chrome)}}))
        value = {'layout_mode': 'suggest-layout', 'artistic_mode': 'specification',
                 'topology': {'bars': 1, 'music': 'inside-bar', 'ai': 'inside-bar'},
                 'rationale': 'One bar preserves the wallpaper horizon', 'wallpaper_facts': ['Quiet horizon'],
                 'palette': {'background': '#112233', 'foreground': '#eeddcc', 'accent': '#aabbcc'},
                 'elements': [], 'apps': {'zen-browser': {'motion': 'static', 'reason': 'Reading'}, 'ayugram': {'reason': 'Legibility'}}, 'status': 'concept'}
        self.design = self.store.save_design(CONTENT, 'quiet', value, expected=None)

    def prepare(self, targets):
        for target in targets:
            detail = theme_release.build_detail(self.store, CONTENT, 'quiet', target)
            theme_release.approve_detail(self.store, CONTENT, 'quiet', target, detail['revision'])
        return theme_release.publish(self.store, CONTENT, 'quiet', targets)

    def test_user_edit_blocks_apply_without_touching_any_config(self):
        release = self.prepare(['zen-browser'])
        original = '@import url("custom.css");\n/* edited since preview */\n'
        (self.chrome / 'userChrome.css').write_text(original)
        with self.assertRaises(theme_store.Conflict):
            theme_release.apply(self.store, CONTENT, release['release_id'], ['zen-browser'])
        self.assertEqual(original, (self.chrome / 'userChrome.css').read_text())
        self.assertFalse((self.chrome / 'theme-library.css').exists())

    def test_rollback_preserves_unrelated_edits_and_selection_does_not_apply(self):
        release = self.prepare(['zen-browser'])
        self.assertFalse((self.chrome / 'theme-library.css').exists())
        receipt = theme_release.apply(self.store, CONTENT, release['release_id'], ['zen-browser'])
        self.assertEqual('installed-awaiting-restart', receipt['targets']['zen-browser']['status'])
        p = self.chrome / 'userChrome.css'
        p.write_text(p.read_text() + '\n/* new user setting */\n')
        theme_release.rollback(self.store, CONTENT, receipt['deployment_id'])
        self.assertTrue(p.read_text().endswith('/* new user setting */\n'))
        self.assertFalse((self.chrome / 'theme-library.css').exists())
        self.assertIn('@import url("custom.css");', p.read_text())

    def test_export_is_a_real_ayugram_theme_but_not_runtime_activation(self):
        release = self.prepare(['ayugram'])
        theme = Path(release['targets']['ayugram']['artifact'])
        with zipfile.ZipFile(theme) as archive:
            colors = archive.read('colors.tdesktop-theme').decode()
        self.assertIn('windowBg: #112233;', colors)
        receipt = theme_release.apply(self.store, CONTENT, release['release_id'], ['ayugram'])
        self.assertEqual('exported-awaiting-user-import', receipt['targets']['ayugram']['status'])

    def test_modified_published_dependency_is_rejected(self):
        release = self.prepare(['zen-browser'])
        Path(release['targets']['zen-browser']['artifact']).write_text('tampered')
        with self.assertRaises(ValueError):
            theme_release.apply(self.store, CONTENT, release['release_id'], ['zen-browser'])
        self.assertFalse((self.chrome / 'theme-library.css').exists())

    def test_unsupported_discord_is_not_publishable(self):
        detail = theme_release.build_detail(self.store, CONTENT, 'quiet', 'discord')
        self.assertEqual('requires-component', detail['status'])
        with self.assertRaises(ValueError):
            theme_release.approve_detail(self.store, CONTENT, 'quiet', 'discord', detail['revision'])

    def test_ornament_intent_is_not_silently_published_as_palette_only(self):
        value = self.design['design']
        value['apps']['zen-browser']['elements'] = ['paper-crane foreground frame']
        self.store.save_design(CONTENT, 'quiet', value, expected=self.design['revision'])
        detail = theme_release.build_detail(self.store, CONTENT, 'quiet', 'zen-browser')
        self.assertEqual('requires-component', detail['status'])
        with self.assertRaises(ValueError):
            theme_release.approve_detail(self.store, CONTENT, 'quiet', 'zen-browser', detail['revision'])
        self.assertFalse((self.chrome / 'theme-library.css').exists())

    def test_analysis_change_cannot_be_relabelled_as_the_old_approved_design(self):
        detail = theme_release.build_detail(self.store, CONTENT, 'quiet', 'zen-browser')
        theme_release.approve_detail(self.store, CONTENT, 'quiet', 'zen-browser', detail['revision'])
        theme_store.atomic(self.wp / 'analysis.json', theme_store.encoded({'content_sha256': CONTENT, 'visual': {'summary': 'Corrected evidence'}}))
        with self.assertRaises(theme_store.Conflict):
            theme_release.publish(self.store, CONTENT, 'quiet', ['zen-browser'])
        with self.assertRaises(theme_store.Conflict):
            theme_release.build_detail(self.store, CONTENT, 'quiet', 'zen-browser')
        self.assertFalse((self.wp / 'releases').exists())

    def test_frame_pack_does_not_imply_a_compatible_shojiwm_adapter(self):
        from unittest.mock import patch
        variant = {'id': 'f' * 32, 'content_id': CONTENT}
        with patch.object(theme_release.storage, 'read_state', return_value={'variants': [variant]}):
            with self.assertRaises(ValueError):
                theme_release._variant(self.store, CONTENT, 'frame', variant['id'])


if __name__ == '__main__':
    unittest.main()
