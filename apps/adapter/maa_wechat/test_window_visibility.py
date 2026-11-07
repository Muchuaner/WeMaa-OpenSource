import unittest
from unittest.mock import Mock, patch

from window_visibility import ensure_visible, Win32Windows, WECHAT_CLASS, WECHAT_EXE, DISPLAY_PROFILE


class WindowVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.hidden = {'hwnd': 123, 'pid': 45, 'class': WECHAT_CLASS,
                       'exe': WECHAT_EXE, 'client': [1920, 1040],
                       'visible': False, 'minimized': False}
        self.visible = dict(self.hidden, visible=True)
        self.api = Mock()
        self.api.windows.return_value = [self.hidden]
        self.api.inspect.side_effect = [self.hidden, self.visible]
        self.probe = Mock(return_value={'available': True})
        self.sleep = Mock()

    def ensure(self, **kw):
        return ensure_visible(restore_hidden=True, api=self.api,
                              probe=self.probe, sleep=self.sleep, **kw)

    def test_hidden_calibrated_official_window_is_restored_and_rechecked(self):
        result = self.ensure()
        self.assertTrue(result['restored'])
        self.assertEqual(result['window'], self.visible)
        self.api.restore.assert_called_once_with(123)
        self.assertEqual(self.api.inspect.call_count, 2)

    def test_visible_window_does_not_require_input_or_mutation(self):
        self.api.windows.return_value = [self.visible]
        self.assertFalse(self.ensure()['restored'])
        self.api.restore.assert_not_called()
        self.probe.assert_not_called()

    def test_modal_disabled_main_is_not_accepted_as_ready(self):
        self.api.windows.return_value = [dict(self.visible, enabled=False)]
        with self.assertRaisesRegex(RuntimeError, 'WECHAT_MAIN_WINDOW_DISABLED'):
            self.ensure()
        self.api.restore.assert_not_called()
        self.api.maximize.assert_not_called()

    def test_dismissed_storage_dialog_can_remain_hidden_without_blocking_main(self):
        main = dict(self.visible, enabled=True, owner=0)
        dormant = dict(main, hwnd=456, owner=main['hwnd'], visible=False, client=[330, 348])
        self.api.windows.return_value = [main, dormant]
        self.assertEqual(self.ensure()['window'], main)
        self.api.restore.assert_not_called()

    def test_hidden_dialog_never_bypasses_disabled_or_different_owner(self):
        main = dict(self.visible, enabled=True, owner=0)
        dormant = dict(main, hwnd=456, owner=main['hwnd'], visible=False, client=[330, 348])
        pairs = [(dict(main, enabled=False), dormant), (main, dict(dormant, owner=999)),
                 (main, dict(dormant, pid=999)), (main, dict(dormant, visible=True)),
                 (main, dict(dormant, client=[280, 380]))]
        for owner, dialog in pairs:
            with self.subTest(owner=owner, dialog=dialog):
                self.api.windows.return_value = [owner, dialog]
                with self.assertRaises(RuntimeError):
                    self.ensure()
        self.api.restore.assert_not_called()

    def test_read_only_runtime_does_not_restore_hidden_window(self):
        with self.assertRaisesRegex(RuntimeError, 'WECHAT_WINDOW_NOT_UNIQUE:0'):
            ensure_visible(api=self.api, probe=self.probe)
        self.api.restore.assert_not_called()

    def test_minimized_window_can_restore_only_with_original_calibrated_client(self):
        minimized = dict(self.hidden, minimized=True)
        self.api.windows.return_value = [minimized]
        self.api.inspect.side_effect = [minimized, self.visible]
        self.assertTrue(self.ensure()['restored'])

    def test_absent_or_ambiguous_window_never_mutates(self):
        for windows in [[], [self.hidden, dict(self.hidden, hwnd=456)]]:
            self.api.windows.return_value = windows
            with self.assertRaisesRegex(RuntimeError, 'WECHAT_WINDOW_NOT_UNIQUE'):
                self.ensure()
        self.api.restore.assert_not_called()

    def test_same_named_untrusted_process_is_ignored(self):
        self.api.windows.return_value = [dict(self.hidden, exe=r'C:\Temp\Weixin.exe')]
        with self.assertRaisesRegex(RuntimeError, 'WECHAT_WINDOW_NOT_UNIQUE:0'):
            self.ensure()
        self.api.restore.assert_not_called()

    def test_changed_layout_never_mutates(self):
        self.api.windows.return_value = [dict(self.hidden, client=[1918, 1040])]
        with self.assertRaisesRegex(RuntimeError, 'LAYOUT_CHANGED_STOP'):
            self.ensure()
        self.api.restore.assert_not_called()

    def test_continuous_geometry_repair_maximizes_same_main_window(self):
        changed = dict(self.visible, client=[1916, 1037])
        self.api.windows.return_value = [changed]
        self.api.inspect.side_effect = [changed, self.visible]
        self.api.display_profile.return_value = DISPLAY_PROFILE
        result = self.ensure(restore_geometry=True, expected_window=self.visible)
        self.assertTrue(result['geometry_restored'])
        self.api.maximize.assert_called_once_with(123, expected_window=changed)
        self.api.restore.assert_not_called()
        self.assertEqual(result['window']['client'], [1920, 1040])

    def test_initial_geometry_repair_requires_main_sized_window(self):
        login = dict(self.visible, client=[280, 380])
        self.api.windows.return_value = [login]
        with self.assertRaisesRegex(RuntimeError, 'WECHAT_MAIN_WINDOW_NOT_VERIFIED'):
            self.ensure(restore_geometry=True)
        self.api.maximize.assert_not_called()

    def test_ambiguous_or_reused_main_window_is_never_resized(self):
        changed = dict(self.visible, client=[1200, 800])
        for windows in [[changed, dict(changed, hwnd=456)], [dict(changed, pid=99)]]:
            self.api.windows.return_value = windows
            with self.assertRaises(RuntimeError):
                self.ensure(restore_geometry=True, expected_window=self.visible)
        self.api.maximize.assert_not_called()

    def test_other_resolution_work_area_or_dpi_is_not_resized(self):
        changed = dict(self.visible, client=[1200, 800])
        for profile in [dict(DISPLAY_PROFILE, screen=[1280, 720]),
                        dict(DISPLAY_PROFILE, work_area=[0, 0, 1920, 1080]),
                        dict(DISPLAY_PROFILE, dpi=120)]:
            self.api.windows.return_value = [changed]
            self.api.inspect.side_effect = [changed]
            self.api.display_profile.return_value = profile
            with self.assertRaisesRegex(RuntimeError, 'WECHAT_DISPLAY_NOT_CALIBRATED'):
                self.ensure(restore_geometry=True, expected_window=self.visible)
        self.api.maximize.assert_not_called()

    def test_locked_desktop_does_not_resize_a_visible_main_window(self):
        self.api.windows.return_value = [dict(self.visible, client=[1200, 800])]
        self.probe.return_value = {'available': False}
        with self.assertRaisesRegex(RuntimeError, 'INPUT_DESKTOP_UNAVAILABLE'):
            self.ensure(restore_geometry=True, expected_window=self.visible)
        self.api.maximize.assert_not_called()

    def test_failed_maximization_does_not_accept_the_old_geometry(self):
        changed = dict(self.visible, client=[1916, 1037])
        self.api.windows.return_value = [changed]
        self.api.inspect.side_effect = [changed, changed]
        self.api.display_profile.return_value = DISPLAY_PROFILE
        with self.assertRaisesRegex(RuntimeError, 'LAYOUT_CHANGED_STOP'):
            self.ensure(restore_geometry=True, expected_window=self.visible)

    def test_locked_or_wrong_input_desktop_never_mutates(self):
        self.probe.return_value = {'available': False}
        with self.assertRaisesRegex(RuntimeError, 'INPUT_DESKTOP_UNAVAILABLE'):
            self.ensure()
        self.api.restore.assert_not_called()

    def test_reused_handle_or_changed_geometry_before_restore_never_mutates(self):
        for change in [{'pid': 99}, {'exe': r'C:\Other\Weixin.exe'},
                       {'class': 'OtherWindow'}, {'client': [1800, 1040]}]:
            self.api.inspect.side_effect = [dict(self.hidden, **change)]
            with self.assertRaisesRegex(RuntimeError, 'WECHAT_WINDOW_IDENTITY_CHANGED'):
                self.ensure()
        self.api.restore.assert_not_called()

    def test_unverified_post_restore_visibility_or_geometry_stops(self):
        for change in [{'visible': False}, {'minimized': True}, {'client': [1900, 1040]}]:
            self.api.inspect.side_effect = [self.hidden, dict(self.visible, **change)]
            with self.assertRaisesRegex(RuntimeError, 'LAYOUT_CHANGED_STOP'):
                self.ensure()

    def test_changed_owner_after_restore_stops(self):
        self.api.inspect.side_effect = [self.hidden, dict(self.visible, pid=99)]
        with self.assertRaisesRegex(RuntimeError, 'WECHAT_WINDOW_IDENTITY_CHANGED'):
            self.ensure()

    def test_native_restore_uses_verified_existing_process_tray(self):
        api=Win32Windows.__new__(Win32Windows)
        api.inspect=Mock(return_value=self.hidden)
        with patch('window_tray.restore_from_tray') as restore:
            api.restore(123)
        restore.assert_called_once_with(self.hidden)

    def test_native_restore_rejects_reused_window_before_launch(self):
        api=Win32Windows.__new__(Win32Windows)
        api.inspect=Mock(return_value=dict(self.hidden,exe=r'C:\Other\Weixin.exe'))
        with patch('window_tray.restore_from_tray') as restore, \
             self.assertRaisesRegex(RuntimeError,'WECHAT_WINDOW_IDENTITY_CHANGED'):
            api.restore(123)
        restore.assert_not_called()

    def test_native_maximize_rechecks_pid_immediately_before_window_request(self):
        api = Win32Windows.__new__(Win32Windows)
        api.inspect = Mock(return_value=dict(self.visible, pid=99))
        api.user = Mock()
        with self.assertRaisesRegex(RuntimeError, 'WECHAT_WINDOW_IDENTITY_CHANGED'):
            api.maximize(123, expected_window=self.visible)
        api.user.ShowWindowAsync.assert_not_called()


if __name__ == '__main__':
    unittest.main()
