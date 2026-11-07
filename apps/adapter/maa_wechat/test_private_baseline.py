import hashlib,json,os,sqlite3,tempfile,time,unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock,patch
import numpy as np
from PIL import Image
from store import Store
from contact_identity import link_verified_chat
from inbound_work import InboundWork
from private_baseline import review_manifest,accept_baseline,receive_only
from admin_export import export

class PrivateBaselineTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.s=Store(self.root/'data/wechat.sqlite3');self.cid='b'*64;self.contact='wx:test_user';self.wx='test_user'
  self.avatar=self.root/'data/avatar.png';Image.new('RGB',(24,24),(30,80,40)).save(self.avatar)
  scan=self.s.begin_scan(1);self.s.contact(scan,0,'Demo User',self.wx,'photo',str(self.avatar),{'scope':'personal','list_avatar_sha':'photo'},str(self.avatar),[],[])
  self.s.contact_position(scan,0,self.contact,'Demo User','photo','');self.s.finish_scan(scan,1,1,True,{'scope':'personal','bottom_verified':True})
  self.target=dict(name='Demo User',conversation_title='Demo User',avatar_sha='photo',row_y=113,list_page=0);self.proof=dict(wechat_id=self.wx,stable=True,profile_sha256='f'*64,observation_sha256='e'*64)
  self.work=InboundWork(self.s);self.row=dict(key=self.cid,name='Demo User',avatar_sha='photo',unread=False)
  old=[self.payload(8,'old one'),self.payload(9,'old two')];self.p=[self.payload(1,'历史来信甲'),self.payload(2,'历史来信乙')]
  self.s.snapshot(self.cid,'Demo User','photo',self.frame(old,'old'),old)
  gap=self.s.snapshot(self.cid,'Demo User','photo',self.frame(self.p,'candidate'),self.p);self.assertEqual(gap['status'],'gap');self.sid=gap['snapshot_id']
  self.work.record_context_gap(self.row,gap);self.s.manual_view(self.cid,'Demo User','photo',self.frame(self.p,'visible'),self.p);self.s.route(self.cid,self.target)
  self.s.db.execute('INSERT INTO contact_chat_links VALUES(?,?,?)',(self.contact,self.cid,1));self.s.db.execute('CREATE TABLE contact_chat_history(contact_id TEXT,conversation_id TEXT,seen REAL,proof_json TEXT,PRIMARY KEY(contact_id,conversation_id))');self.s.db.execute('INSERT INTO contact_chat_history VALUES(?,?,?,?)',(self.contact,self.cid,1,'{"source":"placeholder"}'));self.s.db.commit()
 def tearDown(self):self.s.db.close();self.tmp.cleanup()
 def payload(self,n,text):
  crop=np.full((30,140,3),[(n*23)%255,(n*43)%255,(n*67)%255],dtype=np.uint8)
  return dict(direction='incoming',message_kind='text',text=text,source='maa_native_clipboard',reply_safe=True,visual=dict(verified=True,sha256=hashlib.sha256(crop.tobytes()).hexdigest(),box=[550,100+n*45,140,30]),text_copy=dict(verified=True,method='native_context_menu',sha256=hashlib.sha256(text.encode()).hexdigest()))
 def frame(self,ps,name):
  rgb=np.full((1040,1920,3),245,dtype=np.uint8)
  for p in ps:
   x,y,w,h=p['visual']['box'];n=(y-100)//45;rgb[y:y+h,x:x+w]=[(n*23)%255,(n*43)%255,(n*67)%255]
  path=self.root/'data'/(name+'.png');Image.fromarray(rgb).save(path);return str(path)
 def preview(self):return review_manifest(self.s.db,self.contact,self.cid,self.wx,self.sid)
 def params(self,preview=None):
  v=preview or self.preview();return dict(contact_id=self.contact,wechat_id=self.wx,conversation_id=self.cid,snapshot_id=self.sid,review_hash=v['review_hash'],reason='Human approves listed historical range, keep evidence',receive_only=True)
 def claim(self,params):
  cmd='a'*32;self.s.queue_gui(dict(id=cmd,action='accept_private_baseline',params=params,expires_at=time.time()+300));self.s.gui_result(cmd,'executing',{'status':'executing'});return cmd
 def accept(self):
  v=self.preview();link_verified_chat(self.s,self.contact,self.cid,self.target,self.proof);p=self.params(v);c=self.claim(p);return accept_baseline(self.s,p,c,self.proof),p,c
 def report(self):return export(self.root,{'worker_state':'Running','trial_state':'Ready','old_gateway_state':'Disabled'})[0]
 def test_new_capture_after_virtual_resolution_gets_fresh_seed(self):
  original=self.work.get(self.cid);baseline,_,_=self.accept()
  self.assertEqual(self.work.get(self.cid)['status'],'resolved')
  fresh=[*self.p,self.payload(3,'new message after activation')]
  result=self.s.snapshot(self.cid,'Demo User','photo',self.frame(fresh,'new'),fresh)
  self.assertEqual(result['status'],'append')
  self.assertTrue(self.work.capture(self.row,result,fresh,[2]))
  current=self.work.get(self.cid)
  self.assertNotEqual(current['seed'],original['seed'])
  self.assertGreater(current['seen'],original['seen'])
  self.assertEqual(current['status'],'captured')
  audit=json.loads(self.s.db.execute("SELECT detail_json FROM events WHERE kind='inbound_new_occurrence_seed'").fetchone()[0])
  self.assertEqual(audit['previous_work']['superseded_by'],baseline)
  self.assertEqual(audit['previous_work']['seed'],original['seed'])
 def test_preview_read_only_lists_scope_and_preservation(self):
  n=self.s.db.execute('SELECT count(*) FROM snapshots').fetchone()[0];db=sqlite3.connect((self.root/'data/wechat.sqlite3').as_uri()+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
  v=review_manifest(db,self.contact,self.cid,self.wx,self.sid);db.close();self.assertEqual(v['manifest']['skip_history']['count'],1);self.assertEqual([r['text'] for r in v['manifest']['incoming_history']],['历史来信甲','历史来信乙']);self.assertTrue(v['manifest']['effects']['old_work_preserved']);self.assertEqual(n,self.s.db.execute('SELECT count(*) FROM snapshots').fetchone()[0])
 def test_worker_claim_fresh_profile_and_persisted_proof_required(self):
  p=self.params();n=self.s.db.execute('SELECT count(*) FROM snapshots').fetchone()[0]
  with self.assertRaisesRegex(ValueError,'WORKER_INTENT'):accept_baseline(self.s,p,'a'*32,self.proof)
  c=self.claim(p)
  with self.assertRaisesRegex(ValueError,'FRESH_PROFILE'):accept_baseline(self.s,p,c,dict(self.proof,stable=False))
  with self.assertRaisesRegex(ValueError,'PROFILE_NOT_PERSISTED'):accept_baseline(self.s,p,c,self.proof)
  self.assertEqual(n,self.s.db.execute('SELECT count(*) FROM snapshots').fetchone()[0])
 def test_wrong_hash_never_advances(self):
  link_verified_chat(self.s,self.contact,self.cid,self.target,self.proof);p=self.params();p['review_hash']='0'*64;c=self.claim(p)
  with self.assertRaisesRegex(ValueError,'REVIEW_CHANGED'):accept_baseline(self.s,p,c,self.proof)
  self.assertEqual(self.work.get(self.cid)['status'],'review')
 def test_copy_failure_and_frame_tamper_block_candidate(self):
  ps=deepcopy(self.p);ps[0]['text_copy']['verified']=False;self.s.db.execute('UPDATE snapshot_payloads SET payloads_json=? WHERE snapshot_id=?',(json.dumps(ps),self.sid));self.s.db.commit()
  with self.assertRaisesRegex(ValueError,'TEXT_NOT_VERIFIED'):self.preview()
  self.s.db.execute('UPDATE snapshot_payloads SET payloads_json=? WHERE snapshot_id=?',(json.dumps(self.p),self.sid));self.s.db.commit();Image.new('RGB',(1920,1040),'black').save(self.root/'data/candidate.png')
  with self.assertRaisesRegex(ValueError,'FRAME_NOT_VERIFIED'):self.preview()
 def test_new_input_requires_new_review(self):
  p=self.params();link_verified_chat(self.s,self.contact,self.cid,self.target,self.proof);new=[*self.p,self.payload(3,'新的未批准来信')];self.s.manual_view(self.cid,'Demo User','photo',self.frame(new,'changed'),new);c=self.claim(p)
  with self.assertRaisesRegex(ValueError,'CONTEXT_CHANGED'):accept_baseline(self.s,p,c,self.proof)
  self.assertEqual(self.work.get(self.cid)['status'],'review')
 def test_disappeared_unreviewed_observation_cannot_be_skipped(self):
  p=self.params();link_verified_chat(self.s,self.contact,self.cid,self.target,self.proof);new=[*self.p,self.payload(3,'中间出现的新来信')];self.s.snapshot(self.cid,'Demo User','photo',self.frame(new,'middle'),new,reconciliation_guard=lambda r,ps:dict(r,status='review',new=[]));c=self.claim(p)
  with self.assertRaisesRegex(ValueError,'UNREVIEWED_OBSERVATION'):accept_baseline(self.s,p,c,self.proof)
 def test_captured_obligation_blocks(self):
  self.work.update(self.cid,'captured','captured',self.sid);p=self.params();self.assertTrue(self.preview()['manifest']['blocked_by']);link_verified_chat(self.s,self.contact,self.cid,self.target,self.proof);c=self.claim(p)
  with self.assertRaisesRegex(ValueError,'PENDING_OBLIGATION'):accept_baseline(self.s,p,c,self.proof)
 def test_old_review_preserved_one_chat_only_idempotent(self):
  other='c'*64;r=self.s.snapshot(other,'Other','another','',[]);self.work.observe([dict(key=other,unread=True)]);self.work.update(other,'review','context_gap_not_verified',r['snapshot_id'])
  sid,p,c=self.accept();self.assertEqual(self.s.db.execute('SELECT status FROM snapshots WHERE id=?',(self.sid,)).fetchone()[0],'gap');self.assertEqual(self.s.db.execute('SELECT status FROM inbound_work WHERE row_key=?',(self.cid,)).fetchone()[0],'review')
  self.assertEqual(self.work.get(self.cid)['status'],'resolved');self.assertEqual(self.work.get(other)['status'],'review');self.assertTrue(receive_only(self.s.db,self.cid));self.assertFalse(receive_only(self.s.db,other));self.assertEqual(self.s.db.execute('SELECT count(*) FROM messages').fetchone()[0],0)
  self.assertEqual(accept_baseline(self.s,p,c,self.proof),sid);self.assertEqual(self.s.db.execute('SELECT count(*) FROM context_baselines').fetchone()[0],1)
 def test_new_append_export_distinct_same_body_no_auto_dispatch(self):
  before=self.report();self.assertNotIn('private_contact',before['conversations'][0]);sid,p,c=self.accept();middle=self.report();self.assertEqual(middle['conversations'][0]['messages'],[])
  new=[*self.p,self.payload(3,'全新唯一验收文字')];r=self.s.snapshot(self.cid,'Demo User','photo',self.frame(new,'new'),new,reconciliation_guard=lambda r,ps:self.work.guard_review_append(self.row,r,ps));self.assertEqual(r['status'],'append')
  messages=self.s.db.execute('SELECT id,payload_json FROM messages').fetchall();self.assertEqual(len(messages),1);self.assertEqual(json.loads(messages[0][1])['text'],'全新唯一验收文字')
  from watch import dispatch_incoming
  client=Mock();creation=Mock();experiment=Mock();runtime=Mock();d=dispatch_incoming([(2,dict(conversation_id=self.cid,event_id='f'*64,text=new[2]['text']))],self.s,client,creation,experiment,runtime,None,new,r['snapshot_id'],self.target,'test')
  self.assertEqual(d['accepted'],[]);client.queue.assert_not_called();creation.queue_event.assert_not_called();self.assertEqual(runtime.mock_calls,[])
  first=self.report();repeat=self.report();self.assertEqual(first['conversations'][0]['messages'][0]['id'],repeat['conversations'][0]['messages'][0]['id'])
  again=deepcopy(new[2]);again['visual']['box']=[550,280,140,30];rgb=np.asarray(Image.open(self.root/'data/new.png').convert('RGB')).copy();rgb[280:310,550:690]=rgb[235:265,550:690];path=self.root/'data/repeated.png';Image.fromarray(rgb).save(path)
  r=self.s.snapshot(self.cid,'Demo User','photo',str(path),[*new,again],reconciliation_guard=lambda r,ps:self.work.guard_review_append(self.row,r,ps));self.assertEqual(r['status'],'append');after=self.report();ids=[m['id'] for m in after['conversations'][0]['messages']];self.assertEqual(len(ids),2);self.assertEqual(len(set(ids)),2);self.assertEqual(self.s.db.execute('SELECT count(*) FROM reply_outbox').fetchone()[0],0)
  if os.environ.get('WECHAT_MAA_CHAIN_FIXTURE'):Path(os.environ['WECHAT_MAA_CHAIN_FIXTURE']).write_text(json.dumps(dict(now=time.time(),review=before,baseline=middle,first=first,repeat=repeat,same_body=after),ensure_ascii=False),encoding='utf-8')
 def test_manual_intent_explicit_scope_hash_reason_receive_only(self):
  from manual_gui import validate_intent
  p=self.params();c=dict(id='a'*32,action='accept_private_baseline',params=p,expires_at=time.time()+60);validate_intent(c)
  for key,value in [('receive_only',False),('reason',''),('snapshot_id',True),('review_hash','bad'),('wechat_id','')]:
   with self.assertRaises(ValueError):validate_intent(dict(c,params=dict(p,**{key:value})))

 def test_expired_confirmation_and_interrupted_intent_never_replay(self):
  from manual_gui import recover_interrupted_gui,process_one
  link_verified_chat(self.s,self.contact,self.cid,self.target,self.proof);p=self.params();c=self.claim(p)
  self.s.db.execute('UPDATE gui_commands SET expires=? WHERE id=?',(time.time()-1,c));self.s.db.commit()
  with self.assertRaisesRegex(ValueError,'EXPIRED'):accept_baseline(self.s,p,c,self.proof)
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM context_baselines').fetchone()[0],0)
  recover_interrupted_gui(self.s)
  with patch('manual_gui.open_contact') as navigation:
   self.assertFalse(process_one(Mock(),self.root,self.s,'restart'));navigation.assert_not_called()
  with self.assertRaisesRegex(ValueError,'EXPIRED'):accept_baseline(self.s,p,c,self.proof)

 def test_sole_worker_confirmation_claims_before_navigation_never_sends(self):
  from manual_gui import process_one
  p=self.params();c='d'*32;self.s.queue_gui(dict(id=c,action='accept_private_baseline',params=p,expires_at=time.time()+300))
  def navigation(*args,proof,**kwargs):
   self.assertEqual(self.s.db.execute('SELECT status FROM gui_commands WHERE id=?',(c,)).fetchone()[0],'executing')
   proof.update(self.proof);link_verified_chat(self.s,self.contact,self.cid,self.target,self.proof)
   return self.cid,self.target
  runtime=Mock()
  with patch('manual_gui.open_contact',side_effect=navigation) as opened,patch('send.send_verified') as sender:
   self.assertTrue(process_one(runtime,self.root,self.s,'test'));self.assertFalse(process_one(runtime,self.root,self.s,'test'));sender.assert_not_called();self.assertEqual(opened.call_count,1)
  row=self.s.db.execute('SELECT status,detail_json FROM gui_commands WHERE id=?',(c,)).fetchone();self.assertEqual(row[0],'succeeded');self.assertEqual(json.loads(row[1])['status'],'reviewed')
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM messages').fetchone()[0],0)
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM reply_outbox').fetchone()[0],0)

 def test_receive_only_final_reply_and_media_gates_precede_network_and_gui(self):
  from reply_client import ReplyClient
  from creation_client import CreationClient
  self.accept();transport=Mock();runtime=Mock();client=ReplyClient(self.root,self.s,transport=transport)
  try:
   body=dict(event_id='f'*64,conversation_id=self.cid,text='test',observed_at=time.time());client.queue(body,self.sid,0,self.target);self.s.reply_update(body['event_id'],'ready',response=dict(reply_id='r',reply_text='must not send',conversation_id=self.cid))
   item=self.s.reply_rows(('ready',))[0];self.assertEqual(client.ready(),[])
   with patch('send.send_verified') as sender:
    self.assertEqual(client.send(item,runtime,'test',self.target)['reason'],'PRIVATE_RECEIVE_ONLY');sender.assert_not_called()
   transport.assert_not_called();self.assertEqual(runtime.mock_calls,[])
  finally:client.close()
  media=CreationClient.__new__(CreationClient);media.store=self.s;media.receipt=Mock();media.api=Mock()
  self.s.queue_gui(dict(id='e'*32,action='send_delivery',params=dict(conversation_id=self.cid),expires_at=time.time()+300))
  self.assertTrue(media.process_one(runtime,'test'));media.api.assert_not_called();self.assertEqual(runtime.mock_calls,[])
  self.assertEqual(json.loads(self.s.db.execute('SELECT detail_json FROM gui_commands WHERE id=?',('e'*32,)).fetchone()[0])['reason'],'PRIVATE_RECEIVE_ONLY')

 def test_verified_identity_does_not_export_review_history(self):
  link_verified_chat(self.s,self.contact,self.cid,self.target,self.proof)
  report=self.report();conversation=report['conversations'][0]
  self.assertEqual(conversation['private_contact']['wechat_id'],self.wx)
  self.assertEqual(conversation['messages'],[]);self.assertEqual(self.work.get(self.cid)['status'],'review')
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM messages').fetchone()[0],0)

 def test_explicit_temporary_activation_skips_history_and_expires(self):
  from manual_gui import validate_intent
  from reply_policy import permits,cutoff
  v=review_manifest(self.s.db,self.contact,self.cid,self.wx,self.sid,receive_only=False)
  p=self.params(v);p.update(receive_only=False,auto_reply_expires_at=time.time()+300)
  link_verified_chat(self.s,self.contact,self.cid,self.target,self.proof)
  command=dict(id='d'*32,action='activate_private_replies',params=p,expires_at=time.time()+300)
  validate_intent(command);self.s.queue_gui(command);self.s.gui_result(command['id'],'executing',{})
  sid=accept_baseline(self.s,p,command['id'],self.proof)
  self.assertFalse(receive_only(self.s.db,self.cid));self.assertEqual(self.work.get(self.cid)['status'],'resolved')
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM messages').fetchone()[0],0)
  at=cutoff(self.s.db,self.cid)
  self.assertFalse(permits(self.s.db,dict(conversation_id=self.cid,observed_at=at-1)))
  self.assertTrue(permits(self.s.db,dict(conversation_id=self.cid,observed_at=at+1)))
  self.assertFalse(permits(self.s.db,dict(conversation_id=self.cid,observed_at=at+1),source_seen=at-1))
  with patch('private_baseline.time.time',return_value=p['auto_reply_expires_at']+1):self.assertTrue(receive_only(self.s.db,self.cid))
  self.assertEqual(self.s.db.execute('SELECT status FROM inbound_work WHERE row_key=?',(self.cid,)).fetchone()[0],'review')

 def test_global_scope_requires_worker_intent_and_preserves_old_baseline(self):
  from reply_policy import activate_all,permits,SCOPE
  from manual_gui import validate_intent
  self.accept();p={'scope':SCOPE,'reason':'User enables private and existing allowlisted group replies prospectively'}
  with self.assertRaisesRegex(ValueError,'WORKER_INTENT'):activate_all(self.s,p,'d'*32)
  cmd=dict(id='d'*32,action='activate_reply_scope',params=p,expires_at=time.time()+300);validate_intent(cmd)
  self.s.queue_gui(cmd);self.s.gui_result(cmd['id'],'executing',{})
  r=activate_all(self.s,p,cmd['id']);self.assertFalse(receive_only(self.s.db,self.cid))
  self.assertFalse(permits(self.s.db,dict(conversation_id=self.cid,observed_at=r['enabled_at']-1)))
  self.assertTrue(permits(self.s.db,dict(conversation_id=self.cid,observed_at=r['enabled_at']+1)))
  self.assertEqual(activate_all(self.s,p,cmd['id']),r)
  self.assertTrue(json.loads(self.s.db.execute('SELECT proof_json FROM context_baselines').fetchone()[0])['receive_only'])
