"""用可控后台验证上报与真实发送终态之间的串行边界。"""
import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path

from reply_client import ReplyClient
from store import Store


class SerialReplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'wechat.sqlite3')
        self.calls, self.states = [], {}
        self.client = ReplyClient(self.root, self.store, transport=self.transport)

    def tearDown(self):
        self.client.executor.shutdown(wait=True)
        self.store.db.close()
        self.temp.cleanup()

    def transport(self, path, body, method):
        self.calls.append((path, body, method))
        if path == '/reply/prepare':
            event, conversation = body['event_id'], body['conversation_id']
            self.states.setdefault(event, {'event_id': event, 'conversation_id': conversation,
                                          'serial_reply': True, 'status': 'generating'})
            return dict(self.states[event])
        if path.endswith('/status'):
            return dict(self.states[path.split('/')[2]])
        raise AssertionError('unexpected request: ' + path)

    def queue(self, letter, conversation='b', observed=None):
        body = {'event_id': letter * 64, 'conversation_id': conversation * 64,
                'text': '消息' + letter, 'observed_at': observed or time.time()}
        self.client.queue(body, 1, ord(letter), {'name': 'Demo User'})

    def cycle(self):
        for _ in range(3):
            self.client.pump()
            for future in list(self.client.futures.values()):
                future.result(timeout=3)
        self.client.pump()

    def prepare_events(self):
        return [body['event_id'] for path, body, _ in self.calls if path == '/reply/prepare']

    def finish(self, letter, status):
        self.states[letter * 64]['status'] = status
        self.client.next_attempt[letter * 64] = 0
        self.cycle()

    def test_messages_stay_local_until_sent_then_drain_in_order(self):
        self.queue('a'); self.cycle()
        self.queue('c'); self.queue('d'); self.cycle()
        self.assertEqual(self.prepare_events(), ['a' * 64])
        self.assertEqual(len(self.store.reply_rows(('queued',))), 3)
        self.finish('a', 'sent')
        self.assertEqual(self.prepare_events(), ['a' * 64, 'c' * 64])
        self.assertNotIn('d' * 64, self.states)
        self.finish('c', 'sent')
        self.assertEqual(self.prepare_events(), ['a' * 64, 'c' * 64, 'd' * 64])
        last=next(body for path,body,_ in self.calls if path=='/reply/prepare' and body['event_id']=='d'*64)
        self.assertEqual(last['buffered_after'],'c'*64)
        self.finish('d', 'sent')
        self.cycle()
        self.assertEqual(len(self.prepare_events()), 3)
        self.assertFalse(any('/claim' in path for path, _, _ in self.calls))

    def test_failure_and_unknown_outcome_retain_buffer_after_restart(self):
        for status in ('failed', 'uncertain'):
            with self.subTest(status=status):
                conversation = 'f' if status == 'failed' else 'e'
                first = 'f' if status == 'failed' else 'e'
                second = 'g' if status == 'failed' else 'h'
                self.queue(first, conversation); self.cycle()
                self.queue(second, conversation)
                self.finish(first, status)
                self.client.executor.shutdown(wait=True)
                self.client = ReplyClient(self.root, self.store, transport=self.transport)
                self.cycle()
                self.assertNotIn(second * 64, self.states)

    def test_no_reply_releases_but_unrelated_conversation_does_not_wait(self):
        self.queue('a'); self.cycle()
        self.queue('c'); self.queue('d', 'e'); self.cycle()
        self.assertIn('d' * 64, self.states)
        self.assertNotIn('c' * 64, self.states)
        self.finish('a', 'no_reply')
        self.assertIn('c' * 64, self.states)

    def test_buffered_message_keeps_original_timestamp_and_does_not_age_out_locally(self):
        self.queue('a'); self.cycle()
        observed = time.time() - 700
        self.queue('c', observed=observed)
        self.store.db.execute('UPDATE reply_outbox SET created=? WHERE event_id=?',
                              (observed, 'c' * 64)); self.store.db.commit()
        self.cycle()
        self.assertNotIn('c' * 64, self.states)
        self.finish('a', 'sent')
        body = next(body for path, body, _ in self.calls if path == '/reply/prepare' and body['event_id'] == 'c' * 64)
        self.assertEqual(body['observed_at'], observed)
        self.assertEqual(body['buffered_after'], 'a' * 64)

    def test_restart_queries_active_event_without_replaying_prepare(self):
        self.queue('a'); self.cycle(); self.queue('c')
        self.client.executor.shutdown(wait=True)
        self.client = ReplyClient(self.root, self.store, transport=self.transport)
        self.cycle()
        self.assertEqual(self.prepare_events(), ['a' * 64])
        self.assertTrue(any(path.endswith('/status') for path, _, _ in self.calls))
        self.assertNotIn('c' * 64, self.states)

    def test_permanent_api_error_retains_queue_without_repeated_submission(self):
        def rejected(*_args):
            raise urllib.error.HTTPError('https://example.invalid', 400, 'invalid', {}, None)
        self.client.transport = rejected
        self.queue('a'); self.queue('c')
        self.client.pump()
        for future in list(self.client.futures.values()):
            with self.assertRaises(urllib.error.HTTPError): future.result(timeout=3)
        self.client.pump()
        self.assertEqual(self.store.reply_rows(('uncertain',))[0]['event_id'], 'a' * 64)
        self.assertEqual(self.store.reply_rows(('queued',))[0]['event_id'], 'c' * 64)
        self.assertFalse(self.client.futures)


if __name__ == '__main__':
    unittest.main()
