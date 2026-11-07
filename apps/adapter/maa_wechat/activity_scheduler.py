"""Persistent, clock-injectable activity scheduling for Maa chat reads.

Only a confirmed message change (or an explicitly trusted message timestamp)
updates activity. Merely seeing a chat in the sidebar never keeps it active.
Unread badges always bypass quiet backoff and the 30-day retirement rule.
This module performs no GUI actions, I/O, sleeps or message sending.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import math
import re
import time
from typing import Callable, Iterable, Mapping


DAY = 86_400.0
STOP_AFTER = 30 * DAY
# True message age -> base interval; unchanged observations add backoff.
AGE_INTERVALS = (
    (5 * 60.0, 5.0),
    (60 * 60.0, 15.0),
    (DAY, 60.0),
    (7 * DAY, 300.0),
    (STOP_AFTER, 1_800.0),
)
MAX_BACKOFF_EXPONENT = 6
MAX_INTERVAL = 3_600.0
BADGE_SCAN_INTERVAL = 3.0
COLD_SCAN_INTERVAL = 60.0
ACTIVITY_SOURCES = {'unknown_history', 'trusted_timestamp', 'sidebar_time_hint', 'confirmed_change'}


def parse_sidebar_time(label: str, *, now: float, timezone=None) -> float | None:
    """Interpret a strict *time-column* label, never arbitrary sidebar preview.

    Explicit full dates preserve their year. Yearless month/day uses the latest
    valid past occurrence (including leap day); weekday labels use the latest
    preceding weekday because WeChat normally renders today's label as HH:mm.
    This is an activity-ranking hint, not authoritative message time or identity
    evidence. Unknown/ambiguous free text and future dates return None.
    Caller must not seed the same label afresh on each process or sidebar sight.
    """
    now = _timestamp(now, 'now')
    if not isinstance(label, str):
        return None
    label = re.sub(r'\s+', '', label)
    local = datetime.fromtimestamp(now).astimezone() if timezone is None else datetime.fromtimestamp(now, timezone)
    local_midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    result = None
    try:
        full = re.fullmatch(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?', label)
        month_day = re.fullmatch(r'(\d{1,2})[/月](\d{1,2})日?', label)
        clock = re.fullmatch(r'(今天|昨天|前天)?(\d{1,2})[:：](\d{2})', label)
        weekday = re.fullmatch(r'(?:星期|周)([一二三四五六日天])', label)
        if full:
            year, month, day = map(int, full.groups())
            result = local_midnight.replace(year=year, month=month, day=day)
        elif month_day:
            month, day = map(int, month_day.groups())
            # At most four years finds a valid previous leap-day occurrence.
            for year in range(local.year, local.year-5, -1):
                try:
                    candidate = local_midnight.replace(year=year, month=month, day=day)
                except ValueError:
                    continue
                if candidate <= local:
                    result = candidate
                    break
        elif clock:
            relative, hour, minute = clock.groups()
            delta = {'今天': 0, '昨天': 1, '前天': 2, None: 0}[relative]
            result = (local_midnight-timedelta(days=delta)).replace(hour=int(hour), minute=int(minute))
        elif label in ('昨天', '前天'):
            result = local_midnight-timedelta(days=1 if label == '昨天' else 2)
        elif weekday:
            wanted = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}[weekday.group(1)]
            delta = (local.weekday()-wanted) % 7 or 7
            result = local_midnight-timedelta(days=delta)
    except (ValueError, OverflowError):
        return None
    return result.timestamp() if result is not None and result <= local else None


def _timestamp(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a finite nonnegative timestamp')
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f'{name} must be a finite nonnegative timestamp')
    return result


def _key(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError('conversation key must be a nonempty string of at most 512 characters')
    return value


class ActivityScheduler:
    """Select bounded read batches and expose a preemption hint.

    ``plan`` returns copies of the input rows with an added ``schedule`` dict.
    The input key is the existing verified/provisional visual identity, never
    just a nickname. Rows with an explicit boolean ``unread=True`` are returned
    before all proactive work; while badges are present, no proactive reads are
    selected. Call ``plan`` again after processing the batch to detect new badges.

    ``observe(..., has_new=False)`` records a successful baseline/unchanged read,
    not a new chat activity. ``reliable=False`` means OCR/identity failed: retry
    later but do not treat the failure as unchanged or establish a baseline.

    Persist ``dump_state()`` alongside the existing worker state. No time is
    inferred from sidebar ``last_seen``, previews, or successful OCR itself.
    """

    def __init__(
        self,
        state: Mapping | None = None,
        *,
        clock: Callable[[], float] = time.time,
        idle_break_after: int = 1,
    ) -> None:
        if isinstance(idle_break_after, bool) or not isinstance(idle_break_after, int) or idle_break_after < 1:
            raise ValueError('idle_break_after must be a positive integer')
        self.clock = clock
        self.idle_break_after = idle_break_after
        self._conversations: dict[str, dict] = {}
        self._idle_reads = 0
        self._last_badge_scan: float | None = None
        self._last_cold_scan: float | None = None
        if state is not None:
            self._load(state)

    def _now(self, now: float | None) -> float:
        return _timestamp(self.clock() if now is None else now, 'now')

    def _load(self, state: Mapping) -> None:
        if not isinstance(state, Mapping) or type(state.get('version')) is not int or state.get('version') != 1 or not isinstance(state.get('conversations'), Mapping):
            raise ValueError('unsupported activity scheduler state')
        for key, raw in state['conversations'].items():
            key = _key(key)
            if not isinstance(raw, Mapping) or not isinstance(raw.get('baseline_read'), bool):
                raise ValueError('invalid conversation state')
            item = {'baseline_read': raw['baseline_read']}
            for field in ('discovered_at', 'last_activity_at', 'last_polled_at', 'next_due_at', 'last_unread_served_at', 'retry_due_at'):
                value = raw.get(field)
                item[field] = _timestamp(value, field) if value is not None else None
            if item['discovered_at'] is None:
                raise ValueError('missing discovered_at')
            streak = raw.get('unchanged_streak', 0)
            if isinstance(streak, bool) or not isinstance(streak, int) or streak < 0:
                raise ValueError('invalid unchanged_streak')
            item['unchanged_streak'] = min(streak, MAX_BACKOFF_EXPONENT)
            source = raw.get('activity_source', 'trusted_timestamp' if item['last_activity_at'] is not None else 'unknown_history')
            if source not in ACTIVITY_SOURCES:
                raise ValueError('invalid activity source')
            item['activity_source'] = source
            self._conversations[key] = item
        idle_reads = state.get('idle_reads', 0)
        if isinstance(idle_reads, bool) or not isinstance(idle_reads, int) or idle_reads < 0:
            raise ValueError('invalid idle_reads')
        self._idle_reads = idle_reads
        for field in ('last_badge_scan', 'last_cold_scan'):
            value = state.get(field)
            setattr(self, '_' + field, None if value is None else _timestamp(value, field))

    def register(self, key: str, *, now: float | None = None, last_message_time: float | None = None) -> None:
        """Register a row, optionally supplying an independently trusted date.

        Unknown message history ages from discovery, allowing one baseline read
        and eventual retirement instead of treating repeated sidebar sightings
        as conversation activity. A trusted timestamp may retire an old chat
        immediately; an unread badge can still wake it at any time.
        """
        key = _key(key)
        now = self._now(now)
        message_time = None if last_message_time is None else _timestamp(last_message_time, 'last_message_time')
        if message_time is not None and message_time > now:
            raise ValueError('last_message_time cannot be in the future')
        item = self._conversations.setdefault(key, {
            'discovered_at': now,
            'last_activity_at': None,
            'last_polled_at': None,
            'next_due_at': None,
            'retry_due_at': None,
            'last_unread_served_at': None,
            'baseline_read': False,
            'unchanged_streak': 0,
            'activity_source': 'unknown_history',
        })
        if message_time is not None:
            prior = item['last_activity_at']
            if prior is None or message_time > prior:
                item['last_activity_at'] = message_time
                item['activity_source'] = 'trusted_timestamp'

    def register_sidebar_hint(self, key: str, label: str, *, now: float | None = None, timezone=None) -> float | None:
        """Seed unknown activity once from a time-column label and keep provenance.

        Existing real message activity (or an earlier hint) is never refreshed
        from repeated row observations. ``observe(has_new=True)`` may later
        replace the hint with a confirmed message-change timestamp.
        """
        now = self._now(now)
        self.register(key, now=now)
        item = self._conversations[key]
        if item['last_activity_at'] is not None:
            return None
        message_time = parse_sidebar_time(label, now=now, timezone=timezone)
        if message_time is not None:
            item['last_activity_at'] = message_time
            item['activity_source'] = 'sidebar_time_hint'
        return message_time

    def _details(self, key: str, now: float) -> dict:
        item = self._conversations[key]
        anchor = item['last_activity_at']
        if anchor is None:
            anchor = item['discovered_at']
        age = max(0.0, now - anchor)
        base = next((interval for upper, interval in AGE_INTERVALS if age < upper), None)
        interval = None if base is None else min(MAX_INTERVAL, base * 2 ** item['unchanged_streak'])
        # Activity weight decreases continuously; no sidebar-only renewal.
        weight = 0.0 if base is None else 1.0 / (1.0 + age / 3_600.0)
        return {'age_seconds': age, 'weight': weight, 'interval_seconds': interval, 'retired': base is None}

    def status(self, key: str, *, now: float | None = None) -> dict:
        """Read-only diagnostics; unknown keys raise KeyError."""
        now = self._now(now)
        return {**deepcopy(self._conversations[_key(key)]), **self._details(key, now)}

    def plan(
        self,
        rows: Iterable[Mapping],
        *,
        now: float | None = None,
        active_budget: int = 2,
        unread_budget: int = 10,
    ) -> list[dict]:
        """Choose badge-only or a small due proactive batch without I/O.

        Baseline is eligible once for unknown-history rows. An explicitly known
        30-day-old conversation is not proactively opened merely because its
        process-local baseline is missing. Old due reads receive an overdue
        boost, so continuously active chats cannot monopolize the small budget.
        Calling this method does not claim a task was served; ``observe`` does.
        """
        now = self._now(now)
        for value in (active_budget, unread_budget):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError('budgets must be nonnegative integers')
        unread, proactive, seen = [], [], set()
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise ValueError('rows must be mappings')
            key = _key(row.get('key'))
            if key in seen:
                continue
            seen.add(key)
            self.register(key, now=now)
            item = self._conversations[key]
            details = self._details(key, now)
            result = dict(row)
            if row.get('unread') is True:
                result['schedule'] = {**details, 'reason': 'unread', 'priority': 1_000_000.0}
                served = item['last_unread_served_at']
                unread.append(((served is not None, served or 0.0, index), result))
                continue
            if details['retired']:
                continue
            if not item['baseline_read']:
                if item['next_due_at'] is not None and now < item['next_due_at']:
                    continue
                result['schedule'] = {**details, 'reason': 'baseline', 'priority': 2.0}
                proactive.append(((-2.0, item['discovered_at'], index), result))
                continue
            # Recalculate on every plan as the message ages into a lower tier.
            due = (item['last_polled_at'] or 0.0) + details['interval_seconds']
            if item['next_due_at'] is not None:
                due = max(due, item['next_due_at'])
            if item.get('retry_due_at') is not None:
                # A failed read is pending evidence, not a quiet conversation.
                # Its bounded retry must bypass prior unchanged backoff.
                due = item['retry_due_at']
            if now < due:
                continue
            overdue = (now - due) / max(1.0, details['interval_seconds'])
            priority = details['weight'] + overdue
            result['schedule'] = {**details, 'reason': 'due', 'priority': priority, 'due_at': due}
            proactive.append(((-priority, item['last_polled_at'] or 0.0, index), result))
        if unread:
            return [row for _, row in sorted(unread, key=lambda item: item[0])[:unread_budget]]
        return [row for _, row in sorted(proactive, key=lambda item: item[0])[:active_budget]]

    def observe(
        self,
        key: str,
        has_new: bool,
        *,
        now: float | None = None,
        last_message_time: float | None = None,
        reliable: bool = True,
        was_unread: bool = False,
    ) -> dict:
        """Record a verified read; only real message changes renew activity.

        Pass ``was_unread=True`` when processing a badge candidate for fair badge
        service. Unreliable OCR/title results never count as quiet reads or new
        messages, even if ``has_new=True`` was passed by mistake.
        """
        if not all(isinstance(value, bool) for value in (has_new, reliable, was_unread)):
            raise ValueError('observation flags must be booleans')
        now = self._now(now)
        self.register(key, now=now, last_message_time=last_message_time if reliable else None)
        item = self._conversations[key]
        item['last_polled_at'] = now
        if was_unread:
            item['last_unread_served_at'] = now
        if not reliable:
            item['next_due_at'] = now + 30.0
            item['retry_due_at'] = item['next_due_at']
            return self.status(key, now=now)
        item['retry_due_at'] = None
        item['baseline_read'] = True
        if has_new:
            message_time = now if last_message_time is None else _timestamp(last_message_time, 'last_message_time')
            prior = item['last_activity_at']
            item['last_activity_at'] = message_time if prior is None else max(prior, message_time)
            item['activity_source'] = 'confirmed_change'
            item['unchanged_streak'] = 0
            self._idle_reads = 0
        else:
            item['unchanged_streak'] = min(MAX_BACKOFF_EXPONENT, item['unchanged_streak'] + 1)
            self._idle_reads += 1
        interval = self._details(key, now)['interval_seconds']
        item['next_due_at'] = None if interval is None else now + interval
        return self.status(key, now=now)

    def should_interrupt(self, unread_keys: Iterable[str] | None = None) -> bool:
        """Hint to abandon quiet active reads and rescan the sidebar/top now."""
        return bool(unread_keys and any(True for _ in unread_keys)) or self._idle_reads >= self.idle_break_after

    def resume_after_badge_scan(self) -> None:
        """Reset the quiet-read hint after the caller actually scans badges."""
        self._idle_reads = 0

    def badge_scan_due(self, *, now: float | None = None, interval: float = BADGE_SCAN_INTERVAL) -> bool:
        """Cheap top-of-list peek cadence, not a mandatory blocking sleep."""
        now = self._now(now)
        interval = _timestamp(interval, 'badge scan interval')
        return self._last_badge_scan is None or now - self._last_badge_scan >= interval

    def mark_badge_scan(self, *, now: float | None = None) -> None:
        """Record an actual top badge scan and reset the preemption hint."""
        self._last_badge_scan = self._now(now)
        self.resume_after_badge_scan()

    def cold_scan_due(self, *, now: float | None = None, interval: float = COLD_SCAN_INTERVAL) -> bool:
        """Cadence for sweeping lower sidebar pages without opening old chats.

        Retirement suppresses proactive message reads, not badge discovery:
        occasional lower-page scans still let old conversations wake on badges.
        """
        now = self._now(now)
        interval = _timestamp(interval, 'cold scan interval')
        return self._last_cold_scan is None or now - self._last_cold_scan >= interval

    def mark_cold_scan(self, *, now: float | None = None) -> None:
        self._last_cold_scan = self._now(now)

    def dump_state(self) -> dict:
        """Return independent JSON-safe state; caller chooses atomic storage."""
        return {
            'version': 1,
            'conversations': deepcopy(self._conversations),
            'idle_reads': self._idle_reads,
            'last_badge_scan': self._last_badge_scan,
            'last_cold_scan': self._last_cold_scan,
        }
