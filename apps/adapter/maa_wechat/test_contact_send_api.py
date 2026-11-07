"""Contact-bound API commands use the real one-shot send with simulated Maa."""
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import test_send as send_fixture
from manual_gui import process_one, recover_interrupted_gui, validate_intent, open_contact
from store import Store


class ContactSendTests(unittest.TestCase):
    def setUp(self):
        send_fixture.SendTests.setUp(self)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'data').mkdir()
        self.store=Store(self.root/'data/wechat.sqlite3');self.addCleanup(self.store.db.close)
        self.store.db.execute('INSERT INTO contacts VALUES(?,?,?,?,?,?,?,?)',
            ('wx:test_user','test_user','Demo User','avatar','avatar.png',json.dumps({'scope':'personal'}),time.time(),'ocr_wechat_id'))
        self.store.db.commit()

    def command(self, **params):
        return {'id':'a'*32,'action':'send_contact_message','params':{'contact_id':'wx:test_user','wechat_id':'test_user','text':'你好呀',**params},'expires_at':time.time()+60}

    def result(self):
        status,detail=self.store.db.execute('SELECT status,detail_json FROM gui_commands').fetchone()
        return status,json.loads(detail)

    def open_fake(self, runtime,root,store,contact_id,label,expires,proof):
        self.assertEqual(self.store.db.execute('SELECT status FROM gui_commands').fetchone()[0],'executing')
        proof.update(wechat_id='test_user',stable=True,profile_sha256='c'*64)
        return 'b'*64,{'name':'Demo User','conversation_title':'Demo User','avatar_sha':runtime.avatar_sha,'row_y':113,'list_page':0}

    def run_fake(self, runtime, *, refresh_failure=False):
        command=self.command();self.store.queue_gui(command);self.store.queue_gui(command)
        with patch('manual_gui.open_contact',side_effect=self.open_fake) as opened,patch('manual_gui.observe',side_effect=RuntimeError('sync failed') if refresh_failure else None):
            self.assertTrue(process_one(runtime,self.root,self.store,'test'))
            self.assertFalse(process_one(runtime,self.root,self.store,'test'))
            self.assertEqual(opened.call_count,1)
        return self.result()

    def test_complete_contact_command_one_send_and_duplicate_does_not_replay(self):
        runtime=send_fixture.FakeRuntime();status,detail=self.run_fake(runtime)
        self.assertEqual((status,detail['status']),('succeeded','sent'),detail)
        self.assertTrue(detail['evidence']['profile_verified'])
        self.assertEqual(sum(n[0]=='SendDraft' for n in runtime.nodes),1)
        self.assertTrue(detail['evidence']['outgoing_verified'])
        self.assertFalse(detail['diagnostic']['draft_retained'])

    def test_verified_send_survives_context_sync_failure(self):
        runtime=send_fixture.FakeRuntime();status,detail=self.run_fake(runtime,refresh_failure=True)
        self.assertEqual((status,detail['status']),('succeeded','sent'))
        self.assertTrue(detail['context_refresh_failed'])
        self.assertEqual(sum(n[0]=='SendDraft' for n in runtime.nodes),1)

    def test_existing_draft_blocks_before_input(self):
        runtime=send_fixture.FakeRuntime(draft='用户草稿');status,detail=self.run_fake(runtime)
        self.assertEqual((status,detail['status']),('failed','blocked'))
        self.assertEqual(runtime.nodes,[])
        self.assertEqual(runtime.draft,'用户草稿')

    def test_click_exception_is_uncertain_and_never_replayed(self):
        runtime=send_fixture.FakeRuntime(send_raises=True);status,detail=self.run_fake(runtime)
        self.assertEqual((status,detail['status']),('failed','uncertain'))
        self.assertEqual(sum(n[0]=='SendDraft' for n in runtime.nodes),1)

    def test_mismatch_and_enterprise_are_rejected_before_navigation(self):
        for params in ({'wechat_id':'other_user'},{'contact_id':'missing'}):
            self.store.db.execute('DELETE FROM gui_commands');self.store.db.commit()
            self.store.queue_gui(self.command(**params))
            with patch('manual_gui.open_contact') as opening:
                process_one(None,self.root,self.store,'test');opening.assert_not_called()
            self.assertEqual(self.result()[1]['status'],'failed')
        self.store.db.execute('UPDATE contacts SET facts_json=?',(json.dumps({'scope':'enterprise'}),))
        self.store.db.execute('DELETE FROM gui_commands');self.store.db.commit();self.store.queue_gui(self.command())
        with patch('manual_gui.open_contact') as opening:
            process_one(None,self.root,self.store,'test');opening.assert_not_called()

    def test_profile_proof_and_private_chat_required(self):
        for proof,group in (({'stable':False,'wechat_id':'test_user'},False),({'stable':True,'wechat_id':'other_user'},False),({'stable':True,'wechat_id':'test_user'},True)):
            self.store.db.execute('DELETE FROM gui_commands');self.store.db.commit();self.store.queue_gui(self.command())
            def opening(*args,**kw):
                kw['proof'].update(proof)
                return 'b'*64,{'name':'Demo User','conversation_title':'Demo User','avatar_sha':'c'*64,'row_y':113,'list_page':0}
            with patch('manual_gui.open_contact',side_effect=opening),patch('group_policy.is_group',return_value=group),patch('send.send_verified') as sending:
                process_one(None,self.root,self.store,'test');sending.assert_not_called()

    def test_expired_validation_and_crash_claim_not_replayed(self):
        for text in ('a\nb','a\tb','a\x7fb','a\x85b','a\u2028b','x'*401):
            with self.assertRaises(ValueError):validate_intent(self.command(text=text))
        command=self.command();command['expires_at']=time.time()-1;self.store.queue_gui(command)
        with patch('manual_gui.open_contact') as opening:
            process_one(None,self.root,self.store,'test');opening.assert_not_called()
        self.assertEqual(self.result()[1]['reason'],'MANUAL_COMMAND_EXPIRED')
        self.store.db.execute('DELETE FROM gui_commands');self.store.db.commit();self.store.queue_gui(self.command())
        self.store.gui_result('a'*32,'executing',{})
        recover_interrupted_gui(self.store)
        self.assertEqual(self.result()[1]['status'],'uncertain')
        with patch('manual_gui.open_contact') as opening:
            self.assertFalse(process_one(None,self.root,self.store,'test'));opening.assert_not_called()

    def test_two_full_profile_reads_required_before_open(self):
        image=np.zeros((1040,1920,3),dtype=np.uint8);image[200,65]=[0,180,0]
        scan=self.store.begin_scan(1);self.store.finish_scan(scan,1,1,True,{'scope':'personal'})
        sha=hashlib.sha256(image[188:212,99:123,:3].tobytes()).hexdigest()
        self.store.db.execute('UPDATE contacts SET facts_json=?',(json.dumps({'scope':'personal','list_avatar_sha':sha}),));self.store.db.commit()
        self.store.contact_position(scan,0,'wx:test_user','Demo User',sha,'frame.png')
        class Runtime:
            def __init__(self):self.nodes=[]
            def node(self,name,*args):self.nodes.append(name)
            def capture(self,*args):return image,'frame.png'
            def recognize_image(self,*args):return [{'text':'微信号:test_user','box':[1110,130,200,20],'score':1}]
        runtime=Runtime()
        with patch('manual_gui.scroll_top'),patch('manual_gui.ensure_category',return_value={'count':1}),patch('manual_gui.contact_rows',return_value=[{'name':'Demo User','y':200}]),patch('send._ocr_scaled',return_value=[{'text':'微信号:wrong_user','box':[1,1,20,20],'score':1}]):
            with self.assertRaisesRegex(ValueError,'CONTACT_PROFILE_ID_NOT_STABLE'):
                open_contact(runtime,self.root,self.store,'wx:test_user','test',time.time()+60)
        self.assertNotIn('ContactOpenChat',runtime.nodes)
