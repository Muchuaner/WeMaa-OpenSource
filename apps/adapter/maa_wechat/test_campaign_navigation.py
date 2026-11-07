import unittest
from unittest.mock import patch

from watch import read_title
from manual_gui import find_recent
from pathlib import Path
from types import SimpleNamespace
import time


def hit(text):
    return {'text': text, 'box': [490, 35, 180, 20]}


class HeaderRuntime:
    def __init__(self, title):
        self.title = title

    def recognize_image(self, name, image, **kwargs):
        return [] if name == 'ChatTitleBlueBadge' else [hit(self.title)]


class CampaignNavigationTests(unittest.TestCase):
    def test_partial_header_uses_two_matching_complete_observations(self):
        raw = '创作平台AI-内测001 (54'
        full = raw + ')'
        with patch('send._ocr_scaled', side_effect=[[hit(full)], [hit(full)]]) as reread:
            self.assertEqual(read_title(HeaderRuntime(raw), None, {'name': '创作平台AI-内测001'}), full)
            self.assertEqual(reread.call_count, 2)

    def test_count_disagreement_preserves_unverified_raw_header(self):
        raw = '创作平台AI-内测001 (54'
        with patch('send._ocr_scaled', side_effect=[[hit(raw + ')')], [hit('创作平台AI-内测001 (55)')]]):
            self.assertEqual(read_title(HeaderRuntime(raw), None, {'name': '创作平台AI-内测001'}), raw)

    def test_literal_private_nickname_is_not_promoted_to_group(self):
        raw = 'Tom(10)'
        with patch('send._ocr_scaled') as reread:
            self.assertEqual(read_title(HeaderRuntime(raw), None, {'name': raw}), raw)
            reread.assert_not_called()

    def test_different_chat_is_still_rejected(self):
        with patch('send._ocr_scaled', return_value=[hit('Other')]):
            with self.assertRaisesRegex(ValueError, 'CHAT_TITLE_MISMATCH'):
                read_title(HeaderRuntime('Other'), None, {'name': '创作平台AI-内测001'})

    def test_guard_miss_rereads_current_row_before_navigation(self):
        before = {'name': '创作平台AI-内测001', 'avatar_sha': 'a', 'y': 628}
        after = {**before, 'y': 634}
        runtime = SimpleNamespace(node=lambda *args: None, selection_failure=None)
        target = {**before, 'conversation_title': '创作平台AI-内测001(54)'}
        with patch('manual_gui.scroll_top'), patch('watch.read_rows', side_effect=[([before], None, None), ([after], None, None)]), patch('watch.select', side_effect=[False, True]) as select:
            row, page = find_recent(runtime, Path('.'), target, 'test', time.time()+60)
            self.assertEqual(row['y'], 634)
            self.assertEqual(select.call_args.args[1]['y'], 634)

    def test_changed_private_avatar_cannot_pass_refresh(self):
        before = {'name': 'Demo User', 'avatar_sha': 'a', 'y': 200}
        after = {**before, 'avatar_sha': 'b'}
        runtime = SimpleNamespace(node=lambda *args: None, selection_failure=None)
        with patch('manual_gui.scroll_top'), patch('watch.read_rows', side_effect=[([before], None, None), ([after], None, None)]), patch('watch.select', return_value=False) as select:
            with self.assertRaisesRegex(ValueError, 'TARGET_CHANGED'):
                find_recent(runtime, Path('.'), before, 'test', time.time()+60)
            self.assertEqual(select.call_count, 1)

    def test_wrong_opened_title_is_not_retried(self):
        row = {'name': 'Demo User', 'avatar_sha': 'a', 'y': 200}
        runtime = SimpleNamespace(node=lambda *args: None, selection_failure={'reason': 'CHAT_TITLE_MISMATCH'})
        with patch('manual_gui.scroll_top'), patch('watch.read_rows', return_value=([row], None, None)) as read, patch('watch.select', return_value=False):
            with self.assertRaisesRegex(ValueError, 'TARGET_CHANGED'):
                find_recent(runtime, Path('.'), row, 'test', time.time()+60)
            self.assertEqual(read.call_count, 1)


if __name__ == '__main__':
    unittest.main()
