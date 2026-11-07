import unittest
import numpy as np
from send import _placeholder_reference,_placeholder_pixels,_draft,INPUT_ROI

class HintRuntime:
    def __init__(self,text='按住鼠标 语音输入文字'): self.text=text
    def recognize_image(self,*args,**kw): return [{'text':self.text,'box':[502,912,146,16],'score':.99}]

class PlaceholderTests(unittest.TestCase):
    def image(self):
        image=np.full((1040,1920,3),250,dtype=np.uint8)
        x,y,w,h=INPUT_ROI;image[y:y+h,x:x+w]=_placeholder_reference()
        return image
    def test_calibrated_placeholder_is_empty_with_two_matching_ocr_passes(self):
        image=self.image();self.assertTrue(_placeholder_pixels(image));self.assertTrue(_draft(HintRuntime(),image)['verified'])
    def test_added_text_or_image_anywhere_in_input_remains_blocked(self):
        for x,y in [(700,918),(600,965),(1600,950)]:
            image=self.image();image[y:y+8,x:x+15]=30
            self.assertFalse(_draft(HintRuntime(),image)['verified'])
    def test_typed_identical_words_are_not_a_placeholder(self):
        image=self.image();image[916:922,520:590]=30
        self.assertFalse(_draft(HintRuntime(),image)['verified'])
    def test_unknown_or_missing_ocr_cannot_exempt_pixels(self):
        for text in ('','按住鼠标语音输入文字你好','按住鼠标语音输入文宇'):
            self.assertFalse(_draft(HintRuntime(text),self.image())['verified'])
    def test_expected_message_does_not_use_empty_placeholder_exception(self):
        self.assertFalse(_draft(HintRuntime(),self.image(),'实际待发送内容')['verified'])
