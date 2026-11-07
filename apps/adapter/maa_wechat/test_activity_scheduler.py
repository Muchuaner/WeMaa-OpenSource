import json
from datetime import datetime, timezone
import unittest

try:
    from maa_wechat.activity_scheduler import ActivityScheduler, DAY, STOP_AFTER, parse_sidebar_time
except ModuleNotFoundError:
    from activity_scheduler import ActivityScheduler, DAY, STOP_AFTER, parse_sidebar_time


class ActivitySchedulerTests(unittest.TestCase):
    def setUp(self):
        self.now = 100 * DAY
        self.scheduler = ActivityScheduler(clock=lambda: self.now)

    def row(self, key='a', unread=False, **extra):
        return {'key': key, 'unread': unread, **extra}

    def test_first_unknown_row_has_one_baseline_without_fake_activity(self):
        self.assertEqual(self.scheduler.plan([self.row()])[0]['schedule']['reason'], 'baseline')
        self.scheduler.observe('a', False)
        self.assertEqual(self.scheduler.plan([self.row()]), [])
        self.assertIsNone(self.scheduler.status('a')['last_activity_at'])

    def test_sidebar_last_seen_never_refreshes_activity(self):
        self.scheduler.plan([self.row(last_seen=self.now)])
        discovered = self.scheduler.status('a')['discovered_at']
        self.scheduler.observe('a', False)
        self.now += STOP_AFTER
        self.assertEqual(self.scheduler.plan([self.row(last_seen=self.now)]), [])
        self.assertEqual(self.scheduler.status('a')['discovered_at'], discovered)
        self.assertTrue(self.scheduler.status('a')['retired'])

    def test_age_tiers_reduce_weight_and_increase_interval(self):
        self.scheduler.observe('a', True)
        samples = []
        for age in (0, 5 * 60, 60 * 60, DAY, 7 * DAY, STOP_AFTER):
            samples.append(self.scheduler.status('a', now=self.now + age))
        self.assertEqual([s['interval_seconds'] for s in samples], [5, 15, 60, 300, 1800, None])
        self.assertEqual([s['weight'] for s in samples], sorted([s['weight'] for s in samples], reverse=True))

    def test_unchanged_reads_back_off_but_true_change_resets(self):
        self.scheduler.observe('a', True)
        intervals = []
        for _ in range(8):
            intervals.append(self.scheduler.observe('a', False)['interval_seconds'])
        self.assertEqual(intervals, [10, 20, 40, 80, 160, 320, 320, 320])
        self.assertEqual(self.scheduler.observe('a', True)['interval_seconds'], 5)
        self.assertFalse(self.scheduler.should_interrupt())

    def test_retired_row_wakes_immediately_on_unread(self):
        self.scheduler.register('a', last_message_time=self.now - STOP_AFTER)
        self.assertEqual(self.scheduler.plan([self.row()]), [])
        chosen = self.scheduler.plan([self.row(unread=True)])[0]
        self.assertEqual(chosen['schedule']['reason'], 'unread')
        self.scheduler.observe('a', True, was_unread=True)
        self.assertFalse(self.scheduler.status('a')['retired'])

    def test_unread_preempts_all_due_active_work(self):
        chosen = self.scheduler.plan([self.row('active'), self.row('badge', True)])
        self.assertEqual([r['key'] for r in chosen], ['badge'])
        self.assertGreater(chosen[0]['schedule']['priority'], 100_000)

    def test_badge_fairness_with_budget(self):
        rows = [self.row('a', True), self.row('b', True)]
        self.assertEqual(self.scheduler.plan(rows, unread_budget=1)[0]['key'], 'a')
        self.scheduler.observe('a', False, was_unread=True)
        self.assertEqual(self.scheduler.plan(rows, unread_budget=1)[0]['key'], 'b')

    def test_proactive_budget_and_duplicate_keys(self):
        rows = [self.row('a'), self.row('a'), self.row('b'), self.row('c')]
        self.assertEqual([r['key'] for r in self.scheduler.plan(rows)], ['a', 'b'])
        self.assertEqual(len(self.scheduler.dump_state()['conversations']), 3)
        self.assertEqual(self.scheduler.plan(rows, active_budget=0), [])

    def test_no_change_interrupts_and_scan_resets_hint(self):
        self.scheduler.observe('a', False)
        self.assertTrue(self.scheduler.should_interrupt())
        self.scheduler.resume_after_badge_scan()
        self.assertFalse(self.scheduler.should_interrupt())
        self.assertTrue(self.scheduler.should_interrupt(['another']))
        self.assertFalse(self.scheduler.should_interrupt([]))

    def test_failure_does_not_establish_baseline_or_fake_activity(self):
        result = self.scheduler.observe('a', True, reliable=False)
        self.assertIsNone(result['last_activity_at'])
        self.assertFalse(result['baseline_read'])
        self.assertEqual(result['unchanged_streak'], 0)
        self.assertFalse(self.scheduler.should_interrupt())
        self.assertEqual(self.scheduler.plan([self.row()]), [])
        self.now += 30
        self.assertEqual(self.scheduler.plan([self.row()])[0]['schedule']['reason'], 'baseline')

    def test_state_roundtrip_preserves_deadlines_age_and_fairness(self):
        self.scheduler.observe('a', True, was_unread=True)
        self.scheduler.observe('a', False)
        state = json.loads(json.dumps(self.scheduler.dump_state(), allow_nan=False))
        restored = ActivityScheduler(state, clock=lambda: self.now)
        self.assertEqual(restored.status('a'), self.scheduler.status('a'))
        self.assertEqual(restored.plan([self.row()]), [])
        self.assertTrue(restored.should_interrupt())
        state['conversations']['a']['last_activity_at'] = 0
        self.assertNotEqual(restored.status('a')['last_activity_at'], 0)

    def test_verified_history_gap_retries_after_thirty_seconds_despite_quiet_backoff(self):
        self.scheduler.observe('a',False,last_message_time=self.now-2*DAY)
        for _ in range(4):self.scheduler.observe('a',False)
        before=self.scheduler.status('a')
        self.assertGreater(before['interval_seconds'],30)
        self.scheduler.observe('a',False,reliable=False)
        restored=ActivityScheduler(self.scheduler.dump_state(),clock=lambda:self.now)
        self.now+=29;self.assertEqual(restored.plan([self.row()]),[])
        self.now+=1;self.assertEqual(len(restored.plan([self.row()])),1)
        self.assertEqual(restored.status('a')['last_activity_at'],before['last_activity_at'])
        self.assertEqual(restored.status('a')['unchanged_streak'],before['unchanged_streak'])
        restored.observe('a',False)
        self.assertIsNone(restored.status('a')['retry_due_at'])
        self.assertEqual(restored.plan([self.row()]),[])

    def test_explicit_old_timestamp_is_not_replaced_with_baseline_read_time(self):
        old = self.now - 10 * DAY
        self.scheduler.observe('a', False, last_message_time=old)
        self.assertEqual(self.scheduler.status('a')['last_activity_at'], old)
        self.assertEqual(self.scheduler.status('a')['age_seconds'], 10 * DAY)

    def test_read_plans_do_not_mutate_input_rows(self):
        original = self.row('a', True)
        chosen = self.scheduler.plan([original])[0]
        self.assertNotIn('schedule', original)
        self.assertIsNot(chosen, original)

    def test_timestamp_and_budget_validation(self):
        for value in (float('nan'), float('inf'), -1, True, 'today'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.scheduler.register('a', now=value)
        with self.assertRaises(ValueError):
            self.scheduler.observe('a', True, last_message_time=self.now + 1)
        with self.assertRaises(ValueError):
            self.scheduler.plan([self.row()], active_budget=-1)
        with self.assertRaises(ValueError):
            self.scheduler.plan([self.row()], unread_budget=True)

    def test_string_false_is_not_an_unread_badge(self):
        result = self.scheduler.plan([self.row(unread='false')])[0]
        self.assertEqual(result['schedule']['reason'], 'baseline')

    def test_corrupt_or_wrong_version_state_rejected(self):
        for state in ({}, [], {'version': True, 'conversations': {}}, {'version': 2, 'conversations': {}},
                      {'version': 1, 'conversations': {'a': {'baseline_read': False, 'discovered_at': float('nan')}}}):
            with self.subTest(state=state), self.assertRaises(ValueError):
                ActivityScheduler(state)

    def test_age_progression_does_not_use_stale_short_deadline(self):
        self.scheduler.observe('a', True)
        self.now += 5 * 60
        details = self.scheduler.status('a')
        self.assertEqual(details['interval_seconds'], 15)
        self.assertEqual(self.scheduler.plan([self.row()])[0]['schedule']['reason'], 'due')

    def test_passive_badge_and_cold_scan_cadences_survive_restart(self):
        self.assertTrue(self.scheduler.badge_scan_due())
        self.assertTrue(self.scheduler.cold_scan_due())
        self.scheduler.observe('a', False)
        self.scheduler.mark_badge_scan()
        self.scheduler.mark_cold_scan()
        self.assertFalse(self.scheduler.should_interrupt())
        restored = ActivityScheduler(self.scheduler.dump_state(), clock=lambda: self.now)
        self.assertFalse(restored.badge_scan_due())
        self.assertFalse(restored.cold_scan_due())
        self.now += 3
        self.assertTrue(restored.badge_scan_due())
        self.assertFalse(restored.cold_scan_due())
        self.now += 57
        self.assertTrue(restored.cold_scan_due())

    def test_maximum_backoff_never_exceeds_one_hour(self):
        self.scheduler.register('a', last_message_time=self.now - 10 * DAY)
        for _ in range(20):
            result = self.scheduler.observe('a', False)
        self.assertEqual(result['interval_seconds'], 3600)

    def test_overdue_older_conversation_is_not_starved_by_fresh_chat(self):
        self.scheduler.observe('old', True, now=self.now - DAY)
        self.scheduler.observe('fresh', True, now=self.now - 5)
        selected = self.scheduler.plan([self.row('fresh'), self.row('old')], active_budget=1)
        self.assertEqual(selected[0]['key'], 'old')

    def test_baseline_cannot_lower_existing_trusted_activity_timestamp(self):
        self.scheduler.register('a', last_message_time=self.now - DAY)
        self.scheduler.observe('a', False, last_message_time=self.now - 10 * DAY)
        self.assertEqual(self.scheduler.status('a')['last_activity_at'], self.now - DAY)

    def test_old_sidebar_date_can_retire_without_fake_last_seen_activity(self):
        now = datetime(2026, 9, 26, 20, 0, tzinfo=timezone.utc).timestamp()
        scheduler = ActivityScheduler(clock=lambda: now)
        seeded = scheduler.register_sidebar_hint('old', '8/01', timezone=timezone.utc)
        self.assertEqual(seeded, datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())
        self.assertEqual(scheduler.plan([self.row('old')]), [])
        self.assertEqual(scheduler.status('old')['activity_source'], 'sidebar_time_hint')
        self.assertEqual(scheduler.plan([self.row('old', True)])[0]['key'], 'old')

    def test_same_clock_label_next_day_never_refreshes_seeded_activity(self):
        now = datetime(2026, 9, 26, 20, 0, tzinfo=timezone.utc).timestamp()
        scheduler = ActivityScheduler(clock=lambda: now)
        scheduler.register_sidebar_hint('a', '19:00', timezone=timezone.utc)
        first = scheduler.status('a')['last_activity_at']
        now += DAY
        self.assertIsNone(scheduler.register_sidebar_hint('a', '19:00', timezone=timezone.utc))
        self.assertEqual(scheduler.status('a')['last_activity_at'], first)
        scheduler.observe('a', True)
        self.assertEqual(scheduler.status('a')['activity_source'], 'confirmed_change')
        self.assertEqual(scheduler.status('a')['last_activity_at'], now)

    def test_sidebar_parser_handles_full_year_year_rollover_and_weekday(self):
        now = datetime(2026, 1, 2, 20, 0, tzinfo=timezone.utc).timestamp()
        expected = {
            '2024年12月31日': datetime(2024, 12, 31, tzinfo=timezone.utc),
            '12/31': datetime(2025, 12, 31, tzinfo=timezone.utc),
            '昨天13:15': datetime(2026, 1, 1, 13, 15, tzinfo=timezone.utc),
            '星期五': datetime(2025, 12, 26, tzinfo=timezone.utc),
            '星期四': datetime(2026, 1, 1, tzinfo=timezone.utc),
            '2/29': datetime(2024, 2, 29, tzinfo=timezone.utc),
        }
        for label, result in expected.items():
            with self.subTest(label=label):
                self.assertEqual(parse_sidebar_time(label, now=now, timezone=timezone.utc), result.timestamp())

    def test_invalid_future_or_arbitrary_sidebar_labels_are_unknown(self):
        now = datetime(2026, 9, 26, 20, 0, tzinfo=timezone.utc).timestamp()
        for label in ('2027/1/1', '21:00', '9/31', '24:01', '你好', '有人说昨天发消息', '20:2', ''):
            with self.subTest(label=label):
                self.assertIsNone(parse_sidebar_time(label, now=now, timezone=timezone.utc))


if __name__ == '__main__':
    unittest.main()
