import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from creation_turns import CreationTurns
from store import Store


class CreationTurnTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.folder.name)/'state.sqlite3')
        self.turns=CreationTurns(self.store)

    def tearDown(self):
        self.store.db.close();self.folder.cleanup()

    def body(self, event='a', cid='b'):
        return {'event_id':event*64,'conversation_id':cid*64,'observed_at':time.time(),'text':'/积分'}

    def receipt(self, body, deliveries=None, state='sent'):
        return {'protocol':'creation_receipts_v1','event_id':body['event_id'],
                'conversation_id':body['conversation_id'],'observed_at':body['observed_at'],
                'status':state,'deliveries':deliveries if deliveries is not None else
                [{'id':'d'*32,'status':'sent','verified_sent':True}]}

    def accept(self, body):
        return self.turns.accepted(body['event_id'],{'handled':True,'event_id':body['event_id']})

    def test_chat_command_chat_share_one_receipt_boundary_and_survive_restart(self):
        b=self.body();serial=self.turns.serial
        serial.remember('normal-before',b['conversation_id']);self.turns.remember(b)
        serial.remember('normal-after',b['conversation_id'])
        self.assertFalse(self.turns.begin(b['event_id']))
        serial.finish('normal-before','sent');self.assertTrue(self.turns.begin(b['event_id']))
        self.accept(b);self.assertFalse(serial.acquire('normal-after'))
        self.turns=CreationTurns(self.store)
        self.assertFalse(self.turns.begin(b['event_id']))
        self.turns.observe(b['event_id'],self.receipt(b,[{'id':'d'*32,'status':'sending','verified_sent':False}],'waiting'))
        self.assertFalse(serial.acquire('normal-after'))
        self.turns.observe(b['event_id'],self.receipt(b))
        self.assertTrue(serial.acquire('normal-after'))
        self.assertEqual(self.turns.item(b['event_id'])['observed'],b['observed_at'])

    def test_same_chat_commands_wait_but_another_chat_progresses(self):
        a,b,other=self.body(),self.body('c'),self.body('e','f')
        for item in (a,b,other):self.turns.remember(item)
        self.assertTrue(self.turns.begin(a['event_id']))
        self.assertFalse(self.turns.begin(b['event_id']))
        self.assertTrue(self.turns.begin(other['event_id']))
        self.accept(a);self.turns.observe(a['event_id'],self.receipt(a,[],state='uncertain'))
        self.assertFalse(self.turns.begin(b['event_id']))
        self.assertTrue(self.turns.begin(other['event_id']))

    def test_queue_and_lane_commit_together_or_neither_exists(self):
        b=self.body();remember=self.turns.serial.remember
        def broken(*args,**kwargs):
            remember(*args,**kwargs)
            raise RuntimeError('simulated disk failure before commit')
        with patch.object(self.turns.serial,'remember',side_effect=broken),self.assertRaises(RuntimeError):
            self.turns.remember(b)
        for table,column in [('creation_events','id'),('creation_turns','event_id'),('reply_serial','event_id')]:
            self.assertIsNone(self.store.db.execute(f'SELECT 1 FROM {table} WHERE {column}=?',(b['event_id'],)).fetchone())

    def test_unknown_processing_response_is_not_sent_and_no_reply_must_be_explicit(self):
        b=self.body();self.turns.remember(b);self.turns.begin(b['event_id']);self.accept(b)
        self.assertEqual(self.turns.item(b['event_id'])['state'],'awaiting_delivery')
        with self.assertRaises(ValueError):self.turns.observe(b['event_id'],self.receipt(b,[]))
        self.assertEqual(self.turns.item(b['event_id'])['state'],'awaiting_delivery')
        other=self.body('e','f');self.turns.remember(other);self.turns.begin(other['event_id'])
        self.assertEqual(self.turns.accepted(other['event_id'],{'handled':False}),'no_reply')

    def test_changed_event_or_time_never_releases_the_lane(self):
        b=self.body();self.turns.remember(b);self.turns.begin(b['event_id']);self.accept(b)
        for key,value in [('event_id','c'*64),('conversation_id','c'*64),('observed_at',time.time()+1),('protocol','unknown')]:
            receipt=self.receipt(b);receipt[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):self.turns.observe(b['event_id'],receipt)
        self.assertEqual(self.turns.item(b['event_id'])['state'],'awaiting_delivery')

    def test_download_order_cannot_reverse_immediate_deliveries(self):
        b=self.body();self.turns.remember(b);self.turns.begin(b['event_id'])
        self.assertFalse(self.turns.permits_delivery('e'*32,b['conversation_id']))
        self.accept(b)
        items=[{'id':'d'*32,'status':'ready','verified_sent':False},{'id':'e'*32,'status':'ready','verified_sent':False}]
        self.turns.observe(b['event_id'],self.receipt(b,items,'waiting'))
        self.assertFalse(self.turns.permits_delivery('e'*32,b['conversation_id']))
        self.assertTrue(self.turns.permits_delivery('d'*32,b['conversation_id']))
        items[0].update(status='sent',verified_sent=True)
        self.turns.observe(b['event_id'],self.receipt(b,items,'waiting'))
        self.assertTrue(self.turns.permits_delivery('e'*32,b['conversation_id']))

    def test_restart_finishes_lane_when_exit_interrupted_terminal_receipt_commit(self):
        b=self.body();self.turns.remember(b);self.turns.begin(b['event_id']);self.accept(b)
        with patch.object(self.turns.serial,'finish',side_effect=RuntimeError('process exits')):
            with self.assertRaises(RuntimeError):self.turns.observe(b['event_id'],self.receipt(b))
        self.assertEqual(self.store.db.execute('SELECT state FROM reply_serial WHERE event_id=?',(b['event_id'],)).fetchone()[0],'active')
        CreationTurns(self.store)
        self.assertEqual(self.store.db.execute('SELECT state FROM reply_serial WHERE event_id=?',(b['event_id'],)).fetchone()[0],'sent')

    def test_expired_original_message_is_retained_without_time_refresh(self):
        b=self.body();b['observed_at']-=400;self.turns.remember(b)
        self.assertFalse(self.turns.begin(b['event_id']))
        self.assertEqual(self.turns.item(b['event_id'])['state'],'blocked')
        original=json.loads(self.store.db.execute('SELECT body FROM creation_events WHERE id=?',(b['event_id'],)).fetchone()[0])
        self.assertEqual(original['observed_at'],b['observed_at'])

    def test_old_buffered_child_retains_time_and_requires_completed_lane_parent(self):
        a=self.body();a['observed_at']-=1000
        b=self.body('c');b['observed_at']-=800
        self.turns.remember(a);self.turns.remember(b)
        self.assertFalse(self.turns.begin(b['event_id']))
        self.turns.serial.finish(a['event_id'],'sent')
        self.assertTrue(self.turns.begin(b['event_id']))
        self.assertEqual(self.turns.item(b['event_id'])['observed'],b['observed_at'])

    def test_client_pump_receipt_gate_does_not_deadlock_or_starve_other_chats(self):
        from concurrent.futures import Future
        from creation_client import CreationClient
        root=Path(self.folder.name);(root/'private').mkdir()
        (root/'private/admin-agent.json').write_text(json.dumps({
            'endpoint':'https://your-service.example.com/api/v1/wechat-maa','token':'test-only'}))
        client=CreationClient(root,self.store);client.executor.shutdown(wait=True)
        calls=[]
        class Executor:
            def submit(self,*args):
                future=Future();calls.append((args,future));return future
            def shutdown(self,**kwargs):pass
        client.executor=Executor();client.next_poll=time.time()+1000
        a,b,c,d,other=[self.body(e) for e in ('a','c','d','e')]+[self.body('f','9')]
        for item in (a,b,c,d,other):client.queue_event(item)
        client.pump()
        self.assertIn(a['event_id'],client.futures)
        self.assertIn(other['event_id'],client.futures)
        self.assertNotIn(b['event_id'],client.futures)
        client.futures[a['event_id']][0].set_result({'handled':True,'event_id':a['event_id']})
        client.pump()
        turn_key='turn:'+a['event_id']
        self.assertIn(turn_key,client.futures)
        client.futures[turn_key][0].set_result(self.receipt(a,[{'id':'d'*32,'status':'ready','verified_sent':False}],'waiting'))
        client.pump()
        self.store.queue_gui({'id':'d'*32,'action':'send_delivery','params':{'conversation_id':a['conversation_id']},'expires_at':time.time()+100})
        self.assertTrue(client.delivery_ready())
        self.assertNotIn(b['event_id'],client.futures)
        self.store.gui_result('d'*32,'succeeded',{'status':'sent'})
        client.next_event[turn_key]=0;client.pump()
        client.futures[turn_key][0].set_result(self.receipt(a));client.pump()
        self.assertIn(b['event_id'],client.futures)
        self.assertNotIn(c['event_id'],client.futures)
        # 重启后已经提交的 b 只有幂等入站重查；a 的已送达回信不再入队。
        self.assertEqual(client.turns.item(a['event_id'])['state'],'sent')
        self.assertEqual(self.store.db.execute('SELECT status FROM creation_events WHERE id=?',(a['event_id'],)).fetchone()[0],'done')
        client.close()
