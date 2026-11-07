import hashlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).parent))
import text_copy
from message_vision import extract_message_payloads

class NativeCopyTests(unittest.TestCase):
    def test_vanished_copy_menu_never_sends_escape_to_main_window(self):
        from unittest.mock import Mock
        runtime=Mock();clipboard=Mock();payload={'visual':{'box':[545,200,100,36]}}
        with patch.object(text_copy,'unchanged_message'), \
             patch.object(text_copy,'Clipboard',return_value=clipboard), \
             patch('media_send.owned_windows',side_effect=[[],[],[]]), \
             patch('inactive_chats.RecentMenu') as menu:
            with self.assertRaisesRegex(ValueError,'COPY_MENU_NOT_UNIQUE'):
                text_copy.copy_bubble(runtime,self.image,payload,'test')
        self.assertEqual([call.args[0] for call in runtime.node.call_args_list],['CopyMessageMenu'])
        menu.assert_not_called()

    def test_late_copy_menu_cleanup_is_scoped_to_its_own_window(self):
        from unittest.mock import Mock
        runtime=Mock();clipboard=Mock();payload={'visual':{'box':[545,200,100,36]}}
        row={'hwnd':456,'class':'Qt51514QWindowToolSaveBits','title':'Weixin'}
        with patch.object(text_copy,'unchanged_message'), \
             patch.object(text_copy,'Clipboard',return_value=clipboard), \
             patch('media_send.owned_windows',side_effect=[[],[],[row]]), \
             patch('inactive_chats.RecentMenu') as menu:
            with self.assertRaisesRegex(ValueError,'COPY_MENU_NOT_UNIQUE'):
                text_copy.copy_bubble(runtime,self.image,payload,'test')
        menu.assert_called_once_with(runtime,row)
        menu.return_value.escape.assert_called_once()
        self.assertEqual([call.args[0] for call in runtime.node.call_args_list],['CopyMessageMenu'])

    def test_ambiguous_copy_menu_cleanup_never_inputs(self):
        from unittest.mock import Mock
        runtime=Mock();clipboard=Mock();payload={'visual':{'box':[545,200,100,36]}}
        rows=[{'hwnd':456,'class':'Qt51514QWindowToolSaveBits','title':'Weixin'},
              {'hwnd':789,'class':'Qt51514QWindowToolSaveBits','title':'Weixin'}]
        with patch.object(text_copy,'unchanged_message'), \
             patch.object(text_copy,'Clipboard',return_value=clipboard), \
             patch('media_send.owned_windows',side_effect=[[],rows,rows]), \
             patch('inactive_chats.RecentMenu') as menu:
            with self.assertRaisesRegex(ValueError,'COPY_MENU_NOT_UNIQUE'):
                text_copy.copy_bubble(runtime,self.image,payload,'test')
        menu.assert_not_called()
        self.assertEqual([call.args[0] for call in runtime.node.call_args_list],['CopyMessageMenu'])

    def setUp(self):
        self.image=np.full((1040,1920,3),245,dtype=np.uint8)
        self.image[200:550,545:1300]=255
        self.image[230:240,580:1200]=0
        self.image[610:646,1770:1844]=[100,220,150] # BGR green
    def test_geometry_finds_long_and_short_without_body_ocr(self):
        bubbles,seeds=text_copy.bubble_seeds(self.image)
        self.assertEqual([b['direction'] for b in bubbles],['incoming','outgoing'])
        self.assertEqual(bubbles[0]['box'],[545,200,755,350])
        self.assertEqual(len(seeds),2)
    def test_clipped_body_is_masked_without_copy_seed(self):
        image=np.full((1040,1920,3),245,dtype=np.uint8)
        image[40:160,545:1300]=255
        bubbles,seeds=text_copy.bubble_seeds(image)
        self.assertEqual(seeds,[])
        self.assertTrue(bubbles[0]['partial'])
    def test_masks_body_on_every_ocr_pass(self):
        class Runtime:
            def recognize_image(inner,name,image,**kwargs):
                self.assertEqual(np.max(image[200*(image.shape[0]//1040):550*(image.shape[0]//1040),545*(image.shape[0]//1040):1300*(image.shape[0]//1040)]),245)
                return []
        text_copy.metadata_hits(Runtime(),self.image)
    def test_long_unicode_whitespace_verbatim(self):
        text='  /生图 电影级\t8K\r\n不拆词😀\n'+'原文，标点。'*600+'  '
        proof={'verified':True,'characters':len(text),'sha256':hashlib.sha256(text.encode()).hexdigest()}
        with patch.object(text_copy,'copy_bubble',return_value=(text,proof)):
            payloads,_=text_copy.read_messages(type('R',(),{'recognize_image':lambda *a,**kw:[]})(),self.image)
        bodies=[p for p in payloads if p['message_kind']=='text']
        self.assertTrue(bodies)
        self.assertEqual(bodies[0]['text'],text)
        self.assertEqual(bodies[0]['source'],'maa_native_clipboard')
        self.assertEqual(bodies[0]['ocr']['raw_hits'],[])
    def test_failure_never_keeps_seed_or_ocr_body(self):
        with patch.object(text_copy,'copy_bubble',side_effect=ValueError('COPY_CLIPBOARD_NOT_CHANGED')):
            payloads,_=text_copy.read_messages(type('R',(),{'recognize_image':lambda *a,**kw:[]})(),self.image)
        for p in payloads:
            if p['message_kind']=='text':
                self.assertEqual(p['text'],'');self.assertFalse(p['reply_safe']);self.assertFalse(p['ocr']['stable'])
    def test_text_only_menu_exact_copy_two_votes(self):
        hits=[{'text':'复制','score':.99,'box':[50,20,40,20]},{'text':'放大阅读','score':.99,'box':[50,60,90,20]}]
        self.assertEqual(text_copy.menu_copy_target([hits,hits]),[50,20,40,20])
        self.assertIsNone(text_copy.menu_copy_target([hits[:1],hits[:1]]))
        self.assertIsNone(text_copy.menu_copy_target([hits,hits+[hits[0]]]))
        low=[dict(h,score=.6) for h in hits]
        self.assertEqual(text_copy.menu_copy_target([hits,low,hits]),[50,20,40,20])
        self.assertIsNone(text_copy.menu_copy_target([hits,low,low]))
    def test_frame_change_blocks_wrong_bubble(self):
        im=self.image.copy();im[250,600]=0
        with self.assertRaisesRegex(ValueError,'FRAME_CHANGED'):
            text_copy.unchanged(type('R',(),{'capture_image':lambda _:im})(),self.image)

    def test_message_copy_allows_unrelated_new_pixels_but_protects_bubble_avatar_title(self):
        payload={'visual':{'box':[545,200,755,350], 'avatar_verified':True,'avatar_box':[495,200,36,36]}}
        current=self.image.copy();current[850,1800]=0
        runtime=type('R',(),{'capture_image':lambda _:current})()
        text_copy.unchanged_message(runtime,self.image,payload)
        for y,x in ((250,600),(210,500),(50,600)):
            current=self.image.copy();current[y,x]=0
            with self.subTest(y=y,x=x),self.assertRaisesRegex(ValueError,'FRAME_CHANGED'):
                text_copy.unchanged_message(runtime,self.image,payload)

    def test_cached_exact_occurrences_avoid_recopies_but_new_tail_still_copies(self):
        from copy import deepcopy
        def payload(text,sha,y):
            return {'text':text,'direction':'incoming','message_kind':'text','reply_safe':True,
                    'text_copy':{'verified':True,'method':'native_context_menu'},
                    'visual':{'box':[545,y,100,36],'verified':True,'sha256':sha*64}}
        previous=[payload('first','a',120),payload('second','b',180)]
        current=deepcopy(previous)+[payload('fresh','c',240)]
        runtime=type('R',(),{})()
        with patch.object(text_copy,'metadata_hits',return_value=([],[])), \
             patch.object(text_copy,'extract_message_payloads',return_value=current), \
             patch.object(text_copy,'copy_bubble',return_value=('native-fresh',{'verified':True,'method':'native_context_menu'})) as copy:
            values,_=text_copy.read_messages(runtime,self.image,previous_payloads=previous)
        self.assertEqual(copy.call_count,1)
        self.assertEqual([p['text'] for p in values],['first','second','native-fresh'])
        self.assertTrue(values[0]['text_copy']['reused_verified_occurrence'])

    def test_complete_right_avatar_is_protected_outside_body_roi(self):
        payload={'visual':{'box':[1792,828,56,36], 'avatar_verified':True,
                           'avatar_box':[1858,828,36,36]}}
        current=self.image.copy()
        runtime=type('R',(),{'capture_image':lambda _:current})()
        text_copy.unchanged_message(runtime,self.image,payload)
        for y,x in ((840,1885),(840,1893),(840,1895),(840,1792)):
            current=self.image.copy();current[y,x]=0
            with self.subTest(y=y,x=x),self.assertRaisesRegex(ValueError,'FRAME_CHANGED'):
                text_copy.unchanged_message(runtime,self.image,payload)
        # The avatar fix does not authorize bodies outside their original ROI.
        payload['visual']['box']=[1858,828,36,36]
        with self.assertRaisesRegex(ValueError,'TARGET_NOT_VERIFIED'):
            text_copy.unchanged_message(runtime,self.image,payload)

if __name__=='__main__':unittest.main()


class NativeDraftTests(unittest.TestCase):
    def test_new_incoming_during_native_copy_is_not_a_draft_change(self):
        from unittest.mock import patch, MagicMock
        image=np.full((1040,1920,3),255,dtype=np.uint8);current=image.copy()
        runtime=MagicMock();runtime.hwnd=42
        runtime.recognize_image.return_value=[{'box':[61,211,9,65]}]
        runtime.capture_image.side_effect=lambda:current
        clipboard=MagicMock();clipboard.read.side_effect=['完整帮助文字','完整帮助文字']
        def arrive(*args):current[300:340,545:800]=0
        with patch.object(text_copy,'Clipboard',return_value=clipboard),patch.object(text_copy,'_draft_copy_keys',side_effect=arrive):
            self.assertTrue(text_copy.native_draft(runtime,image,'完整帮助文字')['verified'])

    def test_native_draft_keeps_title_selected_strip_avatar_and_name_bound(self):
        image=np.full((1040,1920,3),255,dtype=np.uint8)
        class Runtime:
            def recognize_image(self,*args):return [{'box':[61,211,9,65]}]
            def capture_image(self):return current
        runtime=Runtime();regions=text_copy.draft_recipient_regions(runtime,image)
        for y,x in [(40,600),(220,65),(240,80),(225,130)]:
            current=image.copy();current[y,x]=0
            with self.subTest(y=y,x=x),self.assertRaisesRegex(ValueError,'DRAFT_RECIPIENT_CHANGED'):
                text_copy.unchanged_draft_recipient(runtime,image,regions)

    def test_native_draft_requires_one_complete_selected_row_before_any_copy(self):
        from unittest.mock import MagicMock,patch
        image=np.zeros((1040,1920,3),dtype=np.uint8);runtime=MagicMock()
        for boxes in [[],[{'box':[61,211,9,65]},{'box':[61,280,9,65]}],[{'box':[61,990,9,65]}]]:
            runtime.recognize_image.return_value=boxes
            with self.subTest(boxes=boxes),patch.object(text_copy,'_draft_copy_keys') as keys,self.assertRaises(ValueError):
                text_copy.native_draft(runtime,image,'text')
            keys.assert_not_called()

    def test_exact_copy_and_release(self):
        from unittest.mock import patch, MagicMock
        runtime=MagicMock();runtime.hwnd=42
        image=np.zeros((1040,1920,3),dtype=np.uint8)
        clipboard=MagicMock();clipboard.read.side_effect=['报价：4K。','报价：4K。']
        with patch.object(text_copy,'Clipboard',return_value=clipboard), patch.object(text_copy,'draft_recipient_regions',return_value=[]), patch.object(text_copy,'unchanged_draft_recipient'), patch.object(text_copy,'_draft_copy_keys'):
            result=text_copy.native_draft(runtime,image,'报价：4K。')
        self.assertTrue(result['verified']);self.assertEqual(result['reads'],2)
        self.assertEqual(runtime.node.call_args_list[-1].args[0],'FocusChatInput')

    def test_one_mismatching_native_read_blocks(self):
        from unittest.mock import patch, MagicMock
        runtime=MagicMock();runtime.hwnd=42
        clipboard=MagicMock();clipboard.read.side_effect=['报价：4K。','报价：2K。']
        with patch.object(text_copy,'Clipboard',return_value=clipboard), patch.object(text_copy,'draft_recipient_regions',return_value=[]), patch.object(text_copy,'unchanged_draft_recipient'), patch.object(text_copy,'_draft_copy_keys'):
            self.assertFalse(text_copy.native_draft(runtime,np.zeros((1,1,3)), '报价：4K。')['verified'])

    def test_failed_copy_releases_control_and_never_sends(self):
        from unittest.mock import patch, MagicMock
        runtime=MagicMock();runtime.hwnd=42
        clipboard=MagicMock();clipboard.read.side_effect=ValueError('COPY_CLIPBOARD_OWNER_MISMATCH')
        with patch.object(text_copy,'Clipboard',return_value=clipboard), patch.object(text_copy,'draft_recipient_regions',return_value=[]), patch.object(text_copy,'unchanged_draft_recipient'), patch.object(text_copy,'_draft_copy_keys'):
            with self.assertRaises(ValueError):text_copy.native_draft(runtime,np.zeros((1,1,3)), '报价')
        self.assertEqual([c.args[0] for c in runtime.node.call_args_list],['FocusChatInput'])


class NativeDraftRowMovement(unittest.TestCase):
 def test_exact_selected_row_can_move_while_time_and_preview_change(self):
  from unittest.mock import Mock
  a=np.full((1040,1920,3),255,dtype=np.uint8)
  rng=np.random.default_rng(15);a[341:406,60:458]=rng.integers(0,256,(65,398,3),dtype=np.uint8)
  b=a.copy();b[341:406,60:458]=255;b[211:276,60:458]=a[341:406,60:458]
  b[220:235,400:450]=0;b[255:270,112:400]=0;b[300:340,545:800]=0
  runtime=Mock();runtime.capture_image.return_value=b
  runtime.recognize_image.side_effect=lambda name,img:[{'box':[61,341 if img is a else 211,9,65]}]
  regions=text_copy.draft_recipient_regions(runtime,a)
  text_copy.unchanged_draft_recipient(runtime,a,regions)
  for y,x in [(40,600),(220,65),(240,80),(225,130)]:
   changed=b.copy();changed[y,x]^=1;runtime.capture_image.return_value=changed
   with self.subTest(y=y,x=x),self.assertRaisesRegex(ValueError,'DRAFT_RECIPIENT_CHANGED'):
    text_copy.unchanged_draft_recipient(runtime,a,regions)

 def test_ambiguous_or_malformed_selected_row_during_copy_still_blocks(self):
  from unittest.mock import Mock
  a=np.full((1040,1920,3),255,dtype=np.uint8);runtime=Mock();runtime.capture_image.return_value=a.copy()
  runtime.recognize_image.return_value=[{'box':[61,211,9,65]}]
  regions=text_copy.draft_recipient_regions(runtime,a)
  for boxes in [[],[{'box':[61,211,9,65]},{'box':[61,341,9,65]}],[{'box':[61,990,9,65]}]]:
   runtime.recognize_image.return_value=boxes
   with self.subTest(boxes=boxes),self.assertRaises(ValueError):text_copy.unchanged_draft_recipient(runtime,a,regions)
