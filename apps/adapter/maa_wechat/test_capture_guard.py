import unittest
from unittest.mock import Mock

import numpy as np

try:
    from maa_wechat.capture_guard import capture_with_retry
except ModuleNotFoundError:
    from capture_guard import capture_with_retry


class CaptureGuardTests(unittest.TestCase):
    def setUp(self):
        self.good = np.full((1040, 1920, 3), 245, dtype=np.uint8)
        self.wrong = np.full((1040, 1918, 3), 245, dtype=np.uint8)
        self.black = np.zeros((1040, 1920, 3), dtype=np.uint8)
        self.sleep = Mock()

    def capture(self, frames, geometry=None):
        get_frame = Mock(side_effect=frames)
        geometry_ok = Mock(return_value=True) if geometry is None else Mock(side_effect=geometry)
        result = capture_with_retry(get_frame, geometry_ok, sleep=self.sleep)
        return result, get_frame, geometry_ok

    def test_first_valid_frame_returned_without_delay(self):
        result, get_frame, geometry_ok = self.capture([self.good])
        self.assertIs(result, self.good)
        self.assertEqual(get_frame.call_count, 1)
        self.assertEqual(geometry_ok.call_count, 2)
        self.sleep.assert_not_called()

    def test_two_pixel_transient_uses_fresh_frame(self):
        result, get_frame, _ = self.capture([self.wrong, self.good])
        self.assertIs(result, self.good)
        self.assertEqual(get_frame.call_count, 2)
        self.sleep.assert_called_once_with(0.15)

    def test_third_attempt_may_recover(self):
        result, get_frame, _ = self.capture([None, self.black, self.good])
        self.assertIs(result, self.good)
        self.assertEqual(get_frame.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [((0.15,),), ((0.15,),)])

    def test_geometry_before_capture_must_be_valid(self):
        result, get_frame, geometry_ok = self.capture(
            [self.good, self.good], [False, True, True, True])
        self.assertIs(result, self.good)
        self.assertEqual(get_frame.call_count, 2)
        self.assertEqual(geometry_ok.call_count, 4)

    def test_geometry_after_capture_must_be_valid(self):
        result, get_frame, _ = self.capture(
            [self.good, self.good], [True, False, True, True])
        self.assertIs(result, self.good)
        self.assertEqual(get_frame.call_count, 2)

    def test_persistent_wrong_size_never_returned_or_resized(self):
        get_frame = Mock(return_value=self.wrong)
        with self.assertRaisesRegex(RuntimeError, r"MAA_CAPTURE_DIMENSIONS:\(1040, 1918, 3\)"):
            capture_with_retry(get_frame, lambda: True, sleep=self.sleep)
        self.assertEqual(get_frame.call_count, 3)
        self.assertEqual(self.wrong.shape, (1040, 1918, 3))
        self.assertEqual(self.sleep.call_count, 2)

    def test_persistent_black_stops(self):
        get_frame = Mock(return_value=self.black)
        with self.assertRaisesRegex(RuntimeError, "MAA_BLACK_FRAME"):
            capture_with_retry(get_frame, lambda: True, sleep=self.sleep)
        self.assertEqual(get_frame.call_count, 3)

    def test_persistent_geometry_failure_stops(self):
        get_frame = Mock(return_value=self.good)
        with self.assertRaisesRegex(RuntimeError, "LAYOUT_CHANGED_STOP"):
            capture_with_retry(get_frame, lambda: False, sleep=self.sleep)
        self.assertEqual(get_frame.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_none_frames_stop(self):
        get_frame = Mock(return_value=None)
        with self.assertRaisesRegex(RuntimeError, "MAA_CAPTURE_DIMENSIONS:None"):
            capture_with_retry(get_frame, lambda: True, sleep=self.sleep)
        self.assertEqual(get_frame.call_count, 3)

    def test_invalid_channels_rejected(self):
        for shape in ((1040, 1920), (1040, 1920, 1), (1040, 1920, 0)):
            with self.subTest(shape=shape), self.assertRaisesRegex(RuntimeError, "MAA_CAPTURE_DIMENSIONS"):
                capture_with_retry(lambda: np.zeros(shape, dtype=np.uint8),
                                   lambda: True, sleep=self.sleep)

    def test_opaque_alpha_does_not_make_black_frame_valid(self):
        image = np.zeros((1040, 1920, 4), dtype=np.uint8)
        image[:, :, 3] = 255
        with self.assertRaisesRegex(RuntimeError, "MAA_BLACK_FRAME"):
            capture_with_retry(lambda: image, lambda: True, sleep=self.sleep)

    def test_nonblack_four_channel_frame_retained_without_changes(self):
        image = np.full((1040, 1920, 4), 245, dtype=np.uint8)
        result, _, _ = self.capture([image])
        self.assertIs(result, image)

    def test_capture_exception_propagates_without_retry(self):
        get_frame = Mock(side_effect=ValueError("CAPTURE_API_ERROR"))
        with self.assertRaisesRegex(ValueError, "CAPTURE_API_ERROR"):
            capture_with_retry(get_frame, lambda: True, sleep=self.sleep)
        self.assertEqual(get_frame.call_count, 1)
        self.sleep.assert_not_called()

    def test_geometry_exception_propagates_without_retry(self):
        get_frame = Mock(return_value=self.good)
        with self.assertRaisesRegex(ValueError, "GEOMETRY_API_ERROR"):
            capture_with_retry(get_frame, Mock(side_effect=ValueError("GEOMETRY_API_ERROR")),
                               sleep=self.sleep)
        get_frame.assert_not_called()
        self.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
