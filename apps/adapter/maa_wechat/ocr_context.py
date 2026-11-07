"""Conservative OCR-only recovery of an otherwise missing historical overlap.

This is not a fuzzy message deduplicator.  Only aligned *historical* rows may
have a few OCR spelling/punctuation defects, supported by several unique exact
anchors.  The new tail is returned verbatim.  Callers must supply the strict
reconciliation result; baseline, unchanged and ambiguous results are untouched.
"""
from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
import json
import re


MIN_OVERLAP = 8
MIN_LONG_TEXT = 24
MAX_NOISY_ROWS = 3
MIN_SIMILARITY = 0.97
STRATEGY = 'unique_exact_anchors_with_bounded_historical_ocr_noise'


def _key(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _valid_payload(payload):
    return (isinstance(payload, dict) and isinstance(payload.get('text'), str)
            and isinstance(payload.get('sender'), str)
            and isinstance(payload.get('source'), str))


def _same_context(left, right):
    # Do not erase or conflate a sender, source, or any future structured field.
    return ({k: v for k, v in left.items() if k != 'text'}
            == {k: v for k, v in right.items() if k != 'text'})


def _time_anchor(text):
    match = re.fullmatch(r'(?:星期[一二三四五六日天]|昨天|今天)?\s*(\d{1,2})[:：](\d{2})', text)
    return bool(match and int(match[1]) < 24 and int(match[2]) < 60)


def _punctuation(text):
    # Do not ignore signs, decimal points, or punctuation in identifiers.
    return all(char.isspace() or char in '\"\u201c\u201d\u2018\u2019\'' for char in text)


def _historical_noise(left, right):
    """Allow only very small long-row OCR defects, not semantic/numeric edits."""
    if min(len(left), len(right)) < MIN_LONG_TEXT:
        return None
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    ratio = matcher.ratio()
    if ratio < MIN_SIMILARITY:
        return None
    edits = []
    changed_characters = 0
    letter_defects = 0
    for operation, a0, a1, b0, b1 in matcher.get_opcodes():
        if operation == 'equal':
            continue
        removed, added = left[a0:a1], right[b0:b1]
        changed_characters += len(removed) + len(added)
        if _punctuation(removed + added):
            pass
        elif (operation in ('insert', 'delete') and len(removed + added) == 1
              and (removed + added) in {'I', 'l'}):
            # E.g. an OCR-dropped I in AI. Never allow digit or Chinese edits.
            letter_defects += 1
        elif operation == 'replace' and len(removed) == len(added) == 1 and {removed, added} == {'I', 'l'}:
            letter_defects += 1
        else:
            return None
        edits.append({'operation': operation, 'previous': removed, 'current': added})
    if not edits or changed_characters > 4 or letter_defects > 1:
        return None
    return {'similarity': ratio, 'changed_characters': changed_characters, 'edits': edits}


def recover_ocr_overlap(previous, current, strict_result):
    """Return an evidence-rich append result, or None when recovery is unsafe.

    Requires >=8 ordered overlapping rows, >=2 unique exact long-text anchors,
    >=2 unique exact time anchors, and <=20%/3 noisy historical rows. Every
    nonexact historical row must be long and have only a bounded punctuation or
    ASCII-letter OCR defect. Multiple eligible overlaps are rejected outright.
    """
    if not isinstance(strict_result, dict) or strict_result.get('status') != 'gap':
        return None
    if not isinstance(previous, list) or not isinstance(current, list):
        return None
    if not previous or not current or not all(_valid_payload(p) for p in previous + current):
        return None
    previous_counts = Counter(_key(p) for p in previous)
    current_counts = Counter(_key(p) for p in current)
    candidates = []
    for length in range(MIN_OVERLAP, min(len(previous), len(current)) + 1):
        history = previous[-length:]
        anchors = []
        noisy = []
        for index, (old, new) in enumerate(zip(history, current[:length])):
            if not _same_context(old, new):
                break
            if old == new:
                key = _key(old)
                if previous_counts[key] == current_counts[key] == 1:
                    kind = 'time' if _time_anchor(old['text']) else 'long' if len(old['text']) >= MIN_LONG_TEXT else None
                    if kind:
                        anchors.append({'index': index, 'kind': kind})
            else:
                defect = _historical_noise(old['text'], new['text'])
                if defect is None:
                    break
                noisy.append(dict(defect, index=index))
        else:
            kinds = Counter(anchor['kind'] for anchor in anchors)
            limit = min(MAX_NOISY_ROWS, length // 5)
            # Exact overlaps remain the strict reconciler's responsibility.
            if (noisy and len(noisy) <= limit and kinds['long'] >= 2
                    and kinds['time'] >= 2 and len(anchors) >= 4):
                candidates.append({'status': 'append' if len(current)>length else 'unchanged', 'overlap': length,
                                   'new': current[length:],
                                   'metadata': {'strategy': STRATEGY,
                                                'noise_count': len(noisy),
                                                'noise_rows': noisy,
                                                'exact_anchors': anchors,
                                                'exact_long_anchors': kinds['long'],
                                                'exact_time_anchors': kinds['time'],
                                                'historical_rows': length,
                                                'new_tail_verbatim': True}})
    return candidates[0] if len(candidates) == 1 else None
