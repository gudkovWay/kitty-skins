import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import hashlib

import art_direction
import profiles

CONSUMERS = Path(__file__).resolve().parents[1] / 'consumers'


class AnalysisTests(unittest.TestCase):
    def test_reference_uses_declared_base_and_never_substitutes_a_missing_frame(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            frames = []
            for i in range(2):
                p = base / f'{i}.png'
                p.write_bytes(b'\x89PNG\r\n\x1a\n' + bytes([i]))
                frames.append({'file': str(p), 'time_s': i * 10.0, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()})
            analysis = {'evidence': {'kind': 'video-frames', 'frames': frames, 'motion_observable': True},
                        'analysis': {'base_frame': {'index': 1, 'sha256': frames[1]['sha256'], 'reason': 'Settled composition'}}}
            references = art_direction.prepare_references(analysis, base / 'out')
            self.assertEqual(frames[1]['sha256'], references[0]['sha256'])
            Path(frames[1]['file']).unlink()
            with self.assertRaises(ValueError):
                art_direction.prepare_references(analysis, base / 'other')

    def test_temporal_identity_keeps_order_and_timestamps(self):
        ev = {'kind': 'video-frames', 'motion_observable': True,
              'frames': [{'sha256': 'a' * 64, 'time_s': 1}, {'sha256': 'b' * 64, 'time_s': 2}]}
        first = profiles._evidence_identity(ev)
        swapped = {**ev, 'frames': list(reversed(ev['frames']))}
        retimed = {**ev, 'frames': [{**ev['frames'][0], 'time_s': 0}, ev['frames'][1]]}
        self.assertNotEqual(first, profiles._evidence_identity(swapped))
        self.assertNotEqual(first, profiles._evidence_identity(retimed))

    def test_different_representative_frame_cannot_reuse_the_same_design_profile(self):
        ev = {'kind': 'video-frames', 'motion_observable': True,
              'frames': [{'file': '/recorded/a.png', 'sha256': 'a' * 64, 'time_s': 1},
                         {'file': '/recorded/b.png', 'sha256': 'b' * 64, 'time_s': 2}]}
        first = {'schema_version': 2, 'content_sha256': 'c' * 64,
                 'evidence': ev, 'visual': {'summary': 'Same final summary'},
                 'analysis': {'base_frame': {'index': 0, 'sha256': 'a' * 64}}}
        second = {**first, 'analysis': {'base_frame': {'index': 1, 'sha256': 'b' * 64}}}
        profile_a = profiles.build({'id': 'c' * 64}, first, {}, {})
        profile_b = profiles.build({'id': 'c' * 64}, second, {}, {})
        self.assertNotEqual(profile_a['id'], profile_b['id'])


if __name__ == '__main__':
    unittest.main()
