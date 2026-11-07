import unittest
from unittest.mock import Mock, patch
import numpy as np

import draft_input


class DraftPasteTests(unittest.TestCase):
    def hits(self):
        return [{'text':'粘贴','score':.99,'box':[12,10,45,18]},
                {'text':'边写边译','score':.99,'box':[12,45,90,18]}]

    def test_only_exact_unique_independent_paste_votes(self):
        a=self.hits()
        self.assertEqual(draft_input.paste_target([a,a]),[12,10,45,18])
        for invalid in ([a], [a,a[:1]], [a,a+[a[0]]],
                        [a,[dict(h,score=.5) for h in a]],
                        [a,a+[{'text':'发送','score':.99,'box':[1,2,30,15]}]]):
            self.assertIsNone(draft_input.paste_target(invalid))

    def test_changed_menu_position_is_not_guessed(self):
        a=self.hits();b=[dict(h,box=[80,*h['box'][1:]]) for h in a]
        self.assertIsNone(draft_input.paste_target([a,b]))

    def run_paste(self,*,matches=(True,True),empty=True,menus=None,fresh_changed=False):
        runtime=Mock(hwnd=123);clip=Mock();clip.stage.return_value=42
        clip.staged_matches.side_effect=matches
        popup=Mock();frame=np.zeros((80,150,3),dtype=np.uint8)
        fresh=frame.copy()
        if fresh_changed:fresh[2,3]=1
        popup.capture.side_effect=[(frame,'test-menu.png'),(fresh,'test-preclick.png')]
        runtime.recognize_image.return_value=self.hits()
        windows=menus if menus is not None else [[],[{'class':'Qt51514QWindowToolSaveBits','title':'Weixin','hwnd':456}],[]]
        with patch.object(draft_input,'Clipboard',return_value=clip), \
             patch.object(draft_input,'unchanged'), \
             patch('send._draft',return_value={'verified':empty}), \
             patch('send._ocr_scaled',return_value=self.hits()), \
             patch('media_send.owned_windows',side_effect=windows), \
             patch('inactive_chats.RecentMenu',return_value=popup) as menu:
            try:result=draft_input.paste_draft(runtime,frame,'收到🙂𠮷')
            except ValueError as exc:result=str(exc)
        return result,runtime,clip,popup,menu

    def test_non_bmp_text_is_staged_intact_and_one_paste_no_send(self):
        result,runtime,clip,popup,_=self.run_paste()
        self.assertEqual(result['method'],'native_paste_menu')
        clip.stage.assert_called_once_with('收到🙂𠮷',123)
        popup.node.assert_called_once()
        self.assertEqual([c.args[0] for c in runtime.node.call_args_list],['CopyMessageMenu'])
        self.assertEqual([c.args[0] for c in popup.node.call_args_list],['CopyMessageSelect'])

    def test_existing_draft_is_never_overwritten(self):
        result,runtime,clip,popup,_=self.run_paste(empty=False)
        self.assertEqual(result,'PASTE_DRAFT_NOT_EMPTY')
        clip.stage.assert_not_called();runtime.node.assert_not_called();popup.node.assert_not_called()

    def test_clipboard_change_before_menu_stops_without_click(self):
        result,runtime,_,popup,_=self.run_paste(matches=(False,))
        self.assertEqual(result,'PASTE_CLIPBOARD_CHANGED')
        runtime.node.assert_not_called();popup.node.assert_not_called()

    def test_clipboard_change_before_paste_stops_without_paste(self):
        result,_,_,popup,_=self.run_paste(matches=(True,False))
        self.assertEqual(result,'PASTE_CLIPBOARD_CHANGED');popup.node.assert_not_called()

    def test_fresh_menu_changed_blocks_click(self):
        result,_,_,popup,_=self.run_paste(fresh_changed=True)
        self.assertEqual(result,'PASTE_MENU_CHANGED');popup.node.assert_not_called()

    def test_vanished_menu_never_escapes_main_window(self):
        result,runtime,_,popup,menu=self.run_paste(menus=[[],[],[]])
        self.assertEqual(result,'PASTE_MENU_NOT_UNIQUE')
        menu.assert_not_called();popup.escape.assert_not_called()
        self.assertEqual([c.args[0] for c in runtime.node.call_args_list],['CopyMessageMenu'])


if __name__=='__main__':unittest.main()
