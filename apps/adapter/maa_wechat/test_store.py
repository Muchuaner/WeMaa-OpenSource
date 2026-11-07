import unittest
from pathlib import Path
import tempfile
import json
import unittest
import time
from pathlib import Path
try:
    from maa_wechat.store import Store, reconcile
except ModuleNotFoundError:
    from store import Store, reconcile


class DiffTests(unittest.TestCase):
    def test_snapshot_time_is_capture_time_not_the_end_of_slow_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'test.sqlite3');captured=time.time()-40
            result=store.snapshot('c','title','avatar','frame.png',[{'text':'history'}],captured_at=captured)
            self.assertEqual(store.db.execute('SELECT seen FROM snapshots WHERE id=?',(result['snapshot_id'],)).fetchone()[0],captured)
            for invalid in (True,float('nan'),float('inf'),-1,time.time()+60):
                with self.subTest(value=invalid),self.assertRaisesRegex(ValueError,'CAPTURE_TIME'):
                    store.snapshot('bad','title','avatar','frame.png',[],captured_at=invalid)
            self.assertIsNone(store.db.execute("SELECT id FROM conversations WHERE id='bad'").fetchone())
            store.db.close()

    def test_group_count_is_mutable_and_literal_person_name_is_not_group(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/'test.sqlite3')
            self.assertFalse(store.observe_group('person','Tom(10)','Tom(10)'))
            self.assertFalse(store.observe_group('person','Demo User','Tom(0)'))
            self.assertTrue(store.observe_group('group','开发组','开发组 (4)'))
            self.assertTrue(store.observe_group('group','开发组','开发组（5）'))
            self.assertEqual(store.db.execute('SELECT count(*),member_count FROM conversation_profiles').fetchone(),(1,5))
            self.assertFalse(store.observe_group('group','开发组','开发组'))
            self.assertEqual(store.db.execute('SELECT member_count FROM conversation_profiles').fetchone()[0],5)
            store.db.close()
    def test_first_snapshot_is_history(self): self.assertEqual(reconcile(None,['a'])['new'],[])
    def test_unchanged(self): self.assertEqual(reconcile(['a','b'],['a','b'])['status'],'unchanged')
    def test_ordered_overlap(self): self.assertEqual(reconcile(['a','b','c'],['b','c','d','d'])['new'],['d','d'])
    def test_gap_is_not_new(self): self.assertEqual(reconcile(['a'],['b'])['status'],'gap')
    def test_repeated_anchor_is_ambiguous(self): self.assertEqual(reconcile(['a','a'],['a','b'])['status'],'ambiguous')
    def test_repeat_notification_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/'test.sqlite3')
            store.snapshot('c','title','avatar','a.png',[{'text':'a'},{'text':'b'}])
            store.snapshot('c','title','avatar','b.png',[{'text':'b'},{'text':'c'}])
            store.snapshot('c','title','avatar','c.png',[{'text':'b'},{'text':'c'}])
            self.assertEqual(store.db.execute('SELECT count(*) FROM messages').fetchone()[0],1)
            store.db.close()
    def test_incomplete_scan_is_never_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/'test.sqlite3')
            scan = store.begin_scan(396)
            self.assertEqual(store.finish_scan(scan,396,390,True,{}),'incomplete')
            store.db.close()
    def test_same_name_different_wechat_ids_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'test.sqlite3')
            scan=store.begin_scan(2)
            for wxid in ('person_a','person_b'):
                store.contact(scan,0,'same name',wxid,'same avatar','avatar.png',{},'profile.png',[],['same name'])
            self.assertEqual(store.db.execute('SELECT count(*) FROM contacts').fetchone()[0],2)
            store.db.close()
    def test_baseline_text_preserved_without_new_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'test.sqlite3')
            store.snapshot('c','title','avatar','a.png',[{'text':'history'}])
            self.assertIn('history',store.db.execute('SELECT payloads_json FROM snapshot_payloads').fetchone()[0])
            self.assertEqual(store.db.execute('SELECT count(*) FROM messages').fetchone()[0],0)
            store.db.close()
    def test_contact_reuse_requires_exact_name_and_avatar_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'test.sqlite3')
            original=store.begin_scan(1)
            cid=store.contact(original,0,'name','person_a','profile','a.png',{'list_avatar_sha':'photo'},'profile.png',[],['name'])
            seeds=store.contact_seeds(original)
            self.assertNotIn(('other','photo'),seeds)
            self.assertNotIn(('name','changed-photo'),seeds)
            repair=store.begin_scan(1)
            self.assertEqual(store.reuse_contact(repair,0,seeds[('name','photo')]),cid)
            self.assertEqual(store.db.execute('SELECT count(*) FROM contacts').fetchone()[0],1)
            store.db.close()
    def test_no_complete_without_persisted_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'test.sqlite3')
            scan=store.begin_scan(1)
            detail={'coverage_grain':'aligned visible list positions'}
            self.assertEqual(store.finish_scan(scan,1,1,True,detail),'incomplete')
            store.contact_position(scan,0,'c','name','avatar','frame.png')
            self.assertEqual(store.finish_scan(scan,1,1,True,detail),'complete')
            store.db.close()
    @unittest.skipUnless((Path(__file__).parent / 'message_context_fixture.json').is_file(), "私有视觉夹具不随开源版发布")
    def test_live_ocr_history_noise_recovers_tail_once(self):
        fixture=json.loads((Path(__file__).parent/'message_context_fixture.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'test.sqlite3')
            store.snapshot('c','Demo User','avatar','before.png',fixture['previous']['payloads'])
            first=store.snapshot('c','Demo User','avatar','current.png',fixture['current']['payloads'])
            second=store.snapshot('c','Demo User','avatar','again.png',fixture['current']['payloads'])
            self.assertEqual(first['status'],'append')
            self.assertEqual(first['overlap'],15)
            self.assertEqual(second['status'],'unchanged')
            rows=[json.loads(r[0]) for r in store.db.execute('SELECT payload_json FROM messages')]
            self.assertEqual([p['text'] for p in rows],['17:18','MAA0926A'])
            self.assertEqual(sum(p['text']=='MAA0926A' for p in rows),1)
            self.assertEqual(store.db.execute('SELECT count(*) FROM snapshot_payloads').fetchone()[0],3)
            store.db.close()

class ActivationBaselineTests(unittest.TestCase):
    def test_old_review_card_can_become_read_only_history_without_new_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'test.sqlite3')
            def item(text,sha,safe=True):
                return {'text':text,'direction':'incoming','reply_safe':safe,'message_kind':'text','visual':{'sha256':sha,'verified':True,'box':[545,100,100,36]}}
            old=[item('anchor one','a'*64),item('anchor two','b'*64)]
            store.snapshot('cid','Demo User','avatar','first',old)
            history=old+[item('unsupported invitation','c'*64,False),item('/菜单','d'*64)]
            self.assertEqual(store.snapshot('cid','Demo User','avatar','review',history)['status'],'review')
            result=store.snapshot('cid','Demo User','avatar','activation',history,activation_baseline=True)
            self.assertEqual(result['status'],'baseline');self.assertEqual(result['new'],[])
            self.assertEqual(store.db.execute('select count(*) from messages').fetchone()[0],0)
            new=history+[item('/菜单','e'*64)]
            self.assertEqual(store.snapshot('cid','Demo User','avatar','next',new)['status'],'append')
            self.assertEqual(store.db.execute('select count(*) from messages').fetchone()[0],1)
            bad=new+[item('unstable new text','f'*64,False)]
            self.assertEqual(store.snapshot('cid','Demo User','avatar','bad',bad)['status'],'review')
            store.db.close()


class VerifiedPostSendContextTests(unittest.TestCase):
    @unittest.skipUnless((Path(__file__).parent / 'post_send_context_fixture.json').is_file(), "私有视觉夹具不随开源版发布")
    def fixture(self):
        return json.loads((Path(__file__).parent/'post_send_context_fixture.json').read_text(encoding='utf8'))

    def setup_context(self,store,fixture,status='succeeded',include_incoming=False):
        from unittest.mock import patch
        cid=fixture['conversation_id']
        with patch('store.time.time',return_value=100):store.snapshot(cid,'开发组(4)','avatar','original.png',fixture['old'])
        outgoing=fixture['manual'][-1]
        proof={'status':'sent','reason':'MEDIA_NEW_RIGHT_BUBBLE_AND_EMPTY_DRAFT_VERIFIED',
               'evidence':{'title_verified':True,'outgoing_verified':True},
               'diagnostic':{'outgoing':outgoing,'preclick_target':{'title':'开发组(4)','title_verified':True}}}
        with patch('store.time.time',return_value=150):
            store.queue_gui({'id':'delivery','action':'send_delivery','params':{'conversation_id':cid},'expires_at':1000})
            store.gui_result('delivery',status,proof)
        values=fixture['manual'] if not include_incoming else fixture['manual']+[fixture['current'][-1]]
        with patch('store.time.time',return_value=200):store.manual_view(cid,'开发组(4)','avatar','after-send.png',values)
        return cid

    def test_real_old_manual_new_chain_emits_only_human_1803_menu_once(self):
        fixture=self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'state.sqlite');cid=self.setup_context(store,fixture)
            first=store.snapshot(cid,'开发组(4)','avatar','1803.png',fixture['current'])
            self.assertEqual((first['status'],first['overlap']),('append',16))
            self.assertEqual(store.db.execute('SELECT count(*) FROM context_baselines').fetchone()[0],1)
            incoming=[json.loads(r[0]) for r in store.db.execute('SELECT payload_json FROM messages') if json.loads(r[0]).get('direction')=='incoming']
            self.assertEqual([p['text'] for p in incoming],['/菜单'])
            self.assertEqual(incoming[0]['visual']['box'],[545,828,63,36])
            again=store.snapshot(cid,'开发组(4)','avatar','same.png',fixture['current'])
            self.assertEqual(again['status'],'unchanged')
            self.assertEqual(store.db.execute("SELECT count(*) FROM messages WHERE json_extract(payload_json,'$.direction')='incoming'").fetchone()[0],1)
            store.db.close()

    def test_manual_new_incoming_or_terminal_failure_never_advances_context(self):
        fixture=self.fixture()
        for status,incoming in [('failed',False),('succeeded',True)]:
            with tempfile.TemporaryDirectory() as directory:
                store=Store(Path(directory)/'state.sqlite');cid=self.setup_context(store,fixture,status,incoming)
                self.assertIsNone(store.promote_manual_sent_context(cid))
                self.assertEqual(store.db.execute('SELECT count(*) FROM context_baselines').fetchone()[0],0)
                self.assertEqual(store.db.execute('SELECT count(*) FROM messages').fetchone()[0],0)
                store.db.close()

    def test_matching_outgoing_with_wrong_verified_title_is_rejected(self):
        fixture=self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'state.sqlite');cid=self.setup_context(store,fixture)
            proof=json.loads(store.db.execute('SELECT detail_json FROM gui_commands').fetchone()[0]);proof['diagnostic']['preclick_target']['title']='other'
            store.db.execute('UPDATE gui_commands SET detail_json=?',(json.dumps(proof),));store.db.commit()
            self.assertIsNone(store.promote_manual_sent_context(cid));store.db.close()

    def test_rejected_repeated_overlap_cannot_fall_back_to_raw_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'state.sqlite')
            item={'text':'/菜单','direction':'incoming','message_kind':'text','reply_safe':True,
                  'visual':{'verified':True,'sha256':'a'*64,'box':[545,200,63,36]}}
            store.snapshot('c','group','avatar','missing-before.png',[item,item])
            result=store.snapshot('c','group','avatar','missing-after.png',[item,item])
            self.assertEqual(result['status'],'gap');store.db.close()
