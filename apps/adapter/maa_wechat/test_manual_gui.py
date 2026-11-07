import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from store import Store
from manual_gui import validate_intent, process_one, open_contact
import hashlib
import numpy as np

class ManualTests(unittest.TestCase):
    def test_deferred_creation_delivery_is_never_claimed_by_manual_owner(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db.sqlite3');runtime=Mock()
            store.queue_gui({'id':'a'*32,'action':'send_delivery','params':{'conversation_id':'b'*64},'expires_at':time.time()+100})
            self.assertFalse(process_one(runtime,Path(directory),store,'test'))
            self.assertEqual(store.db.execute('SELECT status FROM gui_commands').fetchone()[0],'pending')
            runtime.node.assert_not_called();store.db.close()

    def test_manual_owner_can_progress_behind_another_owners_pending_delivery(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db.sqlite3');runtime=Mock();cid='b'*64
            store.queue_gui({'id':'a'*32,'action':'send_delivery','params':{'conversation_id':cid},'expires_at':time.time()+100})
            store.route(cid,{'name':'Demo User','avatar_sha':'f'*64,'conversation_title':'Demo User','row_y':243})
            store.queue_gui({'id':'c'*32,'action':'open_conversation','params':{'conversation_id':cid},'expires_at':time.time()+100})
            with patch('manual_gui.find_recent',return_value=({'name':'Demo User','avatar_sha':'f'*64,'y':243},0)),patch('manual_gui.observe',return_value=(cid,{'name':'Demo User'})):
                self.assertTrue(process_one(runtime,Path(directory),store,'test'))
            self.assertEqual(store.db.execute('SELECT status FROM gui_commands WHERE id=?',('a'*32,)).fetchone()[0],'pending')
            self.assertEqual(store.db.execute('SELECT status FROM gui_commands WHERE id=?',('c'*32,)).fetchone()[0],'succeeded')
            store.db.close()

    def test_latest_position_name_not_old_cached_name_and_wrong_wxid_never_opens(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db.sqlite3');image=np.zeros((1040,1920,3),dtype=np.uint8);image[200,65]=[0,180,0]
            sha=hashlib.sha256(image[188:212,99:123,:3].tobytes()).hexdigest()
            scan=store.begin_scan(1);store.finish_scan(scan,1,1,True,{'scope':'personal'})
            cid=store.contact(scan,0,'old cached name','expected_id','profile','profile.png',{'scope':'personal','list_avatar_sha':'old'},'profile.png',[],[])
            store.contact_position(scan,0,cid,'lom',sha,'frame.png')
            class Runtime:
                calls=[]
                def node(self,name,*args): self.calls.append(name)
                def capture(self,*args): return image,'frame.png'
                def recognize_image(self,*args): return [{'text':'微信号:wrong_id','box':[1110,130,200,20],'score':1}]
            runtime=Runtime()
            with patch('manual_gui.scroll_top'),patch('manual_gui.ensure_category',return_value={'count':1}),patch('manual_gui.contact_rows',return_value=[{'name':'Demo User','y':200}]):
                with self.assertRaisesRegex(ValueError,'CONTACT_PROFILE_ID_MISMATCH'):
                    open_contact(runtime,Path(directory),store,cid,'test',time.time()+60)
            self.assertNotIn('ContactOpenChat',runtime.calls)
            store.db.close()
    def command(self, **extra):
        return dict(id='a'*32,action='send_message',params={'conversation_id':'b'*64,'text':'你好'},expires_at=time.time()+60,**extra)

    def test_validation(self):
        validate_intent(self.command())
        for text in ('', ' '*3, 'a'*401, 'a\x00b'):
            value=self.command();value['params']['text']=text
            with self.assertRaises(ValueError): validate_intent(value)
        for expiry in (0, float('nan'), float('inf'), True, 'future'):
            value=self.command();value['expires_at']=expiry
            with self.assertRaises(ValueError): validate_intent(value)
        value=self.command();value['params']['coordinates']=[1,2]
        with self.assertRaises(ValueError): validate_intent(value)

    def test_manual_observation_does_not_swallow_auto_reply_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db.sqlite3')
            store.snapshot('b'*64,'Demo User','avatar','old.png',[{'text':'a'},{'text':'b'}])
            store.manual_view('b'*64,'Demo User','avatar','manual.png',[{'text':'b'},{'text':'c'}])
            result=store.snapshot('b'*64,'Demo User','avatar','auto.png',[{'text':'b'},{'text':'c'}])
            self.assertEqual(result['status'],'append')
            self.assertEqual(store.db.execute('SELECT count(*) FROM messages').fetchone()[0],1)
            store.db.close()

    def test_queue_idempotent_and_claim_precedes_effect_no_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db.sqlite3');command=self.command()
            store.queue_gui(command);store.queue_gui(command)
            store.route('b'*64,{'name':'Demo User'})
            self.assertEqual(store.db.execute('SELECT count(*) FROM gui_commands').fetchone()[0],1)
            def fail(*_):
                self.assertEqual(store.db.execute('SELECT status FROM gui_commands').fetchone()[0],'executing')
                raise ValueError('TARGET_NOT_FOUND')
            with patch('manual_gui.find_recent',side_effect=fail) as effect:
                self.assertTrue(process_one(None,Path(directory),store,'test'))
                self.assertFalse(process_one(None,Path(directory),store,'test'))
                self.assertEqual(effect.call_count,1)
            detail=json.loads(store.db.execute('SELECT detail_json FROM gui_commands').fetchone()[0])
            self.assertEqual(detail['status'],'failed')
            self.assertFalse(detail['diagnostic']['send_click_attempted'])
            self.assertFalse(detail['diagnostic']['draft_prepared'])
            store.db.close()

    def test_verified_recent_send_is_committed_before_context_refresh_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db.sqlite3');command=self.command()
            store.queue_gui(command);store.route('b'*64,{'name':'Demo User'})
            target={'name':'Demo User','avatar_sha':'avatar','conversation_title':'Demo User','row_y':113}
            receipt={'status':'sent','reason':'verified','evidence':{'title_verified':True,'outgoing_verified':True}}
            calls=[]
            def observe(*args,**kwargs):
                calls.append(args[4])
                if len(calls)==1:return 'b'*64,target
                persisted=store.db.execute('SELECT status,detail_json FROM gui_commands').fetchone()
                self.assertEqual(persisted[0],'succeeded')
                self.assertEqual(json.loads(persisted[1])['status'],'sent')
                raise RuntimeError('context refresh failed')
            with patch('manual_gui.find_recent',return_value=({'name':'Demo User','avatar_sha':'avatar','y':113},0)), \
                 patch('manual_gui.observe',side_effect=observe), \
                 patch('send.send_verified',return_value=receipt) as send:
                self.assertTrue(process_one(None,Path(directory),store,'test'))
            status,detail=store.db.execute('SELECT status,detail_json FROM gui_commands').fetchone()
            self.assertEqual(status,'succeeded')
            self.assertEqual(json.loads(detail)['status'],'sent')
            self.assertTrue(json.loads(detail)['context_refresh_failed'])
            send.assert_called_once()
            store.db.close()
