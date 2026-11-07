import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from store import Store
from reply_client import ReplyClient


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=Store(self.root/'test.sqlite3');self.calls=[]
        self.client=ReplyClient(self.root,self.store,transport=self.transport)
    def tearDown(self):
        self.client.close();self.store.db.close();self.temp.cleanup()
    def transport(self,path,body,method):
        self.calls.append((path,body,method))
        if path.endswith('/claim'): return {'permit':True}
        if path.endswith('/prepare'): return {'event_id':body['event_id'],'conversation_id':body['conversation_id'],'status':'ready','reply_id':'r','reply_text':'你好'}
        return {'status':'sent'}
    def queue(self):
        body={'event_id':'a'*64,'conversation_id':'b'*64,'text':'hello','observed_at':time.time()}
        self.client.queue(body,1,0,{'name':'Demo User'})
        return body
    def ready(self):
        self.queue();self.store.reply_update('a'*64,'ready',response={'event_id':'a'*64,'conversation_id':'b'*64,'reply_id':'r','reply_text':'你好'})
        return self.client.ready()[0]
    def test_queue_is_durable_and_deduplicated(self):
        self.queue();self.queue();self.assertEqual(len(self.store.reply_rows(('queued',))),1)
    def test_prepare_runs_off_gui_and_never_sends(self):
        self.queue();self.client.pump()
        for future in self.client.futures.values(): future.result(timeout=2)
        self.client.pump();self.assertEqual(len(self.client.ready()),1)
        self.assertFalse(any('/claim' in path for path,_,_ in self.calls))
        self.assertNotIn('_target',self.calls[0][1])
    def test_send_started_committed_before_claim_or_action(self):
        item=self.ready()
        original=self.client.transport
        def claim(*args):
            self.assertEqual(self.store.reply_rows(('send_started',))[0]['event_id'],'a'*64)
            return original(*args)
        self.client.transport=claim
        with patch('send.send_verified',return_value={'status':'sent','evidence':{'title_verified':True,'outgoing_verified':True},'reason':'OK'}) as sender:
            self.client.send(item,None,'test',{'name':'Demo User'})
        self.assertEqual(sender.call_count,1);self.assertEqual(len(self.store.reply_rows(('sent',))),1)
    def test_permit_lost_does_not_execute_sender(self):
        item=self.ready();self.client.transport=lambda *args: {'permit':False}
        with patch('send.send_verified') as sender:
            self.assertEqual(self.client.send(item,None,'test',{})['status'],'uncertain')
        sender.assert_not_called()
    def test_missing_outgoing_evidence_is_never_sent(self):
        with patch('send.send_verified',return_value={'status':'sent','evidence':{'title_verified':True}}):
            self.assertEqual(self.client.send(self.ready(),None,'test',{})['status'],'uncertain')
    def test_blocked_maps_failed_and_unbounded_diagnostic_is_not_ack(self):
        with patch('send.send_verified',return_value={'status':'blocked','reason':'DRAFT_EXISTS','evidence':{'title_verified':True,'frame_path':'x'},'diagnostic':{'path':'y'}}):
            self.client.send(self.ready(),None,'test',{})
        row=self.store.reply_rows(('failed',))[0];self.assertNotIn('frame_path',json.loads(row['result_json'])['evidence'])
        self.assertNotIn('diagnostic',json.loads(row['result_json']))
    def test_restart_never_repeats_uncertain_send(self):
        self.queue();self.store.reply_update('a'*64,'send_started')
        second=ReplyClient(self.root,self.store,transport=self.transport)
        self.assertEqual(len(self.store.reply_rows(('uncertain',))),1);self.assertEqual(second.ready(),[]);second.close()

    def test_verified_outgoing_checkpoint_preserves_concurrent_input(self):
        from test_inbound_work import payload
        from inbound_work import InboundWork
        from store import digest
        item={'event_id':'a'*64,'conversation_id':'b'*64}
        target={'name':'Demo User','avatar_sha':'avatar','conversation_title':'Demo User'}
        before=[payload(1)];after=before+[payload(2,'outgoing'),payload(3)]
        result={'status':'sent','diagnostic':{'history_before':before,'after_payloads':[{'stage':'after','payloads':after}], 'frames':[{'stage':'after','path':'frame'}]}}
        self.client.record_sent_observation(item,target,result)
        work=InboundWork(self.store);row=dict(target,key=digest(['Demo User','avatar']))
        self.assertEqual(work.get(row['key'])['status'],'captured')
        self.assertEqual([i for i,_ in work.recover(row,after)],[2])

    def test_uncertain_send_never_changes_observation_checkpoint(self):
        self.client.record_sent_observation({'conversation_id':'b'*64},{},{'status':'uncertain','diagnostic':{'after_payloads':[{}]}})
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM snapshots').fetchone()[0],0)

    def test_concurrent_input_uses_existing_message_cursor_without_second_legacy_capture(self):
        from inbound_messages import InboundMessages
        from test_inbound_work import payload
        messages=InboundMessages(self.store)
        cid='b'*64;target={'name':'Demo User','avatar_sha':'avatar','conversation_title':'Demo User'}
        before=[payload(1),payload(2)]
        def observe(values):
            return self.store.snapshot(cid,'Demo User','avatar','frame',values,
                observation_head=messages.head(cid),on_observation=lambda r,p:messages.record(cid,target,r,p,enabled=True))
        observe(before)
        after=before+[payload(3,'outgoing'),payload(4)]
        result={'status':'sent','diagnostic':{'history_before':before,
            'after_payloads':[{'stage':'after','payloads':after}],
            'frames':[{'stage':'after','path':'frame'}]}}
        self.client.record_sent_observation({'event_id':'a'*64,'conversation_id':cid},target,result)
        rows=self.store.db.execute('SELECT event_id FROM inbound_message_work').fetchall()
        self.assertEqual(len(rows),1)
        next_frame=observe(after)
        self.assertEqual(next_frame['message_event_ids']['3'],rows[0][0])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM inbound_message_work').fetchone()[0],1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM sqlite_master WHERE name='inbound_captures'").fetchone()[0],0)


if __name__=='__main__': unittest.main()
