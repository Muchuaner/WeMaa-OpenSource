import json,tempfile,time,unittest
from pathlib import Path
from store import Store
from context_recovery import resolve_visible,review_hash,retry_source

class RecoveryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(Path(self.tmp.name)/'db');self.cid='c'*64
  self.p=[{'direction':'incoming','message_kind':'text','text':'/菜单'}]
  self.s.route(self.cid,{'name':'测试','avatar_sha':'photo'})
  self.s.manual_view(self.cid,'测试','photo','frame',self.p)
 def tearDown(self):self.s.db.close();self.tmp.cleanup()
 def test_changed_or_unaccounted_input_keeps_gap(self):
  for expected,decisions in [('x'*64,{}),(review_hash(self.p),{})]:
   with self.assertRaises(ValueError):resolve_visible(self.s,self.cid,expected,decisions,'a'*32)
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM context_baselines').fetchone()[0],0)
 def test_missing_or_other_chat_send_cannot_be_claimed(self):
  dec={'0':dict(disposition='sent',reason='menu',delivery_id='d'*32)}
  with self.assertRaisesRegex(ValueError,'SEND_NOT_VERIFIED'):resolve_visible(self.s,self.cid,review_hash(self.p),dec,'a'*32)
  self.s.queue_gui(dict(id='d'*32,action='send_message',params={'conversation_id':'e'*64},expires_at=time.time()+60))
  self.s.gui_result('d'*32,'succeeded',{'status':'sent','conversation_id':'e'*64})
  with self.assertRaisesRegex(ValueError,'WRONG_CONVERSATION'):resolve_visible(self.s,self.cid,review_hash(self.p),dec,'a'*32)
 def test_review_baseline_preserves_history_and_capture_obligation(self):
  from inbound_work import InboundWork
  w=InboundWork(self.s);w.observe([dict(key='row',name='测试',avatar_sha='photo',unread=True)])
  w.update('row','captured','captured')
  dec={'0':dict(disposition='no_reply_needed',reason='operator reviewed test probe')}
  with self.assertRaisesRegex(ValueError,'CAPTURE_REQUIRES'):resolve_visible(self.s,self.cid,review_hash(self.p),dec,'a'*32)
  w.update('row','review','context_gap_not_verified')
  old=self.s.db.execute("INSERT INTO snapshots(conversation_id,status,seen) VALUES(?,'gap',?)",(self.cid,time.time()-1)).lastrowid;self.s.db.commit()
  resolve_visible(self.s,self.cid,review_hash(self.p),dec,'a'*32)
  self.assertEqual(self.s.db.execute('SELECT status FROM snapshots WHERE id=?',(old,)).fetchone()[0],'gap')
  self.assertEqual(w.get('row')['status'],'resolved')
  result=self.s.snapshot(self.cid,'测试','photo','later',[*self.p,dict(direction='incoming',message_kind='text',text='新问题')])
  self.assertEqual(result['status'],'append');self.assertEqual(len(result['new']),1)
 def test_uncertain_or_duplicate_media_never_retried(self):
  source='d'*32;params=dict(conversation_id=self.cid,asset_id=183,path='file',sha256='f'*64)
  self.s.queue_gui(dict(id=source,action='send_delivery',params=params,expires_at=time.time()+60))
  self.s.gui_result(source,'failed',dict(status='uncertain',reason='TARGET_NOT_FOUND',evidence={}))
  with self.assertRaisesRegex(ValueError,'PROVEN_PRECLICK'):retry_source(self.s,self.cid,source,'a'*32)
  self.s.gui_result(source,'failed',dict(status='failed',reason='TARGET_NOT_FOUND',evidence={}))
  self.assertEqual(retry_source(self.s,self.cid,source,'a'*32)['asset_id'],183)
  self.s.queue_gui(dict(id='b'*32,action='recover_media',params=dict(delivery_id=source),expires_at=time.time()+60));self.s.gui_result('b'*32,'failed',dict(status='uncertain'))
  with self.assertRaisesRegex(ValueError,'ALREADY_ATTEMPTED'):retry_source(self.s,self.cid,source,'a'*32)

 def test_resolved_work_cannot_replay_old_history_and_new_input_gets_new_seed(self):
  from inbound_work import InboundWork
  from test_inbound_work import payload
  w=InboundWork(self.s);row=dict(key='row',name='测试',avatar_sha='photo',unread=True,unread_count=1)
  w.observe([row]);old_seed=w.get('row')['seed'];w.update('row','resolved','operator_reviewed_visible_context')
  self.assertIsNone(w.recover(row,[payload(1)]))
  self.assertFalse(w.capture(row,dict(status='unread_recovery',snapshot_id=1),[payload(1)],[0]))
  self.s.snapshot(self.cid,'测试','photo','old',[payload(1),payload(2)])
  fresh=[payload(1),payload(2),payload(3)];result=self.s.snapshot(self.cid,'测试','photo','fresh',fresh)
  self.assertEqual(result['status'],'append')
  self.assertTrue(w.capture(row,result,fresh,[2]))
  self.assertNotEqual(w.get('row')['seed'],old_seed)
  self.assertEqual(w.get('row')['status'],'captured')
