import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from reply_reconcile import ReplyReconciler, MAX_ATTEMPTS, FIRST_DELAY_SECONDS
from store import Store


class ReplyReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data' / 'wechat.sqlite3')
        self.addCleanup(self.store.db.close)
        self.reconciler = ReplyReconciler(self.root, self.store)
        self.image = np.zeros((1040, 1920, 3), dtype=np.uint8)
        self.target = {'name': '创作平台AI-内测001', 'conversation_title': '创作平台AI-内测001(54)',
                       'avatar_sha': 'avatar1', 'row_y': 100}

    def queue(self, event, text='你好', reason='SEND_OUTCOME_UNKNOWN', conversation='b' * 64,
              reported=True, target=None):
        request = {'event_id': event, 'conversation_id': conversation,
                   '_target': self.target if target is None else target}
        self.store.queue_reply(request, 1, 0)
        self.store.reply_update(event, 'ready', response={'reply_id': 'r', 'reply_text': text})
        self.store.reply_update(event, 'uncertain',
                                result={'status': 'uncertain', 'reason': reason, 'evidence': {}},
                                reported=reported)

    def row(self, event):
        return next(r for r in self.store.reply_rows(('sent', 'failed', 'uncertain'))
                    if r['event_id'] == event)

    def events(self, kind):
        return [json.loads(detail) for matched, detail in self.store.db.execute(
            'SELECT kind,detail_json FROM events WHERE kind=?', (kind,))]

    def observe(self, payloads, title='创作平台AI-内测001(54)', navigation_error=None):
        runtime = Mock()
        runtime.capture.return_value = (self.image, 'frame.png')
        row = {'name': '创作平台AI-内测001', 'avatar_sha': 'avatar1', 'y': 100, 'name_clipped': False}
        stack = ExitStack()
        self.addCleanup(stack.close)
        if navigation_error is None:
            find = stack.enter_context(patch('manual_gui.find_recent', return_value=(row, 0)))
        else:
            find = stack.enter_context(patch('manual_gui.find_recent', side_effect=navigation_error))
        stack.enter_context(patch('watch.read_title', return_value=title))
        stack.enter_context(patch('watch.is_group_title', return_value=True))
        stack.enter_context(patch('text_copy.read_messages', return_value=(payloads, [])))
        return runtime, find

    def attempt(self, runtime, event='e' * 32):
        self.reconciler.attempt(runtime, 'run', self.row(event), self.store.reconcile_state(event))

    def test_due_requires_reported_click_reason_and_delay(self):
        self.queue('e' * 32)
        item = self.row('e' * 32)
        due, _ = self.reconciler.due(now=item['updated'] + FIRST_DELAY_SECONDS - 1)
        self.assertIsNone(due)
        due, _ = self.reconciler.due(now=item['updated'] + FIRST_DELAY_SECONDS + 1)
        self.assertEqual(due['event_id'], 'e' * 32)
        self.queue('f' * 32, reported=False)
        self.queue('a' * 32, reason='GENERATION_TIMEOUT')
        self.queue('c' * 32, reason='SEND_CLAIM_NOT_GRANTED')
        self.store.reconcile_schedule('e' * 32, MAX_ATTEMPTS, 0)
        due, _ = self.reconciler.due(now=item['updated'] + FIRST_DELAY_SECONDS + 1)
        self.assertIsNone(due)

    def test_observed_outgoing_text_converges_to_sent_with_evidence(self):
        self.queue('e' * 32)
        runtime, _ = self.observe([{'direction': 'outgoing', 'message_kind': 'text', 'text': '你好'}])
        self.attempt(runtime)
        row = self.row('e' * 32)
        receipt = json.loads(row['result_json'])
        self.assertEqual(row['status'], 'sent')
        self.assertEqual(row['reported'], 0)
        self.assertEqual(receipt['status'], 'sent')
        self.assertEqual(receipt['reason'], 'RECONCILED_OUTGOING_OBSERVED')
        self.assertTrue(receipt['evidence']['title_verified'])
        self.assertTrue(receipt['evidence']['outgoing_verified'])
        self.assertEqual(len(receipt['evidence']['frame_sha256']), 64)
        self.assertTrue(receipt['evidence']['snapshot_id'] > 0)
        self.assertEqual(len(self.events('reply_reconciled')), 1)

    def test_incoming_match_never_converges(self):
        self.queue('e' * 32)
        runtime, _ = self.observe([{'direction': 'incoming', 'message_kind': 'text', 'text': '你好'}])
        self.attempt(runtime)
        self.assertEqual(self.row('e' * 32)['status'], 'uncertain')
        self.assertEqual(len(self.events('reply_reconcile_absent')), 1)

    def test_absent_text_keeps_uncertain_and_schedules_backoff(self):
        self.queue('e' * 32)
        runtime, _ = self.observe([{'direction': 'outgoing', 'message_kind': 'text', 'text': '别的'}])
        self.attempt(runtime)
        self.assertEqual(self.row('e' * 32)['status'], 'uncertain')
        state = self.store.reconcile_state('e' * 32)
        self.assertEqual(state['attempts'], 1)
        self.assertGreater(state['next_attempt'], 0)
        self.assertEqual(len(self.events('reply_reconcile_absent')), 1)

    def test_navigation_failure_is_recorded_without_convergence(self):
        self.queue('e' * 32)
        runtime, _ = self.observe([], navigation_error=ValueError('TARGET_NOT_FOUND'))
        self.attempt(runtime)
        self.assertEqual(self.row('e' * 32)['status'], 'uncertain')
        self.assertEqual(self.events('reply_reconcile_navigation')[0]['reason'], 'TARGET_NOT_FOUND')

    def test_identical_text_in_same_conversation_never_navigates(self):
        self.queue('e' * 32)
        self.queue('d' * 32)
        runtime, find = self.observe([{'direction': 'outgoing', 'message_kind': 'text', 'text': '你好'}])
        self.attempt(runtime)
        find.assert_not_called()
        self.assertEqual(self.row('e' * 32)['status'], 'uncertain')
        self.assertEqual(len(self.events('reply_reconcile_ambiguous_text')), 1)

    def test_incomplete_target_skips_without_navigation(self):
        self.queue('e' * 32, target={'name': '创作平台AI-内测001'})
        runtime, find = self.observe([])
        self.attempt(runtime)
        find.assert_not_called()
        self.assertEqual(self.row('e' * 32)['status'], 'uncertain')
        self.assertEqual(len(self.events('reply_reconcile_skipped')), 1)

    def test_exhausted_attempts_raise_human_review_event(self):
        self.queue('e' * 32)
        self.store.reconcile_schedule('e' * 32, MAX_ATTEMPTS - 1, 0)
        runtime, _ = self.observe([])
        self.attempt(runtime)
        self.assertEqual(self.row('e' * 32)['status'], 'uncertain')
        self.assertEqual(len(self.events('reply_reconcile_review')), 1)
        due, _ = self.reconciler.due(now=self.row('e' * 32)['updated'] + 10 ** 6)
        self.assertIsNone(due)

    def test_old_identical_bubble_does_not_converge(self):
        cid = self.store.group_identity(self.target['name'], self.target['conversation_title'], 'avatar1', [])
        before = [{'direction': 'outgoing', 'message_kind': 'text', 'text': '你好'}]
        self.store.snapshot(cid, self.target['conversation_title'], 'avatar1', 'before.png', before)
        self.queue('e' * 32, text='你好', conversation=cid)
        runtime, _ = self.observe(before)
        self.attempt(runtime)
        self.assertEqual(self.row('e' * 32)['status'], 'uncertain')
        self.assertEqual(len(self.events('reply_reconcile_absent')), 1)

    def test_reconciliation_preserves_concurrent_inbound(self):
        cid = self.store.group_identity(self.target['name'], self.target['conversation_title'], 'avatar1', [])
        before = [{'direction': 'incoming', 'message_kind': 'text', 'text': 'old-anchor'}]
        self.store.snapshot(cid, self.target['conversation_title'], 'avatar1', 'before.png', before)
        self.queue('e' * 32, text='你好', conversation=cid)
        after = before + [
            {'direction': 'outgoing', 'message_kind': 'text', 'text': '你好'},
            {'direction': 'incoming', 'message_kind': 'text', 'text': 'new-live-inbound', 'reply_safe': True}
        ]
        runtime, _ = self.observe(after)
        self.attempt(runtime)
        self.assertEqual(self.row('e' * 32)['status'], 'sent')
        # Next poll must detect new-live-inbound as append, never swallow it as unchanged
        poll_snapshot = self.store.snapshot(cid, self.target['conversation_title'], 'avatar1', 'poll.png', after)
        self.assertEqual(poll_snapshot['status'], 'append')
        self.assertTrue(len(poll_snapshot['new']) > 0)


if __name__ == '__main__':
    unittest.main()
