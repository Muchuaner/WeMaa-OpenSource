"""Regression for a green avatar falsely matching a selected chat row.

Native tests replay the saved failure frame, without a desktop controller or
GUI actions. Optional native/fixture coverage is explicitly skipped on macOS.
"""
import hashlib
import json
from pathlib import Path
import unittest

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent
PIPELINE = ROOT / 'resource/pipeline/navigation.json'
FRAME = 'maa-live-reply-idle-final-sep26-send-3b98c1eddb9cbde3-before.png'


class SelectedRowConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.node = json.loads(PIPELINE.read_text(encoding='utf-8'))['ContactSelectedRow']

    def test_roi_ends_before_avatar_left_edge(self):
        x, y, width, height = self.node['roi']
        self.assertEqual([x, y, width, height], [61, 80, 9, 950])
        self.assertLess(x + width, 72)

    def test_color_and_unique_component_protection_not_relaxed(self):
        self.assertEqual(self.node['lower'], [0, 100, 35])
        self.assertEqual(self.node['upper'], [90, 230, 170])
        self.assertEqual(self.node['count'], 100)
        self.assertIs(self.node['connected'], True)
        self.assertEqual(self.node['action'], 'DoNothing')


class NativeSelectedRowFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from runner import Runtime
            from maa.resource import Resource
            from maa.tasker import Tasker
            from maa.library import Library
            from list_rows import ListRows, VerifyRecentRow
        except (ImportError, OSError) as exc:
            raise unittest.SkipTest('native Maa runtime unavailable') from exc
        fixture = ROOT / 'data' / FRAME
        if not fixture.exists():
            fixture = ROOT.parent / 'artifacts/maa-recovery-sep26/reply-failure-tom-before.png'
        if not fixture.exists():
            raise unittest.SkipTest('saved Tom failure frame unavailable')
        cls.image = np.asarray(Image.open(fixture).convert('RGB'))[:, :, ::-1].copy()
        assert hashlib.sha256(cls.image.tobytes()).hexdigest() == 'e909e695d571188d7a3e8896648868319d0b682bb61bb63fcb4811de3bddce66'
        cls.runtime = Runtime.__new__(Runtime)
        cls.runtime.resource = Resource()
        cls.runtime.resource.register_custom_recognition('ListRows', ListRows())
        cls.runtime.resource.register_custom_recognition('VerifyRecentRow', VerifyRecentRow())
        assert cls.runtime.resource.post_bundle(str(ROOT / 'resource')).wait().succeeded
        cls.runtime.tasker = Tasker()
        # Same resource binding as Tasker.bind, deliberately no GUI controller.
        assert Library.framework().MaaTaskerBindResource(cls.runtime.tasker._handle, cls.runtime.resource._handle)
        cls.runtime.pipeline = json.loads(PIPELINE.read_text(encoding='utf-8'))

    def boxes(self, image=None, width=9):
        node = self.runtime.pipeline['ContactSelectedRow']
        saved = node['roi']
        node['roi'] = [61, 80, width, 950]
        try:
            return [h['box'] for h in self.runtime.recognize_image('ContactSelectedRow', self.image if image is None else image)]
        finally:
            node['roi'] = saved

    def request(self):
        return dict(name='Demo User', conversation_title='Demo User', row_y=373,
                    avatar_sha=hashlib.sha256(self.image[360:386, 78:102, :3].tobytes()).hexdigest())

    def test_original_roi_reproduces_false_avatar_component(self):
        self.assertEqual(self.boxes(width=18), [[61, 341, 18, 65], [72, 420, 7, 36]])

    def test_fixed_roi_recognizes_only_actual_selected_row(self):
        self.assertEqual(self.boxes(), [[61, 341, 9, 65]])

    def test_green_avatar_without_selected_background_cannot_select(self):
        frame = self.image.copy()
        frame[341:406, 61:70] = self.image[438, 65]
        self.assertEqual(self.boxes(frame), [])

    def test_sender_accepts_correct_title_name_avatar_and_row(self):
        from send import _target
        target = _target(self.runtime, self.image, self.request())
        self.assertEqual(target['selected_centers'], [373])
        self.assertIs(target['verified'], True)

    def test_sender_still_rejects_wrong_title_or_avatar(self):
        from send import _target
        for request in (dict(self.request(), conversation_title='Other'),
                        dict(self.request(), avatar_sha='0' * 64)):
            self.assertIs(_target(self.runtime, self.image, request)['verified'], False)

    def test_two_selected_backgrounds_still_fail_closed(self):
        from send import _target
        frame = self.image.copy()
        frame[420:456, 61:70] = self.image[373, 65]
        self.assertEqual(len(self.boxes(frame)), 2)
        self.assertIs(_target(self.runtime, frame, self.request())['verified'], False)
