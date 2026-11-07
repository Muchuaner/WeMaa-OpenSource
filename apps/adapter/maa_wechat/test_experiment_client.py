import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiment_client import ExperimentClient
from store import Store
from reply_client import ReplyClient
from send import send_verified
from test_send import FakeRuntime
from watch import send_ready_on_rows,normal_folded_entry


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        from test_send import SendTests
        SendTests.setUp(self)
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);(self.root/'data').mkdir()
        self.store=Store(self.root/'data/wechat.sqlite3')
        self.reply=ReplyClient(self.root,self.store,transport=lambda *args:{})
        self.client=ExperimentClient(self.root,self.store,self.reply)

    def tearDown(self):
        self.client.close();self.reply.close();self.store.db.close();self.temp.cleanup()

    def test_only_explicit_tester_uses_experiment_before_global_enable(self):
        self.client.control={'enabled':False,'testing_contact':'wx:tester','testing_name':'Demo User'}
        self.assertTrue(self.client.applies_to({'conversation_type':'private','sender_name':'Demo User'}))
        self.assertFalse(self.client.applies_to({'conversation_type':'private','sender_name':'other'}))
        self.assertFalse(self.client.applies_to({'conversation_type':'group','sender_name':'Demo User'}))

    def test_failed_contact_yields_to_untried_and_three_failures_survive_restart(self):
        a={'id':'a'*64,'contact_id':'wx:a','status':'pending','scheduled_at':0,'expires_at':1e12}
        b=dict(a,id='b'*64,contact_id='wx:b',scheduled_at=1)
        self.client.control={'items':[a,b]}
        self.client.navigation_failed(a['id'],'CHAT_TITLE_MISMATCH:details',100)
        self.assertEqual(self.client.choose_opening(300),b)
        self.client.navigation_failed(a['id'],'MANUAL_COMMAND_EXPIRED',300)
        self.client.navigation_failed(a['id'],'CHAT_TITLE_MISMATCH',500)
        self.client.close();self.client=ExperimentClient(self.root,self.store,self.reply)
        self.client.control={'items':[a,b]}
        self.assertEqual(self.client.choose_opening(1000),b)
        self.store.db.execute('INSERT INTO experiment_attempts VALUES(?,?,?,?)',(b['id'],'wx:b','queued',1000));self.store.db.commit()
        self.assertIsNone(self.client.choose_opening(1000))
        self.assertEqual(self.store.db.execute('SELECT attempts FROM experiment_navigation').fetchone()[0],3)

    def test_inbound_preemption_never_uses_failure_budget(self):
        for t in (1,2,3,4):self.client.navigation_failed('a'*64,'CONTACT_NAVIGATION_PREEMPTED',t)
        self.assertEqual(self.store.db.execute('SELECT attempts FROM experiment_navigation').fetchone()[0],0)

    def test_navigation_report_is_idempotent_ack_and_control_poll_continues_after_error(self):
        calls=[]
        def transport(path,body,method):
            calls.append(path)
            if path.endswith('/navigation'):
                if body['event_id']=='b'*64:raise ValueError('OFFLINE')
                return {'recorded':True}
            return {'enabled':True,'items':[]}
        self.reply.transport=transport
        result=self.client.poll([('a'*64,3,'CHAT_TITLE_MISMATCH'),('b'*64,3,'MANUAL_COMMAND_EXPIRED')])
        self.assertEqual(result['_navigation_acks'],['a'*64]);self.assertTrue(result['enabled'])
        self.assertEqual(calls[-1],'/experiment/plan')

    def test_regular_folded_row_is_not_an_unverified_inner_page(self):
        import numpy as np
        image=np.full((1040,1920,3),250,dtype=np.uint8)
        self.assertFalse(normal_folded_entry(image))
        image[115:132,80:100]=[30,150,250]
        self.assertTrue(normal_folded_entry(image))

    def test_incoming_and_queue_preempt_openings(self):
        self.client.control={'enabled':True,'items':[{'id':'a'*64,'status':'pending','scheduled_at':0,'expires_at':1e12}]}
        self.client.last_poll=__import__('time').time()
        with patch('manual_gui.open_contact') as navigate:
            self.assertFalse(self.client.process_one(None,'test',urgent=True));navigate.assert_not_called()
            self.store.queue_reply({'event_id':'b'*64,'conversation_id':'c'*64},1,0)
            self.assertFalse(self.client.process_one(None,'test',urgent=False));navigate.assert_not_called()

    def test_failed_identity_is_persistent_and_never_falls_back_to_customer(self):
        body={'event_id':'a'*64,'conversation_id':'b'*64,'conversation_type':'private'}
        with patch.object(self.client,'verify_private_contact',side_effect=ValueError('PROFILE_UNAVAILABLE')),patch.object(self.reply,'queue') as queue:
            for _ in range(3):self.assertFalse(self.client.queue_incoming(body,1,0,{},None,None,{}))
            self.assertEqual(self.store.db.execute('SELECT status,attempts FROM experiment_inbound_identity').fetchone(),('review',3))
            queue.assert_not_called()

    def test_identity_retry_uses_exact_pixels_and_stable_ocr_without_rewriting_text(self):
        from experiment_client import verified_occurrences
        from test_message_vision import payload
        original=payload('最近有什么歌推荐？','a');later=payload('最近有什么歌推荐?','a')
        self.assertEqual(verified_occurrences([later],original),[later])
        self.assertEqual(verified_occurrences([payload(original['text'],'b')],original),[])
        later['reply_safe']=False;self.assertEqual(verified_occurrences([later],original),[])
        later['reply_safe']=True;self.assertEqual(len(verified_occurrences([later,later],original)),2)

    def test_successful_identity_retries_share_one_event_and_queue_once(self):
        body={'event_id':'a'*64,'conversation_id':'b'*64,'conversation_type':'private'}
        self.store.db.execute("INSERT INTO contacts VALUES('wx:tester','tester','Demo User','photo','f',?,0,'ocr_wechat_id')",(json.dumps({'scope':'personal'}),));self.store.db.commit()
        with patch.object(self.client,'verify_private_contact',return_value={'wechat_id':'tester'}),patch.object(self.reply,'queue',return_value=True) as queue:
            self.assertTrue(self.client.queue_incoming(body,1,0,{},None,None,{}))
            self.assertFalse(self.client.queue_incoming(body,1,0,{},None,None,{}))
            self.assertEqual(queue.call_count,1);self.assertEqual(queue.call_args.args[0]['event_id'],body['event_id'])

    def test_contact_profile_that_returns_another_chat_blocks_identity(self):
        import numpy as np
        from unittest.mock import Mock
        from test_message_vision import payload
        self.store.db.execute("INSERT INTO contacts VALUES('wx:tester','tester','Demo User','photo','f',?,0,'ocr_wechat_id')",(json.dumps({'scope':'personal'}),))
        self.store.db.execute("INSERT INTO contact_chat_links VALUES('wx:tester',?,0)",('b'*64,));self.store.db.commit()
        image=np.zeros((1040,1920,3),dtype=np.uint8);runtime=Mock();runtime.capture.return_value=(image,'f')
        with patch('send._target',return_value={'verified':True}),patch('manual_gui.open_contact',return_value=('c'*64,{})):
            with self.assertRaisesRegex(ValueError,'CONTACT_CHAT_MISMATCH'):
                self.client.verify_private_contact({'event_id':'a'*64,'conversation_id':'b'*64},{'name':'Demo User'},runtime,image,payload('消息','a'))

    def test_opening_with_badge_waits_for_incoming_read(self):
        request={'_opening':True,'_target':{'name':'Demo User','avatar_sha':'a'}}
        fake=type('Reply',(),{'ready':lambda _: [{'request_json':json.dumps(request)}], 'send':lambda *args:self.fail('sent unread opening')})()
        with patch('watch.select') as select:
            self.assertFalse(send_ready_on_rows(fake,None,'test',[{'name':'Demo User','avatar_sha':'a','unread':True}]))
            select.assert_not_called()

    def request(self,runtime):
        return {'name':'Demo User','conversation_title':'Demo User','avatar_sha':runtime.avatar_sha,'row_y':113,
                'reply_text':'你好呀','event_id':'a'*64}

    def test_new_chat_pixels_block_opening_before_input(self):
        runtime=FakeRuntime()
        result=send_verified(runtime,self.root,'test',self.request(runtime),expected_chat_sha='0'*64)
        self.assertEqual(result['status'],'blocked');self.assertEqual(runtime.nodes,[])

    def test_stop_between_input_and_send_retains_draft_and_never_clicks(self):
        runtime=FakeRuntime();calls=[]
        def preflight():
            calls.append(True)
            if len(calls)==2:raise ValueError('EXPERIMENT_STOPPED')
        result=send_verified(runtime,self.root,'test',self.request(runtime),preflight=preflight)
        self.assertEqual(result['status'],'blocked');self.assertTrue(runtime.draft)
        self.assertFalse(any(n=='SendDraft' for n,_ in runtime.nodes))

    def test_verified_opening_sends_once(self):
        runtime=FakeRuntime();image,_=runtime.capture('baseline-before')
        sha=hashlib.sha256(image[82:887,490:1885,:3].tobytes()).hexdigest()
        result=send_verified(runtime,self.root,'test',self.request(runtime),expected_chat_sha=sha,preflight=lambda:None)
        self.assertEqual(result['status'],'sent')
        self.assertEqual(sum(n=='SendDraft' for n,_ in runtime.nodes),1)

    def test_deep_contact_scroll_uses_observed_scan_positions(self):
        import numpy as np
        from unittest.mock import Mock
        from manual_gui import contact_hint
        photos={i:np.random.default_rng(i).integers(0,256,(24,24,3),dtype=np.uint8) for i in range(70)}
        for i,photo in photos.items():
            self.store.contact_position('scan',i,'contact'+str(i),'friend'+str(i),hashlib.sha256(photo.tobytes()).hexdigest(),'frame')
        def frame(start):
            image=np.zeros((1040,1920,3),dtype=np.uint8);image[0,0,0]=start
            for offset,i in enumerate(range(start,start+10)):
                y=200+offset*54;image[y-12:y+12,99:123]=photos[i]
            return image,'frame'
        runtime=Mock();runtime.capture.side_effect=[frame(0),frame(10),frame(50)]
        def rows(_runtime,image,*args):
            start=int(image[0,0,0]);return [{'name':'friend'+str(i),'y':200+(i-start)*54} for i in range(start,start+10)]
        with patch('manual_gui.contact_rows',side_effect=rows):contact_hint(runtime,self.root,self.store,'scan',55,70,'test')
        scrolls=[call.args[1]['ContactsScrollDown']['dy'] for call in runtime.node.call_args_list if call.args[0]=='ContactsScrollDown']
        self.assertEqual(scrolls,[-120,-120])
        repeats=[call.args[1]['ContactsScrollDown']['repeat'] for call in runtime.node.call_args_list if call.args[0]=='ContactsScrollDown']
        self.assertEqual(repeats,[5,19])

    def test_accelerated_contact_scroll_corrects_observed_overshoot(self):
        import numpy as np
        from unittest.mock import Mock
        from manual_gui import contact_hint
        photos={i:np.random.default_rng(i).integers(0,256,(24,24,3),dtype=np.uint8) for i in range(90)}
        for i,photo in photos.items():self.store.contact_position('scan',i,'contact'+str(i),'friend'+str(i),hashlib.sha256(photo.tobytes()).hexdigest(),'f')
        def frame(start):
            image=np.zeros((1040,1920,3),dtype=np.uint8);image[0,0,0]=start
            for i in range(start,start+10):
                y=200+(i-start)*54;image[y-12:y+12,99:123]=photos[i]
            return image,'f'
        runtime=Mock();runtime.capture.side_effect=[frame(0),frame(5),frame(65),frame(50)]
        def rows(_runtime,image,*args):
            start=int(image[0,0,0]);return [{'name':'friend'+str(i),'y':200+(i-start)*54} for i in range(start,start+10)]
        with patch('manual_gui.contact_rows',side_effect=rows):contact_hint(runtime,self.root,self.store,'scan',55,90,'test')
        calls=[c for c in runtime.node.call_args_list if c.args[0] in ('ContactsScrollDown','ContactsScrollUp')]
        self.assertEqual([c.args[0] for c in calls],['ContactsScrollDown','ContactsScrollDown','ContactsScrollUp'])
        self.assertEqual([c.args[1][c.args[0]]['repeat'] for c in calls],[5,25,8])


if __name__=='__main__': unittest.main()
