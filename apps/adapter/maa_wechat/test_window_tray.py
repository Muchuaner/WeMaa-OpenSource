import unittest
from unittest.mock import Mock
import numpy as np
from window_tray import restore_from_tray,click_position


class TrayRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.window={'pid':45,'hwnd':123}
        self.target={'pid':45,'tray_hwnd':456,'bar_hwnd':789,
                     'icon':[1686,1040,1710,1080],'bar':[0,1040,1920,1080]}
        self.frame=np.full((40,1920,3),100,dtype=np.uint8)
        self.api=Mock();self.api.locate.return_value=self.target
        self.api.capture.return_value=self.frame
        self.probe=Mock(return_value={'available':True})

    def restore(self):
        restore_from_tray(self.window,api=self.api,probe=self.probe,sleep=Mock())

    def test_exact_owned_icon_is_activated_only_after_two_stable_frames(self):
        self.restore()
        self.assertEqual(self.api.capture.call_count,2)
        self.assertEqual(self.api.click.call_count,2)
        self.api.click.assert_called_with(self.target,1698,20)
        self.api.leave_icon.assert_called_once_with(self.target)

    def test_wrong_pid_off_taskbar_or_changed_calibration_never_clicks(self):
        for changes in [{'pid':99},{'bar':[0,1000,1920,1040]},
                        {'icon':[1686,1000,1710,1040]},{'icon':[1910,1040,1934,1080]},
                        {'icon':[1686,1040,1686,1080]}]:
            self.api.locate.return_value=dict(self.target,**changes)
            with self.assertRaisesRegex(RuntimeError,'WECHAT_TRAY_NOT_VERIFIED'):self.restore()
        self.api.click.assert_not_called()

    def test_changed_icon_pixels_never_clicks(self):
        changed=self.frame.copy();changed[15,1698]=0
        self.api.capture.side_effect=[self.frame,changed]
        with self.assertRaisesRegex(RuntimeError,'WECHAT_TRAY_NOT_VERIFIED'):self.restore()
        self.api.click.assert_not_called()

    def test_unrelated_taskbar_pixels_do_not_change_owned_icon(self):
        changed=self.frame.copy();changed[15,100]=0
        self.api.capture.side_effect=[self.frame,changed]
        self.restore()
        self.assertEqual(self.api.click.call_count,2)

    def test_icon_moves_or_owner_changes_immediately_before_input(self):
        self.api.locate.side_effect=[self.target,dict(self.target,tray_hwnd=999)]
        with self.assertRaisesRegex(RuntimeError,'WECHAT_TRAY_NOT_VERIFIED'):self.restore()
        self.api.click.assert_not_called()

    def test_locked_input_desktop_never_clicks(self):
        self.probe.return_value={'available':False}
        with self.assertRaisesRegex(RuntimeError,'INPUT_DESKTOP_UNAVAILABLE'):self.restore()
        self.api.click.assert_not_called()

    def test_invalid_frames_never_click(self):
        for frame in [None,np.zeros((40,1918,3)),np.zeros((40,1920)),np.zeros((40,1920,1))]:
            self.api.capture.return_value=frame
            with self.assertRaisesRegex(RuntimeError,'WECHAT_TRAY_NOT_VERIFIED'):self.restore()
        self.api.click.assert_not_called()


if __name__=='__main__':unittest.main()
