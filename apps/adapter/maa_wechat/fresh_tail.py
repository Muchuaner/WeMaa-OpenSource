"""Locate an incoming tail even when a long bubble removes old anchors.

Rendered minute timestamps must be later than the previous live observation,
not merely later than another bubble. Old history, same-minute uncertainty and
sidebar/OCR-only timestamps cannot establish new work. This never sends.
"""
import hashlib
import math
import re

from activity_scheduler import parse_sidebar_time
from message_vision import message_identity, message_occurrence_identity, time_identity
from watch_policy import CHINA_TIME


def locate_fresh_tail(payloads, frame, previous_seen, now):
    if (type(previous_seen) not in (int, float) or type(now) not in (int, float)
            or not math.isfinite(previous_seen) or not math.isfinite(now)
            or not 0 < previous_seen < now):
        return None
    candidates=[]
    for i,p in enumerate(payloads):
        label=(p.get('time_anchor') or {}).get('text','')
        if not re.fullmatch(r'[0-2]?\d:[0-5]\d',label) or not message_occurrence_identity(p):
            continue
        stamp=parse_sidebar_time(label,now=now,timezone=CHINA_TIME)
        if stamp is not None and previous_seen < stamp <= now:
            candidates.append((i,stamp))
    if not candidates:return None
    start=candidates[0][0]
    timed=dict(candidates)
    messages=[(i,p) for i,p in enumerate(payloads) if i>=start and p.get('direction') in ('incoming','outgoing')]
    if (not messages or any(p.get('direction')!='incoming' or p.get('message_kind')!='text'
            or not message_identity(p) or i not in timed for i,p in messages)
            or len({message_occurrence_identity(p) for _,p in messages})!=len(messages)
            or [timed[i] for i,_ in messages]!=sorted(timed[i] for i,_ in messages)):
        return None
    # Validate all three pieces against the actual captured frame, including
    # the independently agreed complete time label that owns this bubble.
    try:
        import numpy as np
        from PIL import Image
        with Image.open(frame) as image:
            if image.size!=(1920,1040):return None
            rgb=np.asarray(image.convert('RGB')).copy()
        def pixels(box,sha,*,body=False):
            if (not isinstance(box,list) or len(box)!=4 or any(type(v) is not int for v in box)):
                return False
            x,y,w,h=box
            if w<=0 or h<=0 or not (490<=x and 82<=y and x+w<=1894 and y+h<=887):return False
            crop=rgb[y:y+h,x:x+w,:3]
            hashes={hashlib.sha256(crop.tobytes()).hexdigest()}
            if body:hashes.add(hashlib.sha256(crop[:,:,::-1].copy().tobytes()).hexdigest())
            return sha in hashes
        for i,p in messages:
            v=p['visual'];a=p['time_anchor']
            clocks=[q for q in payloads[:i] if time_identity(q)
                    and q['text'].strip()==a['text'] and q['visual']['time_sha256']==a['sha256']
                    and q['visual']['time_box']==a['box']]
            if len(clocks)!=1 or not pixels(v['box'],v['sha256'],body=True):return None
            if not pixels(v['avatar_box'],v['avatar_sha256']) or not pixels(a['box'],a['sha256']):return None
    except (OSError,ValueError,TypeError,KeyError,IndexError):
        return None
    return {'status':'append','new':payloads[start:],'overlap':start,
            'metadata':{'strategy':'fresh_rendered_time_after_live_observation',
                        'previous_seen':previous_seen,'rendered_times':timed,
                        'new_tail_verbatim':True,'matched_occurrences':{}}}
