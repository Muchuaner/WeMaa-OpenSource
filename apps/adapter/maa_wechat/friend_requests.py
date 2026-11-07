"""Accept incoming friend requests in the sole GUI worker, with durable receipts."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import time

SCAN_INTERVAL = 30


def policy(root):
    try:
        value = json.loads((Path(root)/'data/friend-request-policy.json').read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return {'auto_accept_friend_requests': True}
    if not isinstance(value, dict) or type(value.get('auto_accept_friend_requests')) is not bool:
        raise ValueError('INVALID_FRIEND_REQUEST_POLICY')
    return value


def initialize(store):
    store.db.execute('CREATE TABLE IF NOT EXISTS friend_requests(id TEXT PRIMARY KEY,status TEXT,detail_json TEXT,updated REAL)')
    store.db.execute("UPDATE friend_requests SET status='uncertain' WHERE status IN ('opened','accept_started')")
    store.db.commit()


def text(hit):
    return re.sub(r'\s+', '', hit.get('text', ''))


def find_label(hits, label, region, score=.90):
    left, top, right, bottom = region
    values = []
    for hit in hits:
        b = hit.get('box', [])
        if (len(b) == 4 and all(type(v) is int for v in b) and hit.get('score', 0) >= score
                and text(hit) == label):
            x, y, w, h = b
            if w > 0 and h > 0 and left <= x < x+w <= right and top <= y < y+h <= bottom:
                values.append((x+w//2, y+h//2))
    if len(values) != 1:
        raise ValueError('FRIEND_LABEL_NOT_UNIQUE:'+label)
    return values[0]


def dialog_state(hits, name):
    find_label(hits, '通过朋友验证', (70, 0, 290, 45))
    find_label(hits, '备注', (20, 45, 95, 95))
    find_label(hits, re.sub(r'\s+', '', name), (20, 90, 335, 140))
    find_label(hits, '朋友权限', (20, 225, 150, 280))
    confirm = find_label(hits, '确定', (65, 565, 160, 630), .95)
    cancel = find_label(hits, '取消', (200, 565, 300, 630), .95)
    return confirm, cancel


def pending_rows(hits):
    """Only the request-category sidebar; never match chat text or an added row."""
    result = []
    headers = [h for h in hits if '新的朋友' in text(h) and h.get('score', 0) >= .9
               and 65 <= h['box'][0] < 200 and 110 <= h['box'][1] < 990]
    if len(headers) != 1:
        return []
    top = headers[0]['box'][1]+25
    ends = [h['box'][1] for h in hits if h.get('score', 0) >= .9
            and h['box'][0] < 200 and h['box'][1] > top
            and any(s in text(h) for s in ('公众号', '企业微信联系人', '联系人'))]
    bottom = min(ends, default=1020)
    for hit in hits:
        x, y, w, h = hit['box']
        if (text(hit) == '等待验证' and hit.get('score', 0) >= .9
                and 410 <= x < x+w <= 477 and top <= y < y+h < bottom):
            result.append(y+h//2)
    return sorted(set(result))


def record(store, key, status, detail):
    store.db.execute('INSERT OR REPLACE INTO friend_requests VALUES(?,?,?,?)',
                     (key, status, json.dumps(detail, ensure_ascii=False), time.time()))
    store.db.commit()
    store.event('friend_request_result', dict(detail, request_id=key, status=status))


class VerificationDialog:
    def __init__(self, runtime, window):
        from maa.controller import Win32Controller, MaaWin32ScreencapMethodEnum as Capture, MaaWin32InputMethodEnum as Input
        from maa.tasker import Tasker
        self.runtime, self.window = runtime, window
        self.controller = Win32Controller(window['hwnd'], Capture.PrintWindow, Input.PostMessage, Input.PostMessage)
        self.controller.set_screenshot_use_raw_size(True)
        if not self.controller.post_connection().wait().succeeded:
            raise RuntimeError('FRIEND_DIALOG_CONNECTION_FAILED')
        self.tasker = Tasker()
        self.tasker.bind(runtime.resource, self.controller)

    def guard(self):
        from window_visibility import Win32Windows, window_candidates, official
        from desktop_input import desktop_probe
        api = Win32Windows()
        windows = window_candidates(api)
        mains = [w for w in windows if w['hwnd'] == self.runtime.hwnd]
        if (desktop_probe().get('available') is not True or len(windows) != 2 or len(mains) != 1
                or mains[0] != self.runtime.verified_window or not official(self.window)
                or api.inspect(self.window['hwnd']) != self.window
                or self.window['pid'] != mains[0]['pid'] or self.window['client'] != [360, 640]
                or not self.window['visible'] or not self.window['enabled']):
            raise RuntimeError('FRIEND_DIALOG_IDENTITY_CHANGED')

    def observe(self):
        from maa.pipeline import JOCR
        from runner import plain
        self.guard()
        image = self.controller.post_screencap().wait().get()
        if image is None or image.shape != (640, 360, 3):
            raise RuntimeError('FRIEND_DIALOG_FRAME_CHANGED')
        detail = plain(self.tasker.post_recognition('OCR', JOCR(roi=[0, 0, 360, 640]), image).wait().get())
        hits = []
        for node in (detail or {}).get('nodes', []):
            hits.extend((node.get('recognition') or {}).get('all_results') or [])
        return image, hits

    def click(self, point):
        self.guard()
        if not self.controller.post_click(*point).wait().succeeded:
            raise RuntimeError('FRIEND_DIALOG_CLICK_FAILED')

    def visible(self):
        from window_visibility import Win32Windows
        return any(w['hwnd'] == self.window['hwnd'] and w['visible']
                   for w in Win32Windows().windows())


def confirm_dialog(store, key, name, dialog, *, sleep=time.sleep):
    """Commit before the membership action; a crash or uncertain outcome cannot replay it."""
    import numpy as np
    prior = store.db.execute('SELECT status FROM friend_requests WHERE id=?', (key,)).fetchone()
    if not prior or prior[0] != 'opened':
        return {'status': prior[0] if prior else 'blocked', 'confirm_clicks': 0}
    first, hits = dialog.observe()
    point, _ = dialog_state(hits, name)
    sleep(.1)
    second, fresh_hits = dialog.observe()
    if not np.array_equal(first, second) or dialog_state(fresh_hits, name)[0] != point:
        raise ValueError('FRIEND_DIALOG_CHANGED')
    detail = {'name': name, 'confirm_clicks': 1}
    record(store, key, 'accept_started', detail)
    try:
        dialog.click(point)
        for _ in range(30):
            sleep(.1)
            if not dialog.visible():
                return dict(detail, status='confirmation_closed')
    except Exception as exc:
        detail['reason'] = str(exc)
    record(store, key, 'uncertain', detail)
    return dict(detail, status='uncertain')


class FriendRequests:
    def __init__(self, root, store):
        self.root, self.store = Path(root), store
        self.next_check = 0
        self.last_result = None
        initialize(store)

    def health(self):
        return {'enabled': policy(self.root)['auto_accept_friend_requests'],
                'scan_interval_seconds': SCAN_INTERVAL,
                'status_counts': dict(self.store.db.execute('SELECT status,count(*) FROM friend_requests GROUP BY status')),
                'last_result': self.last_result}

    def process(self, runtime, label, *, force=False):
        if not policy(self.root)['auto_accept_friend_requests'] or (not force and time.time() < self.next_check):
            return False
        # Never navigate while an earlier send has entered a physical input phase.
        if self.store.db.execute("SELECT 1 FROM reply_outbox WHERE status IN ('queued','ready','send_started') LIMIT 1").fetchone():
            return False
        self.next_check = time.time()+SCAN_INTERVAL
        from window_visibility import Win32Windows, window_candidates
        if window_candidates(Win32Windows()) != [runtime.verified_window]:
            return False
        try:
            runtime.node('OpenContacts')
            image = runtime.capture_image()
            hits = runtime.recognize_image('Read', image, roi=[65, 80, 412, 950])
            headers = [h for h in hits if '新的朋友' in text(h) and h.get('score', 0) >= .9 and h['box'][0] < 200]
            if not headers:
                # Qt consumes small wheel steps even for a large delta. Bound the
                # full return-to-top and verify the category afterward.
                runtime.node('ContactsScrollUp', {'ContactsScrollUp': {'repeat': 600, 'repeat_delay': 1}})
                image = runtime.capture_image()
                hits = runtime.recognize_image('Read', image, roi=[65, 80, 412, 950])
                headers = [h for h in hits if '新的朋友' in text(h) and h.get('score', 0) >= .9 and h['box'][0] < 200]
            if len(headers) != 1:
                raise ValueError('FRIEND_CATEGORY_NOT_VERIFIED')
            header = headers[0]; hy = header['box'][1]
            following = [h['box'][1] for h in hits if h['box'][1] > hy+25 and h['box'][0] < 200
                         and ('公众号' in text(h) or '企业微信联系人' in text(h))]
            if following and min(following)-hy < 65:
                x, y, w, h = header['box']
                self.click(runtime, (x+w//2, y+h//2))
                image = runtime.capture_image()
                hits = runtime.recognize_image('Read', image, roi=[65, 80, 412, 950])
            for y in pending_rows(hits):
                if self.accept_row(runtime, image, y, label):
                    break
            else:
                self.last_result = {'status': 'checked', 'pending_visible': len(pending_rows(hits))}
        except Exception as exc:
            self.last_result = {'status': 'blocked', 'reason': str(exc)}
            self.store.event('friend_request_scan_blocked', self.last_result)
        finally:
            # Unrecognized remaining dialogs keep the ordinary input gate closed.
            if window_candidates(Win32Windows()) == [runtime.verified_window]:
                runtime.node('OpenRecent')
        return True

    @staticmethod
    def click(runtime, point):
        runtime.node('SelectContactRow', {'SelectContactRow': {'target': [*point, 1, 1], 'repeat': 1}})

    def accept_row(self, runtime, image, y, label):
        import numpy as np
        from PIL import Image
        from window_visibility import Win32Windows, window_candidates
        from send import _ocr_scaled
        # Isolate the name from avatar pixels and the validation status column.
        roi = [130, y-17, 280, 34]
        names = []
        for scale in (2, 3):
            hs = _ocr_scaled(runtime, image, 'Read', roi, scale)
            hs = [h for h in hs if abs(h['box'][1]+h['box'][3]/2-y) <= 8 and h.get('score', 0) >= .9]
            names.append(''.join(h['text'] for h in sorted(hs, key=lambda h: h['box'][0])).strip())
        if not names[0] or names[0] != names[1]:
            raise ValueError('FRIEND_NAME_NOT_STABLE')
        name = names[0]
        avatar = image[y-8:y+20, 98:122].copy()
        key = hashlib.sha256(name.encode()+avatar.tobytes()).hexdigest()
        if self.store.db.execute('SELECT 1 FROM friend_requests WHERE id=?', (key,)).fetchone():
            return False
        current = runtime.capture_image()
        if not np.array_equal(image[y-18:y+28, 90:477], current[y-18:y+28, 90:477]):
            raise ValueError('FRIEND_ROW_CHANGED')
        self.click(runtime, (230, y))
        current = runtime.capture_image()
        main_hits = runtime.recognize_image('Read', current, roi=[1030, 80, 420, 800])
        find_label(main_hits, re.sub(r'\s+', '', name), (1100, 80, 1400, 165))
        point = find_label(main_hits, '前往验证', (1050, 290, 1390, 550), .95)
        proof = self.root/'data'/('friend-request-'+key+'-before.png')
        Image.fromarray(current[:, :, ::-1]).save(proof)
        record(self.store, key, 'opened', {'name': name, 'frame': str(proof), 'confirm_clicks': 0})
        dialog = None
        try:
            self.click(runtime, point)
            for _ in range(20):
                windows = window_candidates(Win32Windows())
                pages = [w for w in windows if w['pid'] == runtime.verified_window['pid']
                         and w['client'] == [360, 640] and w['visible']]
                if len(windows) == 2 and len(pages) == 1:
                    dialog = VerificationDialog(runtime, pages[0]);break
                time.sleep(.1)
            if dialog is None:
                raise ValueError('FRIEND_DIALOG_NOT_UNIQUE')
            result = confirm_dialog(self.store, key, name, dialog)
            if result['status'] == 'confirmation_closed':
                result['status'] = 'uncertain'
                for _ in range(10):
                    after, path = runtime.capture(label+'-friend-after')
                    hs = runtime.recognize_image('Read', after, roi=[65, 80, 1380, 850])
                    try:
                        added = (find_label(hs, '已添加', (410, y-25, 477, y+25))
                                 and find_label(hs, '发消息', (1030, 200, 1400, 800)))
                    except ValueError:
                        added = False
                    if np.array_equal(avatar, after[y-8:y+20, 98:122]) and added:
                        result.update(status='accepted', frame=path, added_row_verified=True, profile_verified=True)
                        break
                    time.sleep(.2)
            record(self.store, key, result['status'], result)
            self.last_result = result
        except Exception as exc:
            prior = self.store.db.execute('SELECT status FROM friend_requests WHERE id=?', (key,)).fetchone()[0]
            self.last_result = {'status': 'uncertain' if prior == 'accept_started' else 'blocked', 'name': name, 'reason': str(exc)}
            record(self.store, key, self.last_result['status'], self.last_result)
        finally:
            if dialog and dialog.visible():
                # Cancel only this verified dialog; never send Esc into a chat.
                try:
                    _, hs = dialog.observe();_, cancel = dialog_state(hs, name);dialog.click(cancel)
                except Exception as exc:
                    self.store.event('friend_request_dialog_remains', {'reason': str(exc)})
        return True
