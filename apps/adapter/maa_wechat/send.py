"""One-shot, evidence-verified Maa text sender for the fixed WeChat baseline.

The caller must durably claim event_id and record send_started *before* calling.
Transient failures before input may retry five times. Once the click is attempted,
all missing evidence returns uncertain, and callers must not automatically resend it.
Chinese input uses Maa InputText, not clipboard, Enter, UIA or the old gateway.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Mapping

import numpy as np
from PIL import Image

try:
    from .message_vision import extract_message_payloads, message_identity, reconcile_visual
except ImportError:
    from message_vision import extract_message_payloads, message_identity, reconcile_visual


INPUT_ROI = (495, 902, 1400, 88)
CHAT_ROI = (490, 82, 1395, 805)
MAX_REPLY_CHARS = 400
MIN_OCR_SCORE = 0.80


def _normal(text):
    # Only whitespace introduced by OCR/wrapping is tolerated; no spelling edits.
    return re.sub(r'\s+', '', str(text))


_OUTGOING_PUNCT_MAP = str.maketrans({
    '！': '!', '？': '?', '，': ',', '。': '.',
    '：': ':', '；': ';', '“': '"', '”': '"',
    '‘': "'", '’': "'", '（': '(', '）': ')',
    '【': '[', '】': ']', '—': '-', '–': '-',
})


def _outgoing_text(text):
    # OCR often emits ASCII or fullwidth variants of the same punctuation glyphs.
    # This applies only to observing an already sent bubble, never to draft,
    # identity, or history location. Words and spelling remain exact.
    return _normal(text).translate(_OUTGOING_PUNCT_MAP)


def _box(item):
    value = item.get('box', [])
    if isinstance(value, Mapping):
        value = [value.get(name, 0) for name in ('x', 'y', 'w', 'h')]
    if len(value) != 4:
        raise ValueError('INVALID_OCR_BOX')
    return [int(round(v)) for v in value]


def _text(items):
    return ' '.join(str(item.get('text', '')) for item in sorted(items, key=lambda hit: (_box(hit)[1], _box(hit)[0]))).strip()


def _high_confidence(items):
    for item in items:
        try:
            score = float(item.get('score', 0))
        except (ValueError, TypeError):
            return False
        if not np.isfinite(score) or score < MIN_OCR_SCORE:
            return False
    return bool(items)


def _image(image):
    image = np.asarray(image)
    if image.shape != (1040, 1920, 3) or image.dtype != np.uint8 or not image.any():
        raise ValueError('SEND_FRAME_GEOMETRY_OR_CONTENT_INVALID')
    return image


def _ocr_scaled(runtime, image, node, roi, scale):
    """A separately transformed OCR pass, mapped back to source coordinates."""
    x, y, w, h = roi
    # Nearest-neighbor pixel duplication changed the real Chinese enumeration
    # comma “、” into “。” in the self-test. Lanczos preserves the glyph shape;
    # it is still a genuinely observed independent OCR pass, not punctuation
    # correction or relaxed comparison of different messages.
    width, height = round(w*scale), round(h*scale)
    enlarged = np.asarray(Image.fromarray(image[y:y+h, x:x+w, :3]).resize(
        (width, height), Image.Resampling.LANCZOS))
    crop_hits = runtime.recognize_image(node, enlarged, roi=[0, 0, width, height])
    mapped = []
    for hit in crop_hits:
        bx, by, bw, bh = _box(hit)
        mapped.append(dict(hit, box=[x+round(bx/scale), y+round(by/scale), max(1, round(bw/scale)), max(1, round(bh/scale))]))
    return mapped


def _ocr_passes(runtime, image, node, roi):
    """Keep the native + 2x-crop API; ambiguous callers may request a 1.5x pass."""
    return runtime.recognize_image(node, image), _ocr_scaled(runtime, image, node, roi, 2.0)


def _empty_pixels(image):
    """Empty input requires pixels as well as absent OCR, protecting user drafts.

    The fixed light-theme input ROI excludes the toolbar and rounded border.
    A single thin green caret is allowed; dark or colored text/images are not.
    Unsupported themes fail closed instead of clearing or overwriting content.
    """
    x, y, w, h = INPUT_ROI
    rgb = image[y+4:y+h-3, x+5:x+w-5, :3][:, :, ::-1]
    background = np.median(rgb.reshape(-1, 3), axis=0)
    if background.min() < 240 or background.max()-background.min() > 5:
        return False
    foreground = (np.max(np.abs(rgb.astype(np.int16)-background), axis=2) > 25) & (rgb.min(axis=2) < 220)
    ys, xs = np.nonzero(foreground)
    if not len(xs):
        return True
    colors = rgb[ys, xs].astype(np.int16)
    green = (colors[:, 1]-colors[:, 0] > 40) & (colors[:, 1]-colors[:, 2] > 30)
    return bool(xs.max()-xs.min() <= 2 and ys.max()-ys.min() <= 30 and len(xs) <= 75 and green.all())


@lru_cache(maxsize=1)
def _placeholder_reference():
    return np.asarray(Image.open(Path(__file__).parent/'resource/input-placeholder-v1.png').convert('RGB'))[:, :, ::-1].copy()


def _placeholder_pixels(image):
    """Exact calibrated light-theme placeholder; never exempt arbitrary text.

    Compare the entire input area, including space outside the hint. Only the
    thin green insertion caret may blink; typed text, previews and theme changes
    remain blocked. A missing calibration asset fails closed.
    """
    x,y,w,h=INPUT_ROI
    actual=image[y:y+h,x:x+w,:3]
    try: reference=_placeholder_reference()
    except (OSError,ValueError): return False
    if actual.shape!=reference.shape: return False
    def caret(pixels):
        b,g,r=np.moveaxis(pixels.astype(np.int16),2,0)
        mask=(g-r>40)&(g-b>30)
        ys,xs=np.nonzero(mask)
        if len(xs) and (xs.max()-xs.min()>2 or ys.max()-ys.min()>30 or len(xs)>75): return None
        return mask
    a,b=caret(actual),caret(reference)
    if a is None or b is None: return False
    delta=np.max(np.abs(actual.astype(np.int16)-reference.astype(np.int16)),axis=2)
    # Caret may replace only background, never cover up a changed glyph.
    ignored=(a & (reference.min(axis=2)>=240)) | (b & (actual.min(axis=2)>=240)) | (a&b)
    return bool(np.all((delta<=3)|ignored))


def _draft(runtime, image, expected=None):
    native, enlarged = _ocr_passes(runtime, image, 'ReadChatInput', INPUT_ROI)
    passes = [native, enlarged]
    texts = [_text(items) for items in passes]
    matching_passes = []
    if expected is None:
        verified = not any(_normal(text) for text in texts) and _empty_pixels(image)
        if not verified and all(_normal(text)=='按住鼠标语音输入文字' for text in texts):
            verified = _placeholder_pixels(image)
    else:
        matching_passes = [i for i, items in enumerate(passes) if _normal(texts[i]) == _normal(expected) and _high_confidence(items)]
        if len(matching_passes) < 2:
            passes.append(_ocr_scaled(runtime, image, 'ReadChatInput', INPUT_ROI, 1.5))
            texts.append(_text(passes[-1]))
            if _normal(texts[-1]) == _normal(expected) and _high_confidence(passes[-1]):
                matching_passes.append(2)
        verified = len(matching_passes) >= 2
    result = {'verified': verified, 'texts': texts, 'matching_passes': matching_passes,
              'mode': 'empty' if expected is None else 'exact_draft'}
    if expected is not None and not verified:
        try:
            try:
                from .text_copy import native_draft
            except ImportError:
                from text_copy import native_draft
            result['native_copy'] = native_draft(runtime, image, expected)
            result['verified'] = result['native_copy']['verified']
        except (ValueError, RuntimeError, AttributeError, OSError, ImportError) as exc:
            result['native_copy'] = {'verified': False, 'reason': str(exc)}
    return result


def _target(runtime, image, request, *, enforce_row_y=True):
    blue = runtime.recognize_image('ChatTitleBlueBadge', image)
    badge_x = min((_box(hit)[0] for hit in blue if _box(hit)[0] > 510 and 5 <= _box(hit)[2] <= 25 and 5 <= _box(hit)[3] <= 25), default=None)
    title_roi = [490, 35, badge_x-493, 40] if badge_x else None
    title_hits = runtime.recognize_image('ReadChatTitle', image, roi=title_roi)
    title = ' '.join(str(hit.get('text', '')) for hit in sorted(title_hits, key=lambda hit: _box(hit)[0])).strip()
    expected_title = _normal(request['conversation_title'])
    observed_title = _normal(title)
    from name_match import names_match,title_base
    title_ok = names_match(title_base(observed_title),title_base(expected_title))
    primary_title=title;scaled_titles=[]
    if not title_ok:
        stable=[]
        for scale in (2,3):
            hits=_ocr_scaled(runtime,image,'ReadChatTitle',title_roi or [490,35,1100,40],scale)
            value=' '.join(str(h.get('text','')) for h in sorted(hits,key=lambda h:_box(h)[0])).strip()
            scaled_titles.append(value)
            stable.append(bool(hits) and all(float(h.get('score',0))>=MIN_OCR_SCORE for h in hits))
        if all(stable) and all(names_match(title_base(v),title_base(expected_title)) for v in scaled_titles) and names_match(title_base(scaled_titles[0]),title_base(scaled_titles[1])):
            title=scaled_titles[0];title_ok=True
    selected = runtime.recognize_image('ContactSelectedRow', image)
    centers = []
    for hit in runtime.recognize_image('ListRows', image, 'recent'):
        detail = hit.get('detail') or {}
        if isinstance(detail, str):
            import json
            detail = json.loads(detail)
        centers.extend(detail.get('centers', []))
    selected_centers = []
    for center in centers:
        if any(_box(hit)[1] <= center < _box(hit)[1]+_box(hit)[3] for hit in selected):
            selected_centers.append(int(center))
    selected_centers = sorted(set(selected_centers))
    evidence = {'verified': False, 'title': title, 'title_verified': title_ok, 'selected_centers': selected_centers,'title_ocr_primary':primary_title,'title_ocr_scaled':scaled_titles}
    if not title_ok or len(selected_centers) != 1:
        return evidence
    center = selected_centers[0]
    if not 94 <= center <= 1010:
        return evidence
    from recent_name import row_name
    name,name_items=row_name(runtime,image,center)
    avatar_sha = hashlib.sha256(np.ascontiguousarray(image[center-13:center+13, 78:102, :3]).tobytes()).hexdigest()
    evidence.update({'name': name, 'row_y': center, 'avatar_sha': avatar_sha,
                     'verified': names_match(name, request['name']) and avatar_sha == request['avatar_sha']})
    if enforce_row_y and request.get('row_y') is not None:
        evidence['verified'] = evidence['verified'] and abs(center-request['row_y']) <= 1
    return evidence


def _payloads(runtime, image, *, group_chat=False, previous_payloads=None):
    try:
        from .text_copy import read_messages
    except ImportError:
        from text_copy import read_messages
    options = {'previous_payloads': previous_payloads} if previous_payloads is not None else {}
    payloads,_ = read_messages(runtime,image,group_chat=group_chat,**options)
    return payloads


def _new_outgoing(before, after, expected):
    """Require a complete, stable new outgoing occurrence after visual anchors."""
    old = [(index, message_identity(payload)) for index, payload in enumerate(before) if message_identity(payload)]
    new = [(index, message_identity(payload)) for index, payload in enumerate(after) if message_identity(payload)]
    a, b = [token for _, token in old], [token for _, token in new]
    if not b:
        return None
    if not a:
        # An empty visible history can only confirm a sole new outgoing bubble.
        tail_start = 0 if len(b) == 1 else None
    else:
        overlaps = []
        for length in range(1, min(len(a), len(b))+1):
            anchor = b[:length]
            if a[-length:] != anchor:
                continue
            if sum(a[i:i+length] == anchor for i in range(len(a)-length+1)) != 1:
                continue
            if sum(b[i:i+length] == anchor for i in range(len(b)-length+1)) != 1:
                continue
            overlaps.append(new[length-1][0]+1)
        tail_start = overlaps[0] if len(overlaps) == 1 else None
    if tail_start is None:
        recovered=reconcile_visual(before,after)
        if not recovered or recovered['status']!='append':return None
        tail_start=recovered['overlap']
    counts_before, counts_after = Counter(a), Counter(b)
    candidates = []
    for index, payload in enumerate(after):
        token = message_identity(payload)
        if (index >= tail_start and token and payload.get('direction') == 'outgoing'
                and payload.get('message_kind') == 'text' and payload.get('ocr', {}).get('stable') is True
                and _outgoing_text(payload.get('text', '')) == _outgoing_text(expected)
                and counts_after[token] > counts_before[token]):
            candidates.append({'index': index, 'payload': payload, 'prior_occurrences': counts_before[token], 'current_occurrences': counts_after[token]})
    # 发送期间可能继续收到来信。回复必须是最后一条出站消息，但后续入站
    # 不能把已送达的回复变成 uncertain；位置、唯一新增次数和精确正文仍须成立。
    outgoing_indices = [index for index, _ in new if after[index].get('direction') == 'outgoing']
    if len(candidates) != 1 or candidates[0]['index'] != outgoing_indices[-1]:
        return None
    return candidates[0]


def _request(value):
    if not isinstance(value, Mapping):
        raise ValueError('SEND_REQUEST_MUST_BE_MAPPING')
    request = dict(value)
    request['conversation_title'] = request.get('conversation_title') or request.get('title') or request.get('name')
    request['name'] = request.get('name') or request.get('conversation_title')
    for field in ('event_id', 'conversation_title', 'name', 'reply_text'):
        if not isinstance(request.get(field), str) or not request[field].strip():
            raise ValueError('SEND_REQUEST_MISSING_'+field.upper())
    if len(request['event_id']) > 256 or len(request['name']) > 256 or len(request['conversation_title']) > 256:
        raise ValueError('SEND_IDENTITY_TOO_LONG')
    text = request['reply_text']
    if len(text) > MAX_REPLY_CHARS:
        raise ValueError('SEND_REPLY_TOO_LONG')
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        # In particular, InputText must not inject newline into an Enter-to-send UI.
        raise ValueError('SEND_REPLY_CONTROL_OR_NEWLINE_FORBIDDEN')
    if not isinstance(request.get('avatar_sha'), str) or not re.fullmatch(r'[a-f0-9]{64}', request['avatar_sha']):
        raise ValueError('SEND_AVATAR_SHA_INVALID')
    if request.get('row_y') is not None and (not isinstance(request['row_y'], int) or isinstance(request['row_y'], bool) or not 94 <= request['row_y'] <= 1010):
        raise ValueError('SEND_ROW_Y_INVALID')
    return request


def _send_verified_once(runtime, root, run_id, request, *, preflight=None, expected_chat_sha=None, refresh_row_position=False, previous_payloads=None):
    """Perform at most one SendDraft click and return structured final evidence.

    Status is sent, blocked (no send click attempted), or uncertain (the send
    click might have happened). Neither blocked nor uncertain authorizes retry.
    Existing drafts and all failed prepared drafts are retained, never cleared.
    ``root`` is kept in the worker integration contract; screenshots belong to
    Runtime.capture's own fixed data directory rather than a requested path.
    """
    began = time.monotonic()
    diagnostic = {'frames': [], 'stages': [], 'draft_retained': False, 'send_click_attempted': False,
                  'history_before': [], 'after_payloads': [], 'timings': []}
    evidence = {'title_verified': False, 'outgoing_verified': False}
    event_id = request.get('event_id') if isinstance(request, Mapping) else None
    snapshot_id = request.get('snapshot_id') if isinstance(request, Mapping) else None
    if isinstance(snapshot_id, int) and not isinstance(snapshot_id, bool) and snapshot_id >= 0:
        evidence['snapshot_id'] = snapshot_id

    def result(status, reason):
        diagnostic['elapsed_seconds'] = round(time.monotonic() - began, 4)
        # Only the explicitly bounded summary is fit for remote reporting.
        # Raw OCR, paths and local stages stay in an independent diagnostic key.
        return {'status': status, 'reason': reason, 'event_id': event_id,
                'evidence': evidence, 'diagnostic': diagnostic}

    try:
        request = _request(request)
        if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', run_id):
            raise ValueError('SEND_RUN_ID_INVALID')
        suffix = hashlib.sha256(request['event_id'].encode('utf-8')).hexdigest()[:16]

        def frame(stage):
            started = time.monotonic()
            image, path = runtime.capture(f'{run_id}-send-{suffix}-{stage}')
            image = _image(image)
            sha = hashlib.sha256(image.tobytes()).hexdigest()
            diagnostic['frames'].append({'stage': stage, 'path': str(path), 'sha256': sha,
                                         'captured_at': time.time()})
            diagnostic['timings'].append({'phase': 'capture:' + stage, 'seconds': round(time.monotonic()-started, 4)})
            evidence['frame_sha256'] = sha
            return image

        def verify(image, stage, expected=None):
            started = time.monotonic()
            # Incoming messages and outgoing sends can move this same chat.
            # Opted-in sole owners still reverify title, unique selected row,
            # name and exact avatar at every stage; only its y may change.
            target = _target(runtime, image, request, enforce_row_y=not stage.startswith('after-') and not refresh_row_position)
            if refresh_row_position and target['verified']:
                move={'previous':request.get('row_y'),'verified':target['row_y']}
                if stage=='before':diagnostic['row_position_refresh']=move
                if move['previous']!=move['verified']:
                    diagnostic.setdefault('verified_row_moves',[]).append({'stage':stage,**move})
                request['row_y']=target['row_y']
            draft = (_draft(runtime, image, expected) if target['verified'] or expected is None
                     else {'verified': False, 'mode': 'target_not_verified'})
            diagnostic['stages'].append({'stage': stage, 'target': target, 'draft': draft})
            evidence['title_verified'] = target['verified']
            diagnostic['timings'].append({'phase': 'verify:' + stage, 'seconds': round(time.monotonic()-started, 4)})
            return target['verified'], draft['verified']

        image = frame('before')
        if expected_chat_sha and hashlib.sha256(image[82:887,490:1885,:3].tobytes()).hexdigest()!=expected_chat_sha:
            return result('blocked','experiment_chat_changed_before_opening')
        target_ok, draft_ok = verify(image, 'before')
        if not target_ok:
            return result('blocked', 'target_not_verified')
        if not draft_ok:
            diagnostic['draft_retained'] = True
            return result('blocked', 'existing_or_unreadable_draft')
        started = time.monotonic()
        before = _payloads(runtime, image, previous_payloads=previous_payloads)
        diagnostic['timings'].append({'phase': 'messages:before', 'seconds': round(time.monotonic()-started, 4)})
        diagnostic['history_before'] = before
        runtime.node('FocusChatInput')
        focused = frame('focused')
        target_ok, draft_ok = verify(focused, 'focused')
        if not target_ok or not draft_ok:
            diagnostic['draft_retained'] = not draft_ok
            return result('blocked', 'target_or_draft_changed_before_input')
        diagnostic['draft_retained'] = True
        if preflight: preflight()
        if any(ord(char)>0xffff for char in request['reply_text']):
            from draft_input import paste_draft
            diagnostic['draft_input']=paste_draft(runtime,focused,request['reply_text'])
        else:
            runtime.node('InputDraft', {'InputDraft': {'input_text': request['reply_text'], 'repeat': 1, 'next': [], 'on_error': []}})
        prepared = frame('prepared')
        target_ok, draft_ok = verify(prepared, 'prepared', request['reply_text'])
        if not target_ok or not draft_ok:
            return result('blocked', 'target_or_exact_draft_not_verified')
        # Reverify on a fresh frame immediately before the one-shot send node.
        final = frame('preclick')
        target_ok, draft_ok = verify(final, 'preclick', request['reply_text'])
        if not target_ok or not draft_ok:
            return result('blocked', 'target_or_draft_changed_before_click')
        if expected_chat_sha and hashlib.sha256(final[82:887,490:1885,:3].tobytes()).hexdigest()!=expected_chat_sha:
            return result('blocked','experiment_inbound_before_click')
        if preflight: preflight()
        diagnostic['send_click_attempted'] = True
        sent_node = runtime.node('SendDraft', {'SendDraft': {'repeat': 1, 'next': [], 'on_error': []}}, allow_miss=True)
        diagnostic['send_node_succeeded'] = sent_node.get('succeeded') is True
        previous = before
        # Screenshot/OCR observation can repeat; the send action never repeats.
        for attempt in range(3):
            if attempt > 0:
                time.sleep(0.25 * attempt)
            after_image = frame(f'after-{attempt}')
            target_ok, empty = verify(after_image, f'after-{attempt}')
            if not target_ok:
                return result('uncertain', 'target_changed_after_click')
            if not empty:
                continue
            started = time.monotonic()
            after = _payloads(runtime, after_image, previous_payloads=previous)
            diagnostic['timings'].append({'phase': 'messages:after-' + str(attempt), 'seconds': round(time.monotonic()-started, 4)})
            previous = after
            diagnostic['after_payloads'].append({'stage': f'after-{attempt}', 'payloads': after})
            outgoing = _new_outgoing(before, after, request['reply_text'])
            if outgoing and diagnostic['send_node_succeeded']:
                diagnostic['outgoing'] = outgoing
                diagnostic['draft_retained'] = False
                evidence['outgoing_verified'] = True
                return result('sent', 'empty_draft_and_new_exact_outgoing_verified')
        return result('uncertain', 'send_postcondition_not_verified')
    except Exception as exc:
        return result('uncertain' if diagnostic['send_click_attempted'] else 'blocked', str(exc))


MAX_SEND_RETRIES = 5
TRANSIENT_SEND_FAILURES = frozenset({'MAA_BLACK_FRAME','MAA_CAPTURE_UNAVAILABLE','LAYOUT_CHANGED_STOP','INPUT_DESKTOP_UNAVAILABLE','SEND_FRAME_GEOMETRY_OR_CONTENT_INVALID'})

def retryable_send(result):
    diagnostic=result.get('diagnostic') or {}
    reason=result.get('reason','')
    return (result.get('status')=='blocked'
            and diagnostic.get('send_click_attempted') is False
            and diagnostic.get('draft_retained') is False
            and (reason in TRANSIENT_SEND_FAILURES or reason.startswith('MAA_CAPTURE_DIMENSIONS:')))

def send_verified(runtime, root, run_id, request, *, preflight=None, expected_chat_sha=None, refresh_row_position=False, previous_payloads=None):
    """Initial attempt plus five transient, proven effect-free retries.

    An existing/prepared draft, changed identity, or possible send click stops
    retries. Every attempt rechecks live identity and empty draft from scratch.
    The caller's durable send_started claim stays in force across all attempts.
    """
    import time,json
    try:
        from .group_policy import allowed
    except ImportError:
        from group_policy import allowed
    if not allowed(root,request,request.get('conversation_id')):
        return {'status':'blocked','reason':'GROUP_NOT_WHITELISTED','evidence':{},
                'diagnostic':{'send_click_attempted':False,'draft_retained':False}}
    attempts=[]
    for attempt in range(MAX_SEND_RETRIES+1):
        options={'refresh_row_position':True} if refresh_row_position else {}
        if previous_payloads is not None:
            options['previous_payloads'] = previous_payloads
        result=_send_verified_once(runtime,root,run_id,request,preflight=preflight,expected_chat_sha=expected_chat_sha,**options)
        attempts.append({'attempt':attempt+1,'status':result.get('status'),'reason':result.get('reason'),
                         'send_click_attempted':result.get('diagnostic',{}).get('send_click_attempted')})
        # Audit before another attempt; failure to persist prevents retry.
        path=Path(root)/'data'
        if path.exists():
            with (path/'send-attempts.jsonl').open('a',encoding='utf-8') as stream:
                stream.write(json.dumps({'event_id':request.get('event_id'),**attempts[-1]},ensure_ascii=False)+'\n')
                stream.flush()
        if not retryable_send(result) or attempt==MAX_SEND_RETRIES:break
        time.sleep(3)
    result.setdefault('diagnostic',{})['attempts']=attempts
    return result
