import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from inbound_work import InboundWork
from store import Store
from watch import dispatch_incoming, incoming_requests, record_dispatch_outcome


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'state.sqlite3')
        self.store.db.execute('CREATE TABLE creation_events(id TEXT PRIMARY KEY,body TEXT,status TEXT,updated REAL)')
        self.store.db.commit()
        self.work = InboundWork(self.store)
        self.row = {'key': 'row', 'name': '开发组', 'avatar_sha': 'avatar', 'unread': True}
        self.target = {'name': '开发组', 'conversation_title': '开发组(4)', 'avatar_sha': 'avatar'}
        self.payload = {'text': '/菜单', 'direction': 'incoming', 'message_kind': 'text','sender_observation_key':'1'*64,
                        'reply_safe': True, 'group_sender_name': 'Demo User', 'group_sender_verified': True,
                        'visual': {'box': [545, 828, 63, 36], 'verified': True, 'sha256': 'a'*64,
                                   'avatar_sha256': 'b'*64, 'avatar_verified': True}, 'ocr': {'stable': True}}
        self.result = self.store.snapshot('c'*64, '开发组(4)', 'avatar', 'frame.png', [self.payload])
        self.work.capture(self.row, self.result, [self.payload], [0])
        self.seed = self.work.get('row')['seed']
        self.result.update(event_seed=self.seed, source_ordinals={0: 0})
        self.body = {'event_id': 'e'*64, 'conversation_id': 'c'*64, 'conversation_type': 'group',
                     'sender_name': 'Demo User', 'text': '/菜单', 'observed_at': 100}
        self.proof = {'wechat_id': 'wx_test_only', 'display_name': 'Demo User', 'stable': True,
                      'profile_sha256': 'f'*64, 'message_frame_sha256': 'd'*64,
                      'message_identity': 'a'*64, 'message_avatar_sha256': 'b'*64}
        self.client = SimpleNamespace(queue=Mock())
        self.creation = SimpleNamespace(queue_event=Mock(side_effect=self.persist_creation))
        self.experiment = SimpleNamespace(applies_to=Mock(return_value=False), queue_incoming=Mock())

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def persist_creation(self, body):
        self.store.db.execute('INSERT OR IGNORE INTO creation_events VALUES(?,?,?,?)',
                              (body['event_id'], json.dumps(body), 'pending', 0))
        self.store.db.commit()

    def dispatch(self, body=None, result=None):
        return dispatch_incoming([(0, body or self.body)], self.store, self.client,
            self.creation, self.experiment, None, None, [self.payload], self.result['snapshot_id'],
            self.target, 'test', result=result or self.result)

    def test_identity_failure_keeps_original_body_seed_and_review_instead_of_fake_queue(self):
        with patch('sender_identity.verify_member', side_effect=ValueError('PROFILE_CLICK_FAILED')):
            dispatch = self.dispatch()
        self.assertEqual(dispatch['accepted'], [])
        self.creation.queue_event.assert_not_called()
        record_dispatch_outcome(self.work, self.row, self.result, [0], dispatch, self.store)
        self.assertEqual(self.work.get('row')['status'], 'review')
        self.assertEqual(self.work.get('row')['seed'], self.seed)
        saved = self.store.db.execute('SELECT event_id,event_seed,source_ordinal,request_json,status FROM inbound_dispatch').fetchone()
        self.assertEqual(saved[:3], ('e'*64, self.seed, 0))
        self.assertEqual(json.loads(saved[3]), self.body)
        self.assertEqual(saved[4], 'review')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM creation_events').fetchone()[0], 0)

    def test_success_requires_creation_journal_and_remembers_full_avatar_proof_locally(self):
        with patch('sender_identity.verify_member', return_value=self.proof):
            dispatch = self.dispatch()
        self.assertEqual(dispatch['accepted'], [0])
        sent_body = self.creation.queue_event.call_args.args[0]
        self.assertEqual(set(sent_body['identity']), {'wechat_id', 'display_name', 'stable',
            'profile_sha256', 'message_frame_sha256', 'message_identity'})
        self.assertEqual(self.payload['sender_wechat_id'], 'wx_test_only')
        self.assertEqual(self.store.db.execute('SELECT wechat_id FROM message_senders').fetchone()[0], 'wx_test_only')
        record_dispatch_outcome(self.work, self.row, self.result, [0], dispatch, self.store)
        self.assertEqual(self.work.get('row')['status'], 'queued')

    def test_returning_true_without_persistence_is_never_accepted(self):
        self.creation.queue_event = Mock(return_value=True)
        with patch('sender_identity.verify_member', return_value=self.proof):
            dispatch = self.dispatch()
        self.assertEqual(dispatch['accepted'], [])
        self.assertEqual(dispatch['rejected'][0]['reason'], 'INBOUND_DISPATCH_NOT_PERSISTED')

    def test_terminal_reply_journal_never_calls_profile_or_queues_again(self):
        for status in ('sent', 'failed', 'uncertain', 'send_started'):
            with self.subTest(status=status):
                self.store.queue_reply(self.body, self.result['snapshot_id'], 0)
                self.store.reply_update(self.body['event_id'], status)
                with patch('sender_identity.verify_member') as verify:
                    self.assertEqual(self.dispatch()['accepted'], [0])
                verify.assert_not_called()
                self.creation.queue_event.assert_not_called()
                actual = self.store.db.execute('SELECT status FROM reply_outbox WHERE event_id=?', (self.body['event_id'],)).fetchone()[0]
                self.assertEqual(actual, status)

    def test_existing_done_creation_never_calls_profile_or_changes_terminal_record(self):
        self.persist_creation(self.body)
        self.store.db.execute("UPDATE creation_events SET status='done'");self.store.db.commit()
        with patch('sender_identity.verify_member') as verify:
            self.assertEqual(self.dispatch()['accepted'], [0])
        verify.assert_not_called();self.creation.queue_event.assert_not_called()
        self.assertEqual(self.store.db.execute('SELECT status FROM creation_events').fetchone()[0], 'done')

    def test_review_retry_retains_original_event_and_requires_explicit_review(self):
        with patch('sender_identity.verify_member', side_effect=ValueError('PROFILE_CLICK_FAILED')):
            self.dispatch()
        with patch('sender_identity.verify_member') as verify:
            dispatch = self.dispatch(dict(self.body, event_id='x'*64))
        verify.assert_not_called()
        self.assertEqual(dispatch['rejected'][0]['event_id'], 'e'*64)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM inbound_dispatch').fetchone()[0], 1)

    def test_crash_before_queue_uses_original_event_on_shifted_recovered_ordinal(self):
        with patch('sender_identity.verify_member', side_effect=ValueError('PROFILE_CLICK_FAILED')):
            self.dispatch()
        self.store.db.execute("UPDATE inbound_dispatch SET status='captured'");self.store.db.commit()
        with patch('sender_identity.verify_member', return_value=self.proof):
            dispatch = self.dispatch(dict(self.body, event_id='x'*64), dict(self.result, source_ordinals={'0': 0}))
        self.assertEqual(dispatch['accepted'], [0])
        self.assertEqual(self.creation.queue_event.call_args.args[0]['event_id'], 'e'*64)

    def test_partial_dispatch_is_review_even_when_one_other_message_is_durable(self):
        record_dispatch_outcome(self.work, self.row, self.result, [0, 1],
            {'accepted': [0], 'rejected': [{'event_id': 'x'*64, 'ordinal': 1, 'reason': 'failed'}]}, self.store)
        self.assertEqual(self.work.get('row')['status'], 'review')
        self.assertEqual(self.work.get('row')['seed'], self.seed)

    def test_experiment_false_without_reply_journal_is_review(self):
        self.experiment.applies_to.return_value = True
        self.experiment.queue_incoming.return_value = False
        self.assertEqual(self.dispatch()['accepted'], [])
        self.creation.queue_event.assert_not_called();self.client.queue.assert_not_called()

    def test_repeated_equal_bubbles_have_distinct_recovered_occurrences(self):
        result = {'status': 'unread_recovery', 'overlap': 0, 'snapshot_id': 1, 'event_seed': 'seed'}
        first = incoming_requests([self.payload, self.payload], result, self.row, '开发组(4)', 'c'*64, 100, 90)
        self.assertNotEqual(first[0][1]['event_id'], first[1][1]['event_id'])
        recovered = incoming_requests([self.payload], dict(result, source_ordinals={'0': 1}), self.row,
                                      '开发组(4)', 'c'*64, 100, 90)
        self.assertEqual(recovered[0][1]['event_id'], first[1][1]['event_id'])
        original = incoming_requests([self.payload], dict(result, event_ids={0: 'e'*64}), self.row,
                                    '开发组(4)', 'c'*64, 100, 90)
        self.assertEqual(original[0][1]['event_id'], 'e'*64)


if __name__ == '__main__':
    unittest.main()
