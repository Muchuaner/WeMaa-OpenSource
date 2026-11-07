"""Dismiss only the verified WeChat C-drive warning at a GUI-loop boundary.

No storage is deleted here. The sole worker clicks the dialog's own "do not
remind today" button after native ownership and two fresh OCR frames agree.
"""
from pathlib import Path
import re
import time

from window_visibility import CLIENT_SIZE, DISPLAY_PROFILE, Win32Windows, official, window_candidates

DIALOG_SIZE = [330, 348]
ERROR = 'WECHAT_STORAGE_DIALOG_NOT_VERIFIED'


def dismiss_position(hits):
    """Require the warning and both choices in their calibrated dialog regions."""
    def normalized(text):
        return re.sub(r'\s+', '', text)

    def label(text, region):
        matches = [h for h in hits if normalized(h.get('text', '')) == text
                   and h.get('score', 0) >= .90]
        if len(matches) != 1:
            raise RuntimeError(ERROR)
        box = matches[0].get('box')
        if (not isinstance(box, (list, tuple)) or len(box) != 4
                or any(type(v) is not int for v in box)):
            raise RuntimeError(ERROR)
        x, y, width, height = box
        left, top, right, bottom = region
        if not (width > 0 and height > 0 and left <= x < x+width <= right
                and top <= y < y+height <= bottom):
            raise RuntimeError(ERROR)
        return x+width//2, y+height//2

    label('存储空间不足', (60, 110, 270, 160))
    label('管理存储空间', (45, 210, 285, 248))
    point = label('今日不再提醒', (45, 256, 285, 296))
    body = ''.join(normalized(h.get('text', '')) for h in sorted(
        hits, key=lambda h: (h.get('box', [0, 0])[1], h.get('box', [0])[0]))
        if h.get('score', 0) >= .90)
    if 'C盘存储空间不足' not in body or '请及时清理存储空间' not in body:
        raise RuntimeError(ERROR)
    return point


def recover_storage_dialog(*, expected_window=None, api=None, reader=None,
                           probe=None, sleep=time.sleep):
    api = api or Win32Windows()
    windows = window_candidates(api)
    if len(windows) <= 1:
        if windows and windows[0].get('enabled') is False:
            raise RuntimeError('WECHAT_MAIN_WINDOW_DISABLED')
        return {'restored': False}
    mains = [w for w in windows if w.get('client') == CLIENT_SIZE
             and w.get('visible') and not w.get('minimized') and not w.get('owner')]
    if len(windows) != 2 or len(mains) != 1:
        raise RuntimeError(ERROR)
    main = mains[0]
    dialog = next(w for w in windows if w['hwnd'] != main['hwnd'])
    if expected_window and any(main[k] != expected_window[k] for k in ('hwnd', 'pid')):
        raise RuntimeError('WECHAT_WINDOW_IDENTITY_CHANGED')
    if (main.get('enabled') is not False or dialog.get('owner') != main['hwnd']
            or dialog['pid'] != main['pid'] or dialog.get('client') != DIALOG_SIZE
            or dialog.get('enabled') is not True or not dialog.get('visible')
            or dialog.get('minimized')):
        raise RuntimeError(ERROR)
    if probe is None:
        from desktop_input import desktop_probe
        probe = desktop_probe
    if probe().get('available') is not True:
        raise RuntimeError('INPUT_DESKTOP_UNAVAILABLE')
    if api.display_profile(main['hwnd']) != DISPLAY_PROFILE:
        raise RuntimeError('WECHAT_DISPLAY_NOT_CALIBRATED')
    reader = reader or MaaStorageDialog(dialog)
    import numpy as np
    first, hits1 = reader.observe()
    sleep(.1)
    second, hits2 = reader.observe()
    if (first is None or second is None or first.shape[:2] != (348, 330)
            or first.shape != second.shape or len(first.shape) != 3
            or first.shape[2] not in (3, 4)
            or not np.array_equal(first[:, :, :3], second[:, :, :3])):
        raise RuntimeError(ERROR)
    point = dismiss_position(hits1)
    if dismiss_position(hits2) != point:
        raise RuntimeError(ERROR)
    # Recheck the complete inventory and exact ownership immediately before input.
    fresh = window_candidates(api)
    if (sorted(fresh, key=lambda w: w['hwnd']) != sorted(windows, key=lambda w: w['hwnd'])
            or api.inspect(main['hwnd']) != main or api.inspect(dialog['hwnd']) != dialog):
        raise RuntimeError(ERROR)
    if probe().get('available') is not True:
        raise RuntimeError('INPUT_DESKTOP_UNAVAILABLE')
    reader.click(*point)
    # Qt can acknowledge PostMessage before it actually releases its modal
    # owner. Give the existing click time to finish; never click a second time.
    for _ in range(50):
        sleep(.1)
        current = api.inspect(main['hwnd'])
        remaining = window_candidates(api)
        if (official(current) and current['pid'] == main['pid']
                and current.get('client') == CLIENT_SIZE and current.get('visible')
                and not current.get('minimized') and current.get('enabled') is True
                and len(remaining) == 1 and remaining[0] == current):
            return {'restored': True, 'storage_dialog_dismissed': True,
                    'main': current, 'dialog': dialog,
                    'action': 'do_not_remind_today', 'fresh_frames': 2}
    raise RuntimeError('WECHAT_STORAGE_DIALOG_DISMISS_NOT_VERIFIED')


class MaaStorageDialog:
    def __init__(self, dialog):
        from maa.controller import (Win32Controller,
            MaaWin32ScreencapMethodEnum as Capture, MaaWin32InputMethodEnum as Input)
        from maa.resource import Resource
        from maa.tasker import Tasker
        from maa.toolkit import Toolkit
        root = Path(__file__).resolve().parent
        Toolkit.init_option(str(root/'data'))
        self.controller = Win32Controller(dialog['hwnd'], Capture.PrintWindow,
                                          Input.PostMessage, Input.PostMessage)
        self.controller.set_screenshot_use_raw_size(True)
        if not self.controller.post_connection().wait().succeeded:
            raise RuntimeError(ERROR)
        self.resource = Resource()
        if not self.resource.post_bundle(str(root/'resource')).wait().succeeded:
            raise RuntimeError(ERROR)
        self.tasker = Tasker()
        self.tasker.bind(self.resource, self.controller)
        if not self.tasker.inited:
            raise RuntimeError(ERROR)

    def observe(self):
        from maa.pipeline import JOCR
        from runner import plain
        image = self.controller.post_screencap().wait().get()
        if image is None or image.shape[:2] != (348, 330):
            raise RuntimeError(ERROR)
        job = self.tasker.post_recognition('OCR', JOCR(roi=[0, 0, 330, 348]), image).wait()
        detail = plain(job.get())
        hits = []
        for node in (detail or {}).get('nodes', []):
            hits.extend((node.get('recognition') or {}).get('all_results') or [])
        return image, hits

    def click(self, x, y):
        if not self.controller.post_click(x, y).wait().succeeded:
            raise RuntimeError('WECHAT_STORAGE_DIALOG_DISMISS_NOT_VERIFIED')
