import unittest
import numpy as np
try:
    from maa_wechat.list_rows import avatar_centers
except ModuleNotFoundError:
    from list_rows import avatar_centers


class RowsTests(unittest.TestCase):
    def test_contacts_include_emoji_without_ocr(self):
        image=np.full((1040,1920,3),238,dtype=np.uint8)
        for y in (340,394,448): image[y-15:y+15,96:126]=[20,50,160]
        self.assertEqual(avatar_centers(image,'contacts'),[340,394,448])
    def test_recent_scroll_offset(self):
        image=np.full((1040,1920,3),230,dtype=np.uint8)
        for y in (142,207,272): image[y-18:y+18,74:110]=[20,50,160]
        self.assertEqual(avatar_centers(image,'recent'),[142,207,272])
    def test_blank(self):
        self.assertEqual(avatar_centers(np.full((1040,1920,3),230,dtype=np.uint8),'recent'),[])
