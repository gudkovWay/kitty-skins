"""Behavioral coverage for the wallpaper-owned theme library."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import theme_store

A = 'a' * 64
B = 'b' * 64


class ThemeStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'theme'
        self.store = theme_store.Store(self.root)
        self.store.initialize()

    def test_identity_survives_title_change_and_history_is_wallpaper_owned(self):
        first = self.store.wallpaper(A, 'Rain at night')
        self.assertEqual(first, self.store.wallpaper(A, 'Renamed source'))
        rev = self.store.put(A, 'analysis.json', {'summary': 'Night'}, expected=None)
        self.store.put(A, 'analysis.json', {'summary': 'Rainy night'}, expected=rev)
        self.assertEqual({'summary': 'Night'}, json.loads((first / '.history/analysis' / rev / 'analysis.json').read_text()))
        self.assertFalse((self.root / '.history').exists())
        self.assertNotEqual(first, self.store.wallpaper(B, 'Rain at night'))
        with self.assertRaises(theme_store.Conflict):
            self.store.put(A, 'analysis.json', {'summary': 'stale'}, expected=rev)
        self.assertEqual({'summary': 'Rainy night'}, json.loads((first / 'analysis.json').read_text()))

    def test_element_pins_do_not_follow_later_edits(self):
        wp = self.store.wallpaper(A, 'Rain')
        asset = wp / 'shared/lamp/element.png'
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b'first lamp')
        metadata = {'kind': 'object', 'roles': ['foreground'], 'usage': 'One hanging lamp',
                    'adaptations': ['scale'], 'unique': True, 'layers': [], 'animation': 'static'}
        pin = self.store.accept_element(A, 'lamp', metadata, expected=None)
        asset.write_bytes(b'new lamp')
        newer = self.store.accept_element(A, 'lamp', metadata, expected=pin['revision'])
        self.assertNotEqual(pin['revision'], newer['revision'])
        self.assertEqual(b'first lamp', self.store.resolve_pin(A, pin).joinpath('element.png').read_bytes())
        with self.assertRaises(ValueError):
            self.store.resolve_pin(B, pin)

    def test_primary_retains_alternatives_without_deployment(self):
        first = self.store.save_design(A, 'quiet', self.design('One bar'), expected=None)
        second = self.store.save_design(A, 'split', self.design('Two bars'), expected=None)
        self.store.choose_primary(A, 'quiet', first['revision'], expected=None)
        self.assertEqual('quiet', self.store.selection(A)['primary'])
        self.assertEqual(second['revision'], self.store.design(A, 'split')['revision'])
        self.assertFalse((self.root / '.state/deployments.json').exists())

    def design(self, reason):
        return {'layout_mode': 'suggest-layout', 'artistic_mode': 'specification',
                'topology': {'bars': 1, 'music': 'inside-bar', 'ai': 'inside-bar'},
                'rationale': reason, 'wallpaper_facts': ['negative space at top'],
                'palette': {'background': '#102030', 'foreground': '#e0d0c0', 'accent': '#a08050'},
                'elements': [], 'apps': {'zen-browser': {'motion': 'static', 'reason': 'Keep document reading quiet'}},
                'status': 'concept'}

    def test_path_escape_and_symlink_are_rejected(self):
        wp = self.store.wallpaper(A)
        with self.assertRaises(ValueError):
            self.store.put(A, '../outside.json', {}, expected=None)
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        (wp / 'shared').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.store.accept_element(A, 'lamp', {}, expected=None)
        with self.assertRaises(ValueError):
            self.store.wallpaper('../../escape')

    def test_import_preserves_analysis_ids_artifacts_and_raw_provenance(self):
        old = Path(self.tmp.name) / 'legacy'
        (old / 'analysis').mkdir(parents=True)
        frame = old / 'frames' / A / 'candidate' / 'preview.png'
        frame.parent.mkdir(parents=True)
        frame.write_bytes(b'approved art')
        analysis = {'content_sha256': A, 'visual': {'summary': 'Night'}}
        (old / 'analysis' / (A + '.json')).write_text(json.dumps(analysis))
        (old / 'index.json').write_text(json.dumps({'schema_version': 1, 'items': [{'id': A, 'title': 'Rain'}]}))
        state = {'schema_version': 5, 'variants': [{'id': 'candidate', 'content_id': A, 'preview': str(frame)}], 'mappings': {A: 'candidate'}, 'jobs': [], 'noctalia': {}}
        (old / 'studio.json').write_text(json.dumps(state))
        imported = Path(self.tmp.name) / 'imported'
        report = theme_store.import_legacy(old, imported)
        new = theme_store.Store(imported)
        self.assertEqual(analysis, json.loads(new.analysis_path(A).read_text()))
        migrated = json.loads((imported / '.state/studio.json').read_text())
        self.assertEqual(state['mappings'], migrated['mappings'])
        self.assertEqual(b'approved art', Path(migrated['variants'][0]['preview']).read_bytes())
        self.assertEqual(state, json.loads((new.wallpaper(A) / '.history/import/studio.json').read_text()))
        self.assertEqual(1, report['wallpapers'])
        self.assertTrue(frame.exists())
        with self.assertRaises(theme_store.Conflict):
            theme_store.import_legacy(old, imported)

    def test_file_pages_are_complete_bounded_and_wallpaper_scoped(self):
        wp = self.store.wallpaper(A, 'Rain')
        other = self.store.wallpaper(B, 'Snow')
        for index in range(75):
            file = wp / 'system-design/raw/artifacts' / f'{index:03}.json'
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('{}')
        unrelated = other / 'system-design/raw/artifacts/private.json'
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text('{}')
        seen = []
        for page in range(3):
            result = self.store.browse(file_section='Designs', content_id=A, page=page)
            self.assertEqual(75, result['section_counts']['Designs'])
            self.assertLessEqual(len(result['sections']['Designs']), 30)
            seen.extend(row['path'] for row in result['sections']['Designs'])
        self.assertEqual(75, len(set(seen)))
        self.assertNotIn(str(unrelated), seen)

    def test_summary_is_small_and_late_alternatives_remain_selectable_by_page(self):
        wp = self.store.wallpaper(A, 'Rain')
        self.store.wallpaper(B, 'Snow')
        for index in range(61):
            self.store.save_design(A, f'choice-{index:03}', self.design(f'Choice {index}'), expected=None)
        summary = self.store.browse(summary_only=True)
        entry = next(w for w in summary['wallpapers'] if w['content_id'] == A)
        self.assertEqual(61, entry['counts']['designs'])
        self.assertEqual([], entry['designs'])
        last = self.store.browse(content_id=A, page=2)['wallpapers']
        self.assertEqual([A], [w['content_id'] for w in last])
        self.assertEqual(['choice-060'], [d['design_id'] for d in last[0]['designs']])
        selected = last[0]['designs'][0]
        self.store.choose_primary(A, selected['design_id'], selected['revision'], expected=None)
        self.assertEqual('choice-060', self.store.selection(A)['primary'])


if __name__ == '__main__':
    unittest.main()
