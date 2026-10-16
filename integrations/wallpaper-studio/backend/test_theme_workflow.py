import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

import context
import storage
import theme_guidance
import theme_store as fs
import theme_workflow

RESOURCES = Path(__file__).resolve().parents[1] / 'resources/theme-library'
CONSUMERS = RESOURCES.parents[1] / 'consumers'
CONTENT = 'd' * 64


def png(color):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    row = b'\0' + bytes(color) * 16
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 16, 16, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(row * 16)) + chunk(b'IEND', b'')


def analyzer():
    sys.path.insert(0, str(CONSUMERS))
    try:
        spec = importlib.util.spec_from_file_location('theme_analysis_test_module', CONSUMERS / 'analyze-wallpapers.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(CONSUMERS))


def visual():
    return {'summary': 'Night', 'tags': ['quiet'], 'objects': ['lamp'], 'materials': ['wood'],
            'style': ['painted'], 'vibe': ['quiet'], 'temperature': 'warm', 'colors': [],
            'composition': 'Open center', 'motion': {'level': 'unknown', 'description': 'No observed motion'},
            'confidence': 0.8, 'uncertainties': ['Motion is not observable in a still']}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = fs.Store(self.base / 'theme')
        theme_guidance.seed(self.store.root, RESOURCES)
        self.wp = self.store.wallpaper(CONTENT, 'Night')
        self.frame = self.wp / 'frames/source.png'
        self.frame.parent.mkdir()
        self.frame.write_bytes(png((12, 24, 36)))
        self.analysis = {'schema_version': 1, 'profile': 'visual-v1', 'content_sha256': CONTENT,
                         'created_at': '2026-10-13T00:00:00Z', 'model': 'recorded-model',
                         'evidence': {'kind': 'image', 'motion_observable': False,
                                      'frames': [{'file': str(self.frame), 'time_s': 0.0, 'sha256': fs.digest(self.frame.read_bytes())}]},
                         'visual': visual()}
        fs.atomic(self.wp / 'analysis.json', fs.encoded(self.analysis))

    def test_frozen_context_ignores_later_owner_edit_and_excludes_unrelated_kitty_rules(self):
        folder = self.wp / '.history/generations/check'
        original = theme_guidance.contract(folder, 'zen-browser', base=self.store.root)
        rule = self.store.root / 'design-rules/zen-browser/frame-rules.md'
        rule.write_text('Owner edited the next generation policy')
        (self.store.root / 'design-rules/kitty/animation-rules.md').write_bytes(b'\xff invalid irrelevant document')
        self.assertEqual(original, theme_guidance.contract(folder, 'zen-browser', base=self.store.root))
        next_contract = theme_guidance.contract(self.wp / '.history/generations/next', 'zen-browser', base=self.store.root)
        self.assertIn('Owner edited', next_contract)
        self.assertNotIn('invalid irrelevant', next_contract)

    def test_no_consent_or_changed_frozen_inputs_refuses_before_model_spending(self):
        job = {'id': 'candidate', 'kind': 'theme_element', 'content_id': CONTENT,
               'payload': {'action': 'theme_element', 'element': 'lamp', 'metadata': {'usage': 'One foreground lamp', 'animation': 'static'}, 'reason_new': 'No matching approved element'}}
        with self.assertRaises(ValueError):
            theme_workflow.prepare(self.store.root, job)
        job['payload']['consent'] = theme_workflow.quote(job['payload'])
        with patch.object(context, 'analyzer_module', return_value=analyzer()):
            folder = theme_workflow.prepare(self.store.root, job)
        inputs = fs.read(folder / 'inputs.json')
        inputs['preferences']['vision_model'] = 'changed-model'
        fs.atomic(folder / 'inputs.json', fs.encoded(inputs))
        with patch.object(theme_workflow.art_direction, 'model_json') as model, patch.object(theme_workflow, 'image') as image:
            with self.assertRaises(ValueError):
                theme_workflow.run_job(self.store.root, job, lambda: False)
            model.assert_not_called()
            image.assert_not_called()

    def test_repeated_frames_cannot_claim_change_and_base_binding_is_checked(self):
        module = analyzer()
        evidence = {'kind': 'video-frames', 'motion_observable': False, 'frames': [self.analysis['evidence']['frames'][0]] * 6}
        reply = {'overview': 'Quiet scene', 'base_frame': {'index': 2, 'reason': 'Settled composition'},
                 'baseline': visual(), 'synthesis': visual(),
                 'comparison': [{'from': i, 'to': i + 1, 'changed': False, 'changes': [], 'uncertainty': 'No visible change'} for i in range(5)],
                 'coverage': {'state': 'sampled', 'scene_count': 1, 'uncertainties': ['Unsampled intervals are unknown']}, 'supplements': []}
        accepted, _ = module.validate_analysis(reply, evidence)
        self.assertEqual(evidence['frames'][2]['sha256'], accepted['base_frame']['sha256'])
        reply['comparison'][0]['changed'] = True
        with self.assertRaises(ValueError):
            module.validate_analysis(reply, evidence)
        reply['comparison'][0]['changed'] = False
        reply['base_frame']['sha256'] = 'e' * 64
        with self.assertRaises(ValueError):
            module.validate_analysis(reply, evidence)

    def test_real_contact_sheet_and_legacy_cache_survive_path_migration(self):
        module = analyzer()
        output = self.wp / 'frames/contact-sheet.webp'
        module.contact_sheet(self.analysis['evidence'], output)
        self.assertTrue(output.read_bytes().startswith(b'RIFF'))
        legacy = self.base / 'legacy'
        (legacy / 'analysis').mkdir(parents=True)
        fs.atomic(legacy / 'index.json', fs.encoded({'schema_version': 1, 'items': [{'id': CONTENT, 'title': 'Night'}]}))
        fs.atomic(legacy / 'analysis' / (CONTENT + '.json'), fs.encoded(self.analysis))
        migrated = self.base / 'migrated'
        fs.import_legacy(legacy, migrated)
        copied = fs.read(fs.Store(migrated).analysis_path(CONTENT))
        module.validate_envelope(copied, CONTENT, require_full=True)
        self.assertNotEqual(str(self.frame), copied['evidence']['frames'][0]['file'])
        self.assertEqual(self.analysis['evidence']['frames'][0]['sha256'], copied['evidence']['frames'][0]['sha256'])


    def test_named_supplement_preserves_baseline_and_rejects_stale_publication(self):
        module = analyzer()
        frames = [{'file': str(self.frame), 'time_s': float(i), 'sha256': fs.digest(self.frame.read_bytes())}
                  for i in range(6)]
        evidence = {'kind': 'rendered-frames', 'motion_observable': False, 'frames': frames}
        sections = {'overview': 'An intentional night scene',
                    'base_frame': {'index': 2, 'reason': 'Stable light', 'sha256': frames[2]['sha256']},
                    'baseline': visual(), 'synthesis': visual(),
                    'comparison': [{'from': i, 'to': i + 1, 'changed': False, 'changes': [], 'uncertainty': 'Unsampled intervals unknown'} for i in range(5)],
                    'coverage': {'state': 'sampled', 'scene_count': 1, 'uncertainties': ['Rare events unknown']}, 'supplements': ['Check lamp at the loop end']}
        envelope = {**self.analysis, 'schema_version': 2, 'profile': 'visual-v2', 'evidence': evidence, 'analysis': sections}
        fs.atomic(self.wp / 'analysis.json', fs.encoded(envelope))
        before = (self.wp / 'analysis.json').read_bytes()
        revision = fs.digest(before)
        observation = {'name': 'loop-end', 'reason': 'Check a rare lamp state',
                       'parent_revision': revision, 'evidence': evidence, 'model': 'recorded-model',
                       'created_at': '2026-10-13T00:00:00Z', 'observations': ['Lamp remains visible'],
                       'corrections': [], 'uncertainties': ['Only this sampled interval was examined']}
        synthesis = {**visual(), 'summary': 'Night scene; lamp persists in the additional sampled interval'}
        result = module.publish_supplement(self.store, CONTENT, observation, synthesis, expected=revision)
        self.assertEqual(sections['baseline'], result['analysis']['baseline'])
        self.assertEqual(sections['base_frame'], result['analysis']['base_frame'])
        self.assertEqual(synthesis, result['visual'])
        self.assertEqual(before, (self.wp / '.history/analysis' / revision / 'analysis.json').read_bytes())
        with self.assertRaises(fs.Conflict):
            module.publish_supplement(self.store, CONTENT, {**observation, 'name': 'later'}, synthesis, expected=revision)
        self.assertEqual(1, len(fs.read(self.wp / 'analysis.json')['analysis']['supplement_records']))

    def test_multi_scene_analysis_requires_source_bound_representative_states(self):
        module = analyzer()
        evidence = {'kind': 'image', 'motion_observable': False, 'frames': [self.analysis['evidence']['frames'][0]]}
        sections = {'overview': 'One observed state', 'base_frame': {'index': 0, 'reason': 'Only still'},
                    'baseline': visual(), 'synthesis': visual(), 'comparison': [],
                    'coverage': {'state': 'multi-scene', 'scene_count': 2, 'uncertainties': ['Second scene not supplied']}, 'supplements': []}
        with self.assertRaises(ValueError):
            module.validate_analysis(sections, evidence)

    def test_invalid_or_layered_adaptation_does_not_publish_a_candidate(self):
        element = self.wp / 'shared/lamp'
        element.mkdir(parents=True)
        (element / 'element.png').write_bytes(png((20, 30, 40)))
        metadata = {'kind': 'object', 'roles': ['foreground'], 'usage': 'One lamp',
                    'adaptations': ['scale'], 'unique': True, 'animation': 'static', 'layers': []}
        pin = self.store.accept_element(CONTENT, 'lamp', metadata, expected=None)
        with self.assertRaises(ValueError):
            theme_workflow.adapt(self.store, CONTENT, pin, 'bad-width', 'scale', {'width': -1})
        self.assertFalse((self.wp / 'shared/bad-width').exists())
        (element / 'flame.png').write_bytes(png((100, 40, 10)))
        layered = {**metadata, 'animation': 'layered',
                   'layers': [{'file': 'flame.png', 'pivot': [8, 12], 'ownership': 'lamp-flame'}]}
        layered_pin = self.store.accept_element(CONTENT, 'lamp', layered, expected=pin['revision'])
        with self.assertRaises(ValueError):
            theme_workflow.adapt(self.store, CONTENT, layered_pin, 'broken-layered', 'scale', {'width': 32})
        self.assertFalse((self.wp / 'shared/broken-layered').exists())
        self.assertEqual([8, 12], fs.read(self.store.resolve_pin(CONTENT, layered_pin) / 'element.json')['layers'][0]['pivot'])

    def test_legacy_reset_refuses_theme_dependencies_before_touching_native_state(self):
        import reset
        design = self.wp / 'system-design/chosen'
        design.mkdir(parents=True)
        (design / 'design.json').write_text('{}')
        analysis_before = (self.wp / 'analysis.json').read_bytes()
        with patch.object(reset, '_active_pack_ids') as active:
            with self.assertRaises(reset.ResetError):
                reset._perform(self.store.root, CONTENT)
        active.assert_not_called()
        self.assertEqual(analysis_before, (self.wp / 'analysis.json').read_bytes())
        self.assertTrue((design / 'design.json').is_file())
        self.assertFalse((self.wp / '.history/reset').exists())

if __name__ == '__main__':
    unittest.main()
