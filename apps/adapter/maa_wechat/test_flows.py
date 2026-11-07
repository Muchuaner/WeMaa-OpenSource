import unittest
import tempfile
from pathlib import Path
import numpy as np
from PIL import Image
from flows import title_matches,reuse_visual,scroll_top
from unittest.mock import Mock

class ScrollTopTests(unittest.TestCase):
    def runtime(self,frames,top):
        runtime=Mock()
        runtime.capture.side_effect=[(frame,'frame.png') for frame in frames]
        runtime.recognize_image.side_effect=top
        return runtime

    def frame(self,value=0):
        return np.full((1040,1920,3),value,dtype=np.uint8)

    def test_verified_top_does_not_scroll(self):
        runtime=self.runtime([self.frame()],[True])
        scroll_top(runtime,'test')
        runtime.node.assert_not_called()

    def test_two_unchanged_frames_stop_scrolling(self):
        runtime=self.runtime([self.frame()]*3,[False]*3)
        scroll_top(runtime,'test')
        self.assertEqual(runtime.node.call_count,2)

    def test_changed_frame_resets_stability(self):
        runtime=self.runtime([self.frame(v) for v in (0,0,1,1,1)],[False]*5)
        scroll_top(runtime,'test')
        self.assertEqual(runtime.node.call_count,4)

    def test_unverified_top_stops_at_bounded_budget(self):
        runtime=self.runtime([self.frame(v) for v in range(16)],[False]*16)
        with self.assertRaisesRegex(RuntimeError,'LIST_TOP_NOT_VERIFIED'):
            scroll_top(runtime,'test')
        self.assertEqual(runtime.node.call_count,15)

    def test_large_contact_list_can_use_verified_extended_budget(self):
        runtime=self.runtime([self.frame(v) for v in range(21)],[False]*20+[True])
        scroll_top(runtime,'test',max_batches=80)
        self.assertEqual(runtime.node.call_count,20)

    def test_verified_top_interrupts_next_batch(self):
        runtime=self.runtime([self.frame(0),self.frame(1)],[False,True])
        scroll_top(runtime,'test')
        self.assertEqual(runtime.node.call_count,1)


class TitleTests(unittest.TestCase):
    def test_group_member_count(self): self.assertTrue(title_matches('开发组','开发组（455）'))
    def test_ocr_spacing(self): self.assertTrue(title_matches('开发组@Tom','开发组 @Tom'))
    def test_short_name_requires_exact_base(self):
        self.assertFalse(title_matches('开发组','开发组二（455）'))  # short names never absorb extra characters
        self.assertFalse(title_matches('开发组','开发测试组（455）'))
    def test_empty(self): self.assertFalse(title_matches('',''))
    def test_long_title_requires_clipping_evidence(self):
        self.assertFalse(title_matches('abcdefgh','abcdefghijkl'))
        self.assertTrue(title_matches('abcdefgh','abcdefghijkl',True))
        self.assertFalse(title_matches('abc','abcdef',True))
    def test_reuse_exact_pixels_with_center_jitter(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'data'/'avatars').mkdir(parents=True)
            prior=np.random.default_rng(7).integers(0,256,(24,24,3),dtype=np.uint8)
            Image.fromarray(prior[:,:,::-1]).save(root/'data'/'avatars'/'sha.png')
            image=np.zeros((200,200,3),dtype=np.uint8)
            image[88:112,99:123]=prior
            self.assertEqual(reuse_visual('name',image,101,{('name','sha'):5},root),5)
            self.assertIsNone(reuse_visual('other',image,101,{('name','sha'):5},root))
            image[100,110]=[0,0,0]
            self.assertIsNone(reuse_visual('name',image,101,{('name','sha'):5},root))
