import unittest
from unittest.mock import Mock

import numpy as np

from storage_dialog import dismiss_position, recover_storage_dialog
from window_visibility import WECHAT_EXE, WECHAT_CLASS, DISPLAY_PROFILE


def hits():
    return [
        {'text': '存储空间不足', 'score': .99, 'box': [116, 124, 98, 20]},
        {'text': 'C盘存储空间不足，为避免影响正常', 'score': .99, 'box': [56, 156, 220, 18]},
        {'text': '使用微信，请及时清理存储空间。', 'score': .99, 'box': [60, 176, 210, 18]},
        {'text': '管理存储空间', 'score': .99, 'box': [122, 222, 86, 18]},
        {'text': '今日不再提醒', 'score': .99, 'box': [122, 268, 86, 18]},
    ]


class StorageDialogTests(unittest.TestCase):
    def setUp(self):
        self.main = {'hwnd': 10, 'pid': 50, 'exe': WECHAT_EXE, 'class': WECHAT_CLASS,
                     'client': [1920, 1040], 'visible': True, 'minimized': False,
                     'owner': 0, 'enabled': False}
        self.dialog = dict(self.main, hwnd=20, client=[330, 348], owner=10, enabled=True)
        self.after = dict(self.main, enabled=True)
        self.api = Mock()
        self.api.windows.side_effect = [[self.main, self.dialog], [self.main, self.dialog],
                                       [self.after, dict(self.dialog, visible=False)]]
        self.api.inspect.side_effect = [self.main, self.dialog, self.after]
        self.api.display_profile.return_value = DISPLAY_PROFILE
        self.reader = Mock()
        self.frame = np.zeros((348, 330, 3), dtype=np.uint8)
        self.reader.observe.side_effect = [(self.frame.copy(), hits()), (self.frame.copy(), hits())]
        self.probe = Mock(return_value={'available': True})

    def recover(self, **kwargs):
        return recover_storage_dialog(api=self.api, reader=self.reader, probe=self.probe,
                                      sleep=Mock(), **kwargs)

    def test_verified_modal_clicks_its_own_dismiss_button_once_and_checks_enabled_main(self):
        result = self.recover(expected_window=self.main)
        self.assertTrue(result['restored'])
        self.assertTrue(result['storage_dialog_dismissed'])
        self.reader.click.assert_called_once_with(165, 277)
        self.assertEqual(result['main'], self.after)

    def test_no_modal_does_not_capture_or_click(self):
        self.api.windows.side_effect = [[self.after]]
        self.assertFalse(self.recover()['restored'])
        self.reader.observe.assert_not_called()
        self.reader.click.assert_not_called()

    def test_disabled_main_without_known_modal_blocks(self):
        self.api.windows.side_effect = [[self.main]]
        with self.assertRaisesRegex(RuntimeError, 'MAIN_WINDOW_DISABLED'):
            self.recover()
        self.reader.click.assert_not_called()

    def test_wrong_owner_process_geometry_or_enabled_state_never_clicks(self):
        for changes in [{'owner': 99}, {'pid': 99}, {'client': [330, 350]},
                        {'enabled': False}, {'visible': False}, {'minimized': True},
                        {'exe': r'C:\Temp\Weixin.exe'}]:
            with self.subTest(changes=changes):
                self.api.windows.side_effect = [[self.main, dict(self.dialog, **changes)]]
                with self.assertRaises(RuntimeError):
                    self.recover()
        self.reader.click.assert_not_called()

    def test_ambiguous_main_or_extra_windows_never_click(self):
        self.api.windows.side_effect = [[self.main, self.dialog, dict(self.dialog, hwnd=30)]]
        with self.assertRaises(RuntimeError):
            self.recover()
        self.reader.click.assert_not_called()

    def test_reused_main_or_changed_display_never_clicks(self):
        with self.assertRaisesRegex(RuntimeError, 'IDENTITY_CHANGED'):
            self.recover(expected_window=dict(self.main, pid=60))
        self.reader.click.assert_not_called()
        self.api.windows.side_effect = [[self.main, self.dialog]]
        self.api.display_profile.return_value = dict(DISPLAY_PROFILE, dpi=120)
        with self.assertRaisesRegex(RuntimeError, 'DISPLAY_NOT_CALIBRATED'):
            self.recover()
        self.reader.click.assert_not_called()

    def test_locked_desktop_never_observes_or_clicks(self):
        self.probe.return_value = {'available': False}
        with self.assertRaisesRegex(RuntimeError, 'INPUT_DESKTOP_UNAVAILABLE'):
            self.recover()
        self.reader.observe.assert_not_called()
        self.reader.click.assert_not_called()

    def test_lock_between_confirmation_and_click_blocks(self):
        self.probe.side_effect = [{'available': True}, {'available': False}]
        with self.assertRaisesRegex(RuntimeError, 'INPUT_DESKTOP_UNAVAILABLE'):
            self.recover()
        self.reader.click.assert_not_called()

    def test_two_fresh_frames_must_agree(self):
        changed = self.frame.copy();changed[270, 150, 0] = 255
        self.reader.observe.side_effect = [(self.frame, hits()), (changed, hits())]
        with self.assertRaises(RuntimeError):
            self.recover()
        self.reader.click.assert_not_called()

    def test_fresh_inventory_or_ownership_change_blocks(self):
        self.api.windows.side_effect = [[self.main, self.dialog], [self.main, dict(self.dialog, pid=80)]]
        with self.assertRaises(RuntimeError):
            self.recover()
        self.reader.click.assert_not_called()

    def test_handle_reuse_after_inventory_blocks(self):
        self.api.inspect.side_effect = [self.main, dict(self.dialog, owner=99)]
        with self.assertRaises(RuntimeError):
            self.recover()
        self.reader.click.assert_not_called()

    def test_button_must_not_move_between_ocr_frames(self):
        moved = hits();moved[-1]['box'][0] += 2
        self.reader.observe.side_effect = [(self.frame, hits()), (self.frame, moved)]
        with self.assertRaises(RuntimeError):
            self.recover()
        self.reader.click.assert_not_called()

    def test_failed_dismissal_does_not_report_recovery_or_click_again(self):
        self.api.windows.side_effect = None;self.api.windows.return_value = [self.main, self.dialog]
        self.api.inspect.side_effect = [self.main, self.dialog]+[self.main]*50
        with self.assertRaisesRegex(RuntimeError, 'DISMISS_NOT_VERIFIED'):
            self.recover()
        self.reader.click.assert_called_once()

    def test_slow_modal_release_waits_without_clicking_twice(self):
        self.api.windows.side_effect = [[self.main, self.dialog]]*14+[[self.after]]
        self.api.inspect.side_effect = [self.main, self.dialog]+[self.main]*12+[self.after]
        self.assertTrue(self.recover()['storage_dialog_dismissed'])
        self.reader.click.assert_called_once()

    def test_missing_changed_ambiguous_or_low_confidence_text_is_rejected(self):
        cases = [hits()[:-1], hits()+[hits()[-1]],
                 [dict(h, text='清理全部聊天记录') if i == 4 else h for i, h in enumerate(hits())],
                 [dict(h, score=.5) if i == 4 else h for i, h in enumerate(hits())],
                 [dict(h, text='D盘存储空间不足') if i == 1 else h for i, h in enumerate(hits())]]
        for evidence in cases:
            with self.subTest(evidence=evidence), self.assertRaises(RuntimeError):
                dismiss_position(evidence)

    def test_dismiss_text_outside_its_button_is_rejected(self):
        evidence = hits();evidence[-1]['box'][1] = 222
        with self.assertRaises(RuntimeError):
            dismiss_position(evidence)


if __name__ == '__main__':
    unittest.main()
