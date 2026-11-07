import unittest
import numpy as np
try:
    from maa_wechat.layout import check_frame
except ModuleNotFoundError:
    from layout import check_frame


class LayoutTests(unittest.TestCase):
    def image(self,x=480):
        image=np.full((1040,1920,3),245,dtype=np.uint8)
        image[:,:x]=230
        return image
    def test_exact_layout(self): self.assertLessEqual(abs(check_frame(self.image())-480),1)
    def test_scrollbar_does_not_move_divider(self):
        image=self.image()
        image[:,470:478]=166
        image[:,478:480]=[240,238,238]
        image[:,480:]=250
        self.assertEqual(check_frame(image),480)
    def test_shift_rejected(self):
        with self.assertRaises(ValueError): check_frame(self.image(420))
    def test_blank_rejected(self):
        with self.assertRaises(ValueError): check_frame(np.zeros((1040,1920,3),dtype=np.uint8))
    def test_wrong_dimensions_rejected(self):
        with self.assertRaises(ValueError): check_frame(np.zeros((1080,1920,3),dtype=np.uint8))

class NavigationRecoveryTests(unittest.TestCase):
    def test_discover_only(self):
        from layout import navigation_entry_safe
        image=np.full((1040,1920,3),227,np.uint8)
        hits=[{'text':'发现','box':[75,50,30,18]}]
        self.assertTrue(navigation_entry_safe(image,hits))
        self.assertFalse(navigation_entry_safe(image,[{'text':'登录','box':[75,50,30,18]}]))
        self.assertFalse(navigation_entry_safe(image[:1000],hits))
