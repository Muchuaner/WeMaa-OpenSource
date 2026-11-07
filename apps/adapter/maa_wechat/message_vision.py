"""Geometry-backed Maa OCR evidence, not a spelling corrector or sender resolver.

Maa supplies screenshots and OCR hits.  This module does no mouse, OCR, network,
or database work.  Extra OCR passes must refer to the *same screenshot*, with
their boxes mapped back to screenshot coordinates (e.g. after a 2x crop).
Only identical visual evidence can excuse a changed historical short word.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import re

import numpy as np


DEFAULT_CHAT_ROI = (490, 82, 1395, 805)
# Maa sometimes reads the thin glyph 一 in a weekday separator as a dash.
# This only classifies bare centered text; a clock inside a bubble stays text.
TIME_PATTERN = re.compile(r'(?:(?:星期[一二三四五六日天—–－-]|周[一二三四五六日天]|昨天|今天)\s*)?\d{1,2}[:：]\d{2}')


def _box(hit):
    value = hit.get('box', ())
    if isinstance(value, dict):
        value = [value.get(k, 0) for k in ('x', 'y', 'w', 'h')]
    if len(value) != 4:
        return None
    try:
        x, y, w, h = [int(round(v)) for v in value]
    except (TypeError, ValueError, OverflowError):
        return None
    return [x, y, w, h] if w > 0 and h > 0 else None


def _normal_text(text):
    # Whitespace comes from line wrapping, not a Chinese-word correction rule.
    return re.sub(r'\s+', ' ', str(text)).strip()


def consensus_text(votes, *, min_agreement=2, min_score=0.80, ignore_whitespace=False):
    """Select an actually observed variant; disagreement never invokes a lexicon.

    Each vote is {text, score, pass_id}; duplicate pass IDs count only once.
    Two independent OCR preprocessing passes must agree above the score floor.
    All raw variants survive in the returned evidence.
    """
    retained = {}
    for index, vote in enumerate(votes):
        if not isinstance(vote, dict) or not isinstance(vote.get('text'), str):
            continue
        try:
            score = float(vote.get('score', 0))
        except (TypeError, ValueError):
            score = 0.0
        if not np.isfinite(score):
            score = 0.0
        item = {'text': vote['text'], 'score': max(0.0, min(1.0, score)),
                'pass_id': str(vote.get('pass_id', index))}
        prior = retained.get(item['pass_id'])
        if prior is None or item['score'] > prior['score']:
            retained[item['pass_id']] = item
    raw = list(retained.values())
    normalize = (lambda text: re.sub(r'\s+', '', text)) if ignore_whitespace else _normal_text
    counts = Counter(normalize(v['text']) for v in raw if normalize(v['text']))
    if not counts:
        return {'text': '', 'stable': False, 'agreement': 0, 'confidence': 0,
                'raw_votes': raw, 'reason': 'no_text'}
    winner, count = counts.most_common(1)[0]
    winning = [v for v in raw if normalize(v['text']) == winner]
    high = [v for v in winning if v['score'] >= min_score]
    second = max((n for t, n in counts.items() if t != winner), default=0)
    stable = count > second and len(high) >= min_agreement
    selected = max(winning, key=lambda v: v['score'])
    return {'text': selected['text'], 'stable': stable, 'agreement': count,
            'confidence': min((v['score'] for v in high), default=0),
            'raw_votes': raw,
            'reason': 'agreement' if stable else 'insufficient_or_conflicting_ocr'}


def _image_rgb(image, color_order):
    value = np.asarray(image)
    if value.ndim != 3 or value.shape[2] < 3 or value.dtype != np.uint8:
        raise ValueError('MESSAGE_VISION_REQUIRES_UINT8_COLOR_IMAGE')
    if color_order not in ('BGR', 'RGB'):
        raise ValueError('MESSAGE_VISION_BAD_COLOR_ORDER')
    return value[:, :, :3][:, :, ::-1] if color_order == 'BGR' else value[:, :, :3]


def _fill_component_box(rgb, seed, fill, rect, roi):
    """Exact flat-fill connected component, excluding disconnected nearby avatars.

    A run/span flood fill avoids per-pixel Python traversal.  Text creates holes
    but bubble padding remains connected.  Recovering the entire rounded shape
    makes hashes independent of which OCR padding probe happened to succeed.
    """
    x, y, w, h = rect
    rx, ry, rw, rh = roi
    left, top = max(rx, x-12), max(ry, y-12)
    right, bottom = min(rx+rw, x+w+12), min(ry+rh, y+h+12)
    mask = np.max(np.abs(rgb[top:bottom, left:right].astype(np.int16)-fill), axis=2) <= 3
    sx, sy = seed[0]-left, seed[1]-top
    if not (0 <= sx < mask.shape[1] and 0 <= sy < mask.shape[0] and mask[sy, sx]):
        return rect
    visited = np.zeros(mask.shape, dtype=bool)
    pending = [(sx, sy)]
    x0 = x1 = sx
    y0 = y1 = sy
    while pending:
        px, py = pending.pop()
        if visited[py, px] or not mask[py, px]:
            continue
        a = b = px
        while a > 0 and mask[py, a-1] and not visited[py, a-1]:
            a -= 1
        while b+1 < mask.shape[1] and mask[py, b+1] and not visited[py, b+1]:
            b += 1
        visited[py, a:b+1] = True
        x0, x1, y0, y1 = min(x0, a), max(x1, b), min(y0, py), max(y1, py)
        for ny in (py-1, py+1):
            if not 0 <= ny < mask.shape[0]:
                continue
            segment = mask[ny, a:b+1] & ~visited[ny, a:b+1]
            starts = np.flatnonzero(segment & np.concatenate(([True], ~segment[:-1])))
            pending.extend((a+int(i), ny) for i in starts)
    return [left+x0, top+y0, x1-x0+1, y1-y0+1]


def _bubble_for_hit(rgb, rect, roi, background):
    """Recover a flat-filled bubble from padding pixels, rejecting bare labels.

    Tested light theme geometry: inbound is neutral gray/white anchored near
    the left avatar; outbound is green anchored near the right avatar.  Other
    themes/layouts deliberately produce unknown rather than guessing direction.
    """
    x, y, w, h = rect
    rx, ry, rw, rh = roi
    probes = [(x+w//2, y-offset) for offset in (3, 5, 7)]
    probes += [(x-offset, y+h//2) for offset in (4, 7, 9)]
    probes += [(x+w+offset, y+h//2) for offset in (3, 6)]
    candidates = []
    for px, py in probes:
        if not (rx <= px < rx+rw and ry <= py < ry+rh):
            continue
        fill = rgb[py, px].astype(np.int16)
        neutral = (fill.max()-fill.min() <= 8 and fill.min() >= 205
                   and np.max(np.abs(fill-background)) >= 6)
        green = (fill[1] >= 170 and fill[1]-fill[0] >= 25
                 and fill[1]-fill[2] >= 30 and fill[0] >= 80)
        if not (neutral or green):
            continue
        # Padding rows avoid letter holes. Stop at the first actual background;
        # unlike perceptual hashes this cannot equate different Chinese glyphs.
        row = np.max(np.abs(rgb[py, rx:rx+rw].astype(np.int16)-fill), axis=1) <= 3
        left = right = px-rx
        while left > 0 and row[left-1]:
            left -= 1
        while right+1 < rw and row[right+1]:
            right += 1
        left, right = left+rx, right+rx+1
        if not (left <= x and x+w <= right) or right-left < 22:
            continue
        # A side padding column is intact even for multiline text.
        column_x = left+4
        column = np.max(np.abs(rgb[ry:ry+rh, column_x].astype(np.int16)-fill), axis=1) <= 3
        top = bottom = py-ry
        while top > 0 and column[top-1]:
            top -= 1
        while bottom+1 < rh and column[bottom+1]:
            bottom += 1
        top, bottom = top+ry, bottom+ry+1
        if not (top <= y and y+h <= bottom) or bottom-top < 22:
            continue
        canonical = _fill_component_box(rgb, (px, py), fill,
                                        [left, top, right-left, bottom-top], roi)
        left, top, width, height = canonical
        right, bottom = left+width, top+height
        # Cut-off bubbles are not safe evidence for sending a reply.
        if left <= rx or right >= rx+rw or top <= ry or bottom >= ry+rh:
            continue
        direction = ('incoming' if neutral and 35 <= left-rx <= 115 else
                     'outgoing' if green and 10 <= rx+rw-right <= 100 else 'unknown')
        if direction == 'unknown':
            continue
        crop = rgb[top:bottom, left:right]
        fraction = float((np.max(np.abs(crop.astype(np.int16)-fill), axis=2) <= 3).mean())
        if fraction < 0.55:
            continue
        candidates.append({'box': [left, top, right-left, bottom-top],
                           'direction': direction, 'fill_fraction': fraction})
    if not candidates:
        return None
    # Prefer the complete, largest padding rectangle, not a text-internal patch.
    return max(candidates, key=lambda item: item['box'][2]*item['box'][3])


def _assemble_hits(hits):
    rows = []
    for hit in sorted(hits, key=lambda v: (_box(v)[1], _box(v)[0])):
        x, y, w, h = _box(hit)
        center = y+h/2
        if rows and abs(center-rows[-1]['center']) <= max(5, h/3):
            rows[-1]['hits'].append(hit)
        else:
            rows.append({'center': center, 'hits': [hit]})
    text = '\n'.join(' '.join(v['text'] for v in sorted(row['hits'], key=lambda v: _box(v)[0]))
                     for row in rows)
    scores = []
    for hit in hits:
        try:
            score = float(hit.get('score', 0))
        except (ValueError, TypeError):
            score = 0
        scores.append(score if np.isfinite(score) else 0)
    return text, min(scores, default=0)


def _rect_union(rects):
    left = min(r[0] for r in rects)
    top = min(r[1] for r in rects)
    right = max(r[0]+r[2] for r in rects)
    bottom = max(r[1]+r[3] for r in rects)
    return [left, top, right-left, bottom-top]


def _same_bare_label(a, b):
    """Same physical label across passes, including small OCR box jitter.

    Text is deliberately not part of this comparison: conflicting hypotheses
    must become one unstable observation rather than two apparent messages.
    Callers never combine two different hits from the same OCR pass.
    """
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    overlap_x = max(0, min(ax+aw, bx+bw)-max(ax, bx))
    overlap_y = max(0, min(ay+ah, by+bh)-max(ay, by))
    return (overlap_x >= .35*min(aw, bw) and overlap_y >= .5*min(ah, bh)
            and abs(ay+ah/2-by-bh/2) <= 6
            and abs(ax+aw/2-bx-bw/2) <= max(12, .35*max(aw, bw)))


def _nickname_observation(rgb, passes, bubble, roi, background):
    """Read only the bare nickname row immediately above this bubble."""
    x, y, w, h = bubble
    votes, raw = [], []
    for pass_index, hits in enumerate(passes):
        labels = []
        for hit in hits:
            rect = _box(hit) if isinstance(hit, dict) else None
            if not rect or not isinstance(hit.get('text'), str) or not hit['text'].strip():
                continue
            hx, hy, hw, hh = rect
            if (x-4 <= hx <= x+30 and hx+hw <= x+360 and hh <= 24
                    and 1 <= y-(hy+hh) <= 24
                    and not TIME_PATTERN.fullmatch(hit['text'].strip())
                    and _bubble_for_hit(rgb, rect, roi, background) is None):
                labels.append(hit)
        if not labels or max(_box(v)[1] for v in labels)-min(_box(v)[1] for v in labels) > 6:
            continue
        text, score = _assemble_hits(labels)
        votes.append({'text': text, 'score': score, 'pass_id': pass_index})
        raw.extend({'pass_id': pass_index, 'text': v['text'], 'box': _box(v),
                    'score': v.get('score', 0)} for v in labels)
    result = consensus_text(votes, min_score=.85, ignore_whitespace=True)
    result.update(source='same_frame_nickname_row', raw_hits=raw)
    if raw:
        result['box'] = _rect_union([v['box'] for v in raw])
    return result


def _avatar_evidence(rgb, payload, roi, background, group_chat):
    """A complete 36px avatar beside a verified bubble in the tested layout.

    This is an exact same-frame pixel identity, never a wxid or a contact-list
    avatar. Missing/cut-off/unfamiliar geometry gives no avatar evidence.
    """
    visual = payload.get('visual') or {}
    if not visual.get('verified') or payload.get('direction') not in ('incoming', 'outgoing'):
        return
    x, y, w, h = visual['box']
    rx, ry, rw, rh = roi
    incoming = payload['direction'] == 'incoming'
    ax = rx+10 if incoming else rx+rw-27
    if not (0 <= ax and ax+36 <= rgb.shape[1]):
        return
    # Incoming group avatars are aligned with the nickname row. Outgoing and
    # private avatars are aligned with the top of the message bubble.
    expected = y-21 if incoming and group_chat else y
    top, bottom = max(ry, expected-10), min(ry+rh, expected+46)
    if top >= bottom:
        return
    band = rgb[top:bottom, ax:ax+36].astype(np.int16)
    detailed = np.max(np.abs(band-background), axis=2) > 12
    rows = np.flatnonzero(detailed[:, 3:33].sum(axis=1) >= 20)
    runs = []
    for value in rows:
        if runs and value == runs[-1][-1]+1:
            runs[-1].append(int(value))
        else:
            runs.append([int(value)])
    candidates = []
    for run in runs:
        ay, height = top+run[0], len(run)
        if height != 36 or abs(ay-expected) > 8 or ay <= ry or ay+36 >= ry+rh:
            continue
        crop = np.ascontiguousarray(rgb[ay:ay+36, ax:ax+36])
        mask = np.max(np.abs(crop.astype(np.int16)-background), axis=2) > 12
        # A nickname/clock glyph row or a flat bubble cannot satisfy a square,
        # detailed avatar component with clean space immediately above/below.
        if mask.mean() < .55 or float(crop.astype(np.float32).std()) < 8:
            continue
        if (detailed[run[0]-1].sum() > 6 if run[0] > 0 else True):
            continue
        if (detailed[run[-1]+1].sum() > 6 if run[-1]+1 < len(detailed) else True):
            continue
        candidates.append((ay, crop))
    if len(candidates) != 1:
        return
    ay, crop = candidates[0]
    visual.update(avatar_box=[ax, ay, 36, 36],
                  avatar_sha256=hashlib.sha256(crop.tobytes()).hexdigest(),
                  avatar_verified=True, avatar_color_order='RGB',
                  avatar_source='same_frame_message_avatar')


def _time_evidence(rgb, payload, roi, background):
    """Canonical exact pixels of a complete, bare centered timestamp.

    The original OCR crop remains untouched. A separate ink bounding box
    excludes variable OCR padding and never makes a timestamp a replyable
    message. OCR must already agree across independent same-frame passes.
    """
    visual = payload.get('visual') or {}
    for key in ('time_box','time_sha256','time_color_order','time_source'):
        visual.pop(key,None)
    visual['time_verified']=False
    box=visual.get('box')
    if (payload.get('direction')!='time' or payload.get('message_kind')!='time'
            or payload.get('ocr',{}).get('stable') is not True
            or not TIME_PATTERN.fullmatch(str(payload.get('text','')).strip())
            or not isinstance(box,list) or len(box)!=4 or any(type(v) is not int for v in box)):
        return
    x,y,w,h=box;rx,ry,rw,rh=roi
    if (not (10<=w<=160 and 6<=h<=30) or abs(x+w/2-(rx+rw/2))>40
            or not (rx<=x and ry<=y and x+w<=rx+rw and y+h<=ry+rh)):
        return
    left,top=max(rx,x-10),max(ry,y-10)
    right,bottom=min(rx+rw,x+w+10),min(ry+rh,y+h+10)
    patch=rgb[top:bottom,left:right].astype(np.int16)
    mask=np.max(np.abs(patch-background),axis=2)>6
    yy,xx=np.nonzero(mask)
    if len(xx)<12:return
    a,b,c,d=int(xx.min()),int(yy.min()),int(xx.max())+1,int(yy.max())+1
    # No content may touch the search margin: a partial timestamp, adjacent
    # bubble or overlay cannot become an exact history anchor.
    if min(a,b,patch.shape[1]-c,patch.shape[0]-d)<2:return
    width,height=c-a,d-b
    if not (10<=width<=150 and 6<=height<=26):return
    ink=patch[mask]
    if np.any(ink.max(axis=1)-ink.min(axis=1)>8):return
    if float(mask[b:d,a:c].mean())>.80:return
    crop=np.ascontiguousarray(rgb[top+b:top+d,left+a:left+c])
    visual.update(time_box=[left+a,top+b,width,height],
                  time_sha256=hashlib.sha256(crop.tobytes()).hexdigest(),
                  time_verified=True,time_color_order='RGB',
                  time_source='same_frame_centered_time_pixels')


def verify_time_anchors(image, payloads, *, color_order='BGR', chat_roi=DEFAULT_CHAT_ROI):
    """Upgrade stored same-frame OCR evidence without OCR, input or mutation.

    Callers must supply the original screenshot for these payloads. This only
    verifies timestamp pixels; it preserves every message and reply-safety flag.
    """
    rgb=_image_rgb(image,color_order)
    rx,ry,rw,rh=[int(v) for v in chat_roi]
    if min(rx,ry)<0 or rw<1 or rh<1 or rx+rw>rgb.shape[1] or ry+rh>rgb.shape[0]:
        raise ValueError('MESSAGE_VISION_BAD_ROI')
    roi=(rx,ry,rw,rh)
    background=np.median(rgb[ry:ry+rh,rx:rx+3].reshape(-1,3),axis=0).astype(np.int16)
    result=[]
    for payload in payloads:
        item=dict(payload,visual=dict(payload.get('visual') or {}))
        if item.get('direction')=='time' or item.get('message_kind')=='time':
            _time_evidence(rgb,item,roi,background)
        result.append(item)
    return _attach_time_anchors(result)


def time_identity(payload):
    visual=payload.get('visual') or {}
    box=visual.get('time_box') or []
    sha=visual.get('time_sha256')
    if (payload.get('direction')!='time' or payload.get('message_kind')!='time'
            or payload.get('ocr',{}).get('stable') is not True or visual.get('time_verified') is not True
            or len(box)!=4 or any(type(v) is not int for v in box) or min(box[2:])<=0
            or not isinstance(sha,str) or not re.fullmatch('[a-f0-9]{64}',sha)
            or not TIME_PATTERN.fullmatch(str(payload.get('text','')).strip())):
        return None
    return (str(payload['text']).strip(),sha,box[2],box[3])


def _attach_time_anchors(payloads):
    """Only the first verified message after a complete time owns that time."""
    last=None
    for payload in payloads:
        payload.pop('time_anchor',None)
        if payload.get('direction')=='time' or payload.get('message_kind')=='time':
            # An intervening unverified clock is a context boundary too; it
            # cannot cause us to borrow an older, different verified time.
            last=payload if time_identity(payload) else None
        elif message_identity(payload):
            visual=payload['visual'];avatar=visual.get('avatar_box')
            if last and visual.get('avatar_verified') is True and isinstance(avatar,list) and len(avatar)==4:
                time_box=last['visual']['time_box'];message_box=visual['box']
                if 0<=message_box[1]-time_box[1]-time_box[3]<=85:
                    payload['time_anchor']={'text':last['text'],'sha256':last['visual']['time_sha256'],
                        'box':list(time_box),'verified':True,'ocr_stable':True,
                        'relative_message':[message_box[0]-time_box[0],message_box[1]-time_box[1]],
                        'relative_avatar':[avatar[0]-time_box[0],avatar[1]-time_box[1]],
                        'source':'same_frame_centered_time_pixels'}
            # Never borrow a timestamp through another verified bubble.
            last=None
    return payloads


def message_occurrence_identity(payload):
    """Exact time + bubble + portrait + relative layout, never a person ID."""
    message=message_identity(payload)
    visual=payload.get('visual') or {}
    avatar=visual.get('avatar_box') or []
    sha=visual.get('avatar_sha256')
    time=payload.get('time_anchor') or {}
    box=time.get('box') or []
    if (not message or visual.get('avatar_verified') is not True or len(avatar)!=4
            or any(type(v) is not int for v in avatar) or min(avatar[2:])<=0
            or not isinstance(sha,str) or not re.fullmatch('[a-f0-9]{64}',sha)
            or time.get('verified') is not True or time.get('ocr_stable') is not True or len(box)!=4
            or any(type(v) is not int for v in box) or min(box[2:])<=0
            or not isinstance(time.get('sha256'),str) or not re.fullmatch('[a-f0-9]{64}',time['sha256'])
            or not TIME_PATTERN.fullmatch(str(time.get('text','')).strip())
            or len(time.get('relative_message') or [])!=2 or len(time.get('relative_avatar') or [])!=2):
        return None
    if (time['relative_message']!=[visual['box'][0]-box[0],visual['box'][1]-box[1]]
            or time['relative_avatar']!=[avatar[0]-box[0],avatar[1]-box[1]]):
        return None
    return (message,sha,avatar[2],avatar[3],str(time['text']).strip(),time['sha256'],box[2],box[3],
            tuple(time['relative_message']),tuple(time['relative_avatar']))


def extract_message_payloads(image, ocr_hits, *, color_order='BGR',
                             chat_roi=DEFAULT_CHAT_ROI, consensus_passes=None,
                             group_chat=False):
    """Return bubble-level text + raw OCR evidence in visible chronological order.

    `consensus_passes` contains lists of additional same-frame OCR hits.  Their
    coordinates must already be mapped back after cropping/upscaling.  Baseline
    hits plus one matching high-confidence extra pass can establish OCR stability;
    no extra pass means reply_safe=False.  Pixel SHA excludes screen coordinates
    so a scrolled but unchanged bubble still anchors history.
    """
    rgb = _image_rgb(image, color_order)
    rx, ry, rw, rh = [int(v) for v in chat_roi]
    if min(rx, ry) < 0 or rw < 1 or rh < 1 or rx+rw > rgb.shape[1] or ry+rh > rgb.shape[0]:
        raise ValueError('MESSAGE_VISION_BAD_ROI')
    roi = (rx, ry, rw, rh)
    background = np.median(rgb[ry:ry+rh, rx:rx+3].reshape(-1, 3), axis=0).astype(np.int16)
    groups = {}
    passes = [ocr_hits] + list(consensus_passes or [])
    for pass_index, hits in enumerate(passes):
        for hit in sorted(hits, key=lambda h: ((_box(h) or [0, 0, 0, 0])[1],
                                              (_box(h) or [0, 0, 0, 0])[0])):
            rect = _box(hit) if isinstance(hit, dict) else None
            if not rect or not isinstance(hit.get('text'), str) or not hit['text'].strip():
                continue
            x, y, w, h = rect
            if not (rx <= x and ry <= y and x+w <= rx+rw and y+h <= ry+rh):
                continue
            bubble = _bubble_for_hit(rgb, rect, roi, background)
            if bubble:
                key = tuple(bubble['box'])
                group = None
                for candidate in groups.values():
                    if candidate['direction'] != bubble['direction']:
                        continue
                    ax, ay, aw, ah = candidate['box']
                    bx, by, bw, bh = bubble['box']
                    if max(abs(ax-bx), abs(ay-by), abs(ax+aw-bx-bw), abs(ay+ah-by-bh)) <= 4:
                        group = candidate
                        left, top = min(ax, bx), min(ay, by)
                        group['box'] = [left, top, max(ax+aw, bx+bw)-left,
                                        max(ay+ah, by+bh)-top]
                        break
                if group is None:
                    group = groups.setdefault(key, dict(bubble, passes={}, raw=[]))
            else:
                # Bare text does not inherit direction from its horizontal edge.
                enclosing = [g for g in groups.values()
                             if g['direction'] in ('incoming', 'outgoing')
                             and g['box'][0] <= x and g['box'][1] <= y
                             and x+w <= g['box'][0]+g['box'][2]
                             and y+h <= g['box'][1]+g['box'][3]]
                if len(enclosing) == 1:
                    group = enclosing[0]
                else:
                    key = ('bare', x, y, w, h, _normal_text(hit['text']))
                    group = next((g for g in groups.values()
                                  if g['direction'] == 'unknown' and pass_index not in g['passes']
                                  and _same_bare_label(g['box'], rect)), None)
                    if group is None:
                        group = groups.setdefault(key, {'box': rect, 'direction': 'unknown',
                                                        'passes': {}, 'raw': []})
                    else:
                        group['box'] = _rect_union([group['box'], rect])
            group['passes'].setdefault(pass_index, []).append(hit)
            group['raw'].append({'pass_id': pass_index, 'text': hit['text'],
                                 'box': rect, 'score': hit.get('score', 0)})
    payloads = []
    for group in sorted(groups.values(), key=lambda g: (g['box'][1], g['box'][0])):
        votes = []
        for pass_index, hits in group['passes'].items():
            text, score = _assemble_hits(hits)
            votes.append({'text': text, 'score': score, 'pass_id': pass_index})
        consensus = consensus_text(votes)
        x, y, w, h = group['box']
        direction = group['direction']
        kind = 'text' if direction in ('incoming', 'outgoing') else 'unknown'
        if kind == 'unknown' and TIME_PATTERN.fullmatch(consensus['text'].strip()) and abs(x+w/2-(rx+rw/2)) <= 180:
            kind, direction = 'time', 'time'
        if re.fullmatch(r'\[(?:图片|视频|语音|文件|表情|位置|名片|链接)\]', consensus['text'].strip()):
            kind = 'unsupported'
        verified = direction in ('incoming', 'outgoing')
        crop = np.ascontiguousarray(rgb[y:y+h, x:x+w])
        visual = {'sha256': hashlib.sha256(crop.tobytes()).hexdigest(),
                  'box': [x, y, w, h], 'verified': verified,
                  'fill_fraction': group.get('fill_fraction'), 'avatar_verified': False}
        payloads.append({'text': consensus['text'], 'sender': 'unresolved',
                         'source': 'maa_bubble_ocr', 'direction': direction,
                         'message_kind': kind, 'visual': visual,
                         'ocr': dict(consensus, raw_hits=group['raw']),
                         'reply_safe': direction == 'incoming' and kind == 'text' and consensus['stable'],
                         'group_sender_name': None, 'group_sender_verified': False})
    if group_chat:
        for payload in payloads:
            if payload['direction'] != 'incoming':
                continue
            sender = _nickname_observation(rgb, passes, payload['visual']['box'], roi, background)
            payload['group_sender_name'] = sender['text'] or None
            payload['group_sender_verified'] = sender['stable']
            payload['group_sender_ocr'] = sender
            # A nickname is only OCR-verified display text, never a stable wxid.
            payload['reply_safe'] = payload['reply_safe'] and sender['stable']
    if rgb.shape==(1040,1920,3):
        try:
            from .media_vision import media_payloads
        except ImportError:
            from media_vision import media_payloads
        opaque=media_payloads(rgb[:,:,::-1],payloads)
        if group_chat:
            for p in opaque:
                if p['direction']!='incoming':continue
                sender=_nickname_observation(rgb,passes,p['visual']['box'],roi,background)
                p['group_sender_name']=sender['text'] or None;p['group_sender_verified']=sender['stable']
                p['group_sender_ocr']=sender
        if opaque:
            # OCR inside a photograph is content, not a separate chat message.
            payloads=[p for p in payloads if not any(
                p['visual']['box'][0]>=m['visual']['box'][0] and p['visual']['box'][1]>=m['visual']['box'][1]
                and p['visual']['box'][0]+p['visual']['box'][2]<=m['visual']['box'][0]+m['visual']['box'][2]
                and p['visual']['box'][1]+p['visual']['box'][3]<=m['visual']['box'][1]+m['visual']['box'][3]
                for m in opaque)]
            payloads=sorted(payloads+opaque,key=lambda p:(p['visual']['box'][1],p['visual']['box'][0]))
    for payload in payloads:
        _avatar_evidence(rgb, payload, roi, background, group_chat)
        if payload['message_kind']=='time':
            _time_evidence(rgb,payload,roi,background)
    # Preserve all OCR evidence, while explicitly separating nickname/portrait
    # OCR from messages. These labels never acquire a sender or reply permission.
    for label in payloads:
        if label['direction'] != 'unknown':
            continue
        box = label['visual']['box']
        for message in payloads:
            nickname_box = (message.get('group_sender_ocr') or {}).get('box')
            if nickname_box and _same_bare_label(box, nickname_box):
                label['message_kind'] = 'sender_label'
                label['label_for_visual_sha256'] = message['visual']['sha256']
                break
            avatar_box = message['visual'].get('avatar_box')
            # Full-frame OCR may put a 4-10px padding margin around a portrait
            # it hallucinates as "8". Keep that raw evidence attached to the
            # verified portrait, never as a separate chat bubble.
            if avatar_box and (avatar_box[0]-12 <= box[0] and avatar_box[1]-12 <= box[1]
                               and box[0]+box[2] <= avatar_box[0]+avatar_box[2]+12
                               and box[1]+box[3] <= avatar_box[1]+avatar_box[3]+12):
                label['message_kind'] = 'avatar_ocr'
                label['label_for_visual_sha256'] = message['visual']['sha256']
                break
    return _attach_time_anchors(payloads)


def message_identity(payload):
    """Immutable visual token; use this rather than hashing noisy OCR metadata."""
    if not isinstance(payload, dict):
        return None
    visual = payload.get('visual') or {}
    box = visual.get('box') or []
    sha = visual.get('sha256', '')
    if (payload.get('direction') not in ('incoming', 'outgoing')
            or visual.get('verified') is not True or len(box) != 4
            or not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{64}', sha)):
        return None
    return (payload['direction'], sha, box[2], box[3])


def _reconcile_time_sequence(previous,current,old,new):
    """Unique suffix/prefix of at least two exact timed message occurrences."""
    a=[message_occurrence_identity(previous[i]) for i,_ in old]
    b=[message_occurrence_identity(current[i]) for i,_ in new]
    candidates=[]
    for length in range(2,min(len(a),len(b))+1):
        anchor=b[:length]
        if any(v is None for v in anchor) or a[-length:]!=anchor:continue
        # Two separately rendered times are mandatory. Repeated same-minute
        # greetings, or a time label alone, cannot locate a history occurrence.
        if len({v[4:8] for v in anchor})<2:continue
        if (sum(a[i:i+length]==anchor for i in range(len(a)-length+1))!=1
                or sum(b[i:i+length]==anchor for i in range(len(b)-length+1))!=1):
            continue
        matched=list(zip(old[-length:],new[:length]))
        translations=[]
        for (oi,_),(ni,_) in matched:
            before,after=previous[oi],current[ni]
            for left,right in ((before['visual']['box'],after['visual']['box']),
                               (before['visual']['avatar_box'],after['visual']['avatar_box']),
                               (before['time_anchor']['box'],after['time_anchor']['box'])):
                if left[2:]!=right[2:]:break
                translations.append((right[0]-left[0],right[1]-left[1]))
            else:continue
            break
        else:
            if len(set(translations))!=1:continue
            end=new[length-1][0]+1
            candidates.append({'status':'append' if len(new)>length else 'unchanged',
                'new':current[end:] if len(new)>length else [],'overlap':end if len(new)>length else len(current),
                'metadata':{'strategy':'exact_time_bubble_avatar_sequence','visual_anchors':length,
                    'translation':list(translations[0]),'new_tail_verbatim':True,
                    'matched_occurrences':{str(ni):oi for (oi,_),(ni,_) in matched},
                    'time_anchors':[{'previous_index':oi,'current_index':ni,
                                     'text':current[ni]['time_anchor']['text'],
                                     'sha256':current[ni]['time_anchor']['sha256']}
                                    for (oi,_),(ni,_) in matched]}})
    if len(candidates)==1:return candidates[0]
    if len(candidates)>1:
        return {'status':'ambiguous','new':[],'overlap':0,
                'metadata':{'strategy':'exact_time_bubble_avatar_sequence','reason':'multiple_overlaps'}}
    return None


def _verified_avatar_identity(payload):
    visual=payload.get('visual') or {}
    box=visual.get('avatar_box') or []
    sha=visual.get('avatar_sha256')
    if (visual.get('avatar_verified') is not True or len(box)!=4
            or any(type(v) is not int for v in box) or min(box[2:])<=0
            or not isinstance(sha,str) or not re.fullmatch('[a-f0-9]{64}',sha)):
        return None
    return (sha,box[2],box[3])


def _matched_evidence_compatible(previous,current,matched):
    """Missing legacy fields may pass; positive conflicting evidence never does."""
    translations=[]
    timed=False
    for (oi,_),(ni,_) in matched:
        before,after=previous[oi],current[ni]
        left,right=before['visual']['box'],after['visual']['box']
        translation=(right[0]-left[0],right[1]-left[1])
        translations.append(translation)
        old_avatar,new_avatar=_verified_avatar_identity(before),_verified_avatar_identity(after)
        if old_avatar and new_avatar:
            if old_avatar!=new_avatar:return False
            left,right=before['visual']['avatar_box'],after['visual']['avatar_box']
            if (right[0]-left[0],right[1]-left[1])!=translation:return False
        old_time,new_time=message_occurrence_identity(before),message_occurrence_identity(after)
        if old_time and new_time:
            if old_time!=new_time:return False
            timed=True
            left,right=before['time_anchor']['box'],after['time_anchor']['box']
            translations.append((right[0]-left[0],right[1]-left[1]))
    # The strict timestamp strategy may have failed its one-translation gate.
    # A body-only fallback must not bypass that known geometric contradiction.
    return not timed or len(set(translations))==1


def _verified_time_separator_reflow(previous,current,matched):
    """微信补绘时间行可改变局部位移；正文和身份必须另有原生强证据。"""
    if len(matched)<3:return False
    shifted=stable_time=False
    horizontal=set()
    for (oi,_),(ni,_) in matched:
        before,after=previous[oi],current[ni]
        if before.get('text')!=after.get('text') or not before.get('text'):return False
        for value in (before,after):
            proof=value.get('text_copy') or {}
            if proof.get('verified') is not True or proof.get('method')!='native_context_menu':return False
        if not _verified_avatar_identity(before) or _verified_avatar_identity(before)!=_verified_avatar_identity(after):return False
        left,right=before['visual']['box'],after['visual']['box']
        avatar_left,avatar_right=before['visual']['avatar_box'],after['visual']['avatar_box']
        delta=(right[0]-left[0],right[1]-left[1])
        if (avatar_right[0]-avatar_left[0],avatar_right[1]-avatar_left[1])!=delta:return False
        horizontal.add(delta[0])
        old_time,new_time=message_occurrence_identity(before),message_occurrence_identity(after)
        if old_time and new_time and old_time!=new_time:return False
    if len(horizontal)!=1:return False
    def gap(items,start,end):
        values=items[start+1:end]
        if any(p.get('message_kind') not in ('time','avatar_ocr','sender_label') for p in values):return None
        times=[p for p in values if p.get('message_kind')=='time']
        if len(times)>1 or any(not time_identity(p) for p in times):return None
        left,right=items[start]['visual']['box'],items[end]['visual']['box']
        space=right[1]-left[1]-left[3]
        if times:
            t=times[0]['visual']['time_box']
            # 已在真实帧验证的微信标准时间行：29px + 9px 字形 +23px。
            # 只解释明确的 41px 时间行增删；任意气泡移动仍拒绝。
            if t[1]-left[1]-left[3]!=29 or t[3]!=9 or right[1]-t[1]-t[3]!=23:return None
        return space,times
    for ((oa,_),(na,_)),((ob,_),(nb,_)) in zip(matched,matched[1:]):
        old_gap,new_gap=gap(previous,oa,ob),gap(current,na,nb)
        if old_gap is None or new_gap is None:return False
        old_space,old_times=old_gap;new_space,new_times=new_gap
        if len(old_times)==len(new_times):
            if old_space!=new_space:return False
            if old_times:
                if time_identity(old_times[0])!=time_identity(new_times[0]):return False
                stable_time=True
        else:
            if old_space-41*len(old_times)!=20 or new_space-41*len(new_times)!=20:return False
            shifted=True
    return shifted and stable_time


def reconcile_visual(previous, current, strict_result=None):
    """Reconcile a unique ordered overlap of exact bubble pixels, or return None.

    At least two consecutive unique visual anchors are required. Exact verified
    time + bubble + portrait sequences can distinguish repeated bodies, only
    with a unique suffix/prefix and one exact translation for every component.
    Changed short historical OCR is tolerated *only* with identical glyph pixels;
    text differences in the new tail never influence overlap or get rewritten.
    Legacy line-only baselines return None and must be upgraded without replies.
    """
    if previous is None or not isinstance(previous, list) or not isinstance(current, list):
        return None
    def anchors(items):
        # A recognizer may find both a whole card and a narrower rectangle
        # inside it. They occupy the same physical bubble, not two messages.
        # Ignore only a strictly enclosed rectangle of the same direction and
        # vertical span; keep the exact pixels of the containing bubble.
        candidates = [(i, message_identity(p)) for i, p in enumerate(items) if message_identity(p)]
        result = []
        for i, key in candidates:
            x,y,w,h = items[i]['visual']['box']
            shadow = False
            for j, other in candidates:
                if i == j or key[0] != other[0]: continue
                ax,ay,aw,ah = items[j]['visual']['box']
                if (abs(y-ay)<=4 and abs(y+h-ay-ah)<=4 and ax<=x and x+w<=ax+aw
                        and aw>w and ah>=h):
                    shadow = True; break
            if not shadow: result.append((i,key))
        return result
    old = anchors(previous)
    new = anchors(current)
    if not old or not new:
        return None
    a, b = [key for _, key in old], [key for _, key in new]
    timed=_reconcile_time_sequence(previous,current,old,new)
    if timed:return timed
    if a == b:
        # A viewport full of repeated identical bodies can have scrolled to
        # later occurrences while keeping exactly the same bubble sequence.
        # Conflicting portraits, timestamps or their geometry must never be
        # ignored even when two distinct body glyphs occupy the same sequence.
        pairs=list(zip(old,new))
        reflow=not _matched_evidence_compatible(previous,current,pairs) and _verified_time_separator_reflow(previous,current,pairs)
        if len(set(a))<2 or not (_matched_evidence_compatible(previous,current,pairs) or reflow):
            return None
        # OCR noise or new bare timestamps are not newly arrived user messages.
        return {'status': 'unchanged', 'new': [], 'overlap': len(current),
                'metadata': {'strategy': 'verified_native_time_separator_reflow' if reflow else 'exact_bubble_pixels', 'visual_anchors': len(a),
                             'matched_occurrences':{str(ni):oi for (oi,_),(ni,_) in zip(old,new)}}}
    candidates = []
    for length in range(2, min(len(a), len(b))+1):
        anchor = b[:length]
        if a[-length:] != anchor:
            continue
        if len(set(anchor)) < 2:
            # Counts alone cannot locate a run of visually identical greetings
            # after scrolling. Require at least two distinguishable bubbles.
            continue
        old_occurrences = sum(a[i:i+length] == anchor for i in range(len(a)-length+1))
        new_occurrences = sum(b[i:i+length] == anchor for i in range(len(b)-length+1))
        # An identical sequence elsewhere cannot locate this occurrence safely.
        if old_occurrences != 1 or new_occurrences != 1:
            continue
        matched=list(zip(old[-length:],new[:length]))
        compatible=_matched_evidence_compatible(previous,current,matched)
        reflow=not compatible and _verified_time_separator_reflow(previous,current,matched)
        if not compatible and not reflow:continue
        end = new[length-1][0]+1
        historical_noise = []
        for (oi, _), (ni, _) in matched:
            if previous[oi].get('text') != current[ni].get('text'):
                historical_noise.append({'previous_index': oi, 'current_index': ni,
                                         'previous_text': previous[oi].get('text'),
                                         'current_text': current[ni].get('text'),
                                         'proof': 'identical_bubble_pixels'})
        candidates.append({'status': 'append' if len(b) > length else 'unchanged',
                           'new': current[end:] if len(b) > length else [], 'overlap': end,
                           'metadata': {'strategy': 'verified_native_time_separator_reflow' if reflow else 'exact_bubble_pixels', 'visual_anchors': length,
                                        'historical_ocr_variants': historical_noise,
                                        'new_tail_verbatim': True,
                                        'matched_occurrences':{str(ni):oi for (oi,_),(ni,_) in matched}}})
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        return {'status': 'ambiguous', 'new': [], 'overlap': 0,
                'metadata': {'strategy': 'exact_bubble_pixels', 'reason': 'multiple_overlaps'}}
    return None
