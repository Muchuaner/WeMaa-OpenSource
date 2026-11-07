import json,tempfile,time,unittest
from pathlib import Path
from store import Store
from contact_identity import link_verified_chat
class ContactIdentityTests(unittest.TestCase):
 def test_changed_photo_preserves_private_history_only_after_exact_wxid(self):
  with tempfile.TemporaryDirectory() as t:
   s=Store(Path(t)/'db');old='a'*64;new='b'*64
   s.db.execute('INSERT INTO contacts(id,wechat_id,identity_status,facts_json) VALUES(?,?,?,?)',('wx:one','one','ocr_wechat_id',json.dumps({'scope':'personal'})))
   s.manual_view(old,'Demo User','old','oldframe',[]);s.manual_view(new,'Demo User','new','newframe',[])
   s.db.execute('INSERT INTO contact_chat_links VALUES(?,?,?)',('wx:one',old,time.time()-10));s.db.commit()
   target=dict(name='Demo User',conversation_title='Demo User',avatar_sha='new');proof=dict(wechat_id='two',stable=True,profile_sha256='f'*64,observation_sha256='e'*64)
   with self.assertRaisesRegex(ValueError,'NOT_VERIFIED'):link_verified_chat(s,'wx:one',new,target,proof)
   self.assertEqual(s.canonical(new),new)
   proof['wechat_id']='one'
   with self.assertRaisesRegex(ValueError,'REQUIRES_VERIFIED_HISTORY'):link_verified_chat(s,'wx:one',new,target,proof)
   self.assertEqual(s.canonical(new),new)
   old_proof=dict(proof,contact_id='wx:one',conversation_id=old)
   s.db.execute('CREATE TABLE IF NOT EXISTS contact_chat_history(contact_id TEXT,conversation_id TEXT,seen REAL,proof_json TEXT,PRIMARY KEY(contact_id,conversation_id))')
   s.db.execute('INSERT INTO contact_chat_history VALUES(?,?,?,?)',('wx:one',old,1,json.dumps(old_proof)));s.db.commit()
   self.assertEqual(link_verified_chat(s,'wx:one',new,target,proof),old)
   self.assertEqual(s.canonical(new),old)
   self.assertEqual(s.db.execute('SELECT frame_path FROM manual_views WHERE conversation_id=?',(old,)).fetchone()[0],'newframe')
   self.assertEqual(json.loads(s.db.execute('SELECT target_json FROM conversation_routes WHERE conversation_id=?',(old,)).fetchone()[0])['avatar_sha'],'new')
   self.assertEqual(link_verified_chat(s,'wx:one',new,target,proof),old)
   target['conversation_title']='Tom(54)'
   with self.assertRaisesRegex(ValueError,'NOT_VERIFIED'):link_verified_chat(s,'wx:one',new,target,proof)
   s.db.close()

 def test_name_typo_with_same_photo_uses_unique_private_route(self):
  with tempfile.TemporaryDirectory() as t:
   s=Store(Path(t)/'db');old='a'*64
   s.route(old,dict(name='ABCDEFGHIJ',conversation_title='ABCDEFGHIJ',avatar_sha='photo'))
   self.assertEqual(s.group_identity('ABCXEFYHIZ','ABCXEFYHIZ','photo'),old)
   self.assertNotEqual(s.group_identity('ABCXEFYHIZ','ABCXEFYHIZ','otherphoto'),old)
   s.route('b'*64,dict(name='ABCXEFYHIZ',conversation_title='ABCXEFYHIZ',avatar_sha='photo'))
   with self.assertRaisesRegex(ValueError,'PRIVATE_NAME_AMBIGUOUS'):s.group_identity('ABCXEFYHIZ','ABCXEFYHIZ','photo')
   s.db.close()


class BindingUpdateTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(Path(self.tmp.name)/'db');self.cid='a'*64
  self.s.db.execute('INSERT INTO contacts(id,wechat_id,identity_status,facts_json) VALUES(?,?,?,?)',('wx:one','one','ocr_wechat_id','{"scope":"personal"}'))
  self.s.manual_view(self.cid,'Demo User','photo','frame',[])
  self.target=dict(name='Demo User',conversation_title='Demo User',avatar_sha='photo')
  self.proof=dict(wechat_id='one',stable=True,profile_sha256='e'*64,observation_sha256='f'*64)
  self.s.db.execute('INSERT INTO contact_chat_links VALUES(?,?,?)',('wx:one',self.cid,1))
  self.s.db.execute('CREATE TABLE contact_chat_history(contact_id TEXT,conversation_id TEXT,seen REAL,proof_json TEXT,PRIMARY KEY(contact_id,conversation_id))')
  self.s.db.execute('INSERT INTO contact_chat_history VALUES(?,?,?,?)',('wx:one',self.cid,1,'{"source":"previous_verified_profile_link"}'));self.s.db.commit()
 def tearDown(self):self.s.db.close();self.tmp.cleanup()
 def state(self):return self.s.db.execute('SELECT * FROM contact_chat_history').fetchall(),self.s.db.execute('SELECT * FROM contact_chat_links').fetchall()
 def test_fresh_proof_updates_placeholder_repeat_is_idempotent(self):
  link_verified_chat(self.s,'wx:one',self.cid,self.target,self.proof)
  first=self.state();count=self.s.db.execute('SELECT count(*) FROM events').fetchone()[0]
  proof=json.loads(first[0][0][3]);self.assertTrue(proof['stable']);self.assertEqual(proof['conversation_id'],self.cid)
  self.assertEqual(link_verified_chat(self.s,'wx:one',self.cid,self.target,self.proof),self.cid)
  self.assertEqual(self.state(),first);self.assertEqual(self.s.db.execute('SELECT count(*) FROM events').fetchone()[0],count)
  audit=json.loads(self.s.db.execute("SELECT detail_json FROM events WHERE kind='private_profile_route_linked'").fetchone()[0])
  self.assertEqual(audit['previous'][0]['proof']['source'],'previous_verified_profile_link')
 def test_missing_proof_preserves_rows_and_audits(self):
  for key in ('stable','wechat_id','profile_sha256','observation_sha256'):
   proof=dict(self.proof);proof.pop(key);before=self.state()
   with self.assertRaisesRegex(ValueError,'NOT_VERIFIED'):link_verified_chat(self.s,'wx:one',self.cid,self.target,proof)
   self.assertEqual(self.state(),before)
  self.assertEqual(self.s.db.execute("SELECT count(*) FROM events WHERE kind='private_profile_binding_blocked'").fetchone()[0],4)
 def test_account_conflict_cannot_be_overwritten(self):
  self.s.db.execute('UPDATE contact_chat_history SET proof_json=?',(json.dumps(dict(self.proof,wechat_id='foreign')),));self.s.db.commit();before=self.state()
  with self.assertRaisesRegex(ValueError,'CONFLICT'):link_verified_chat(self.s,'wx:one',self.cid,self.target,self.proof)
  self.assertEqual(self.state(),before)
 def test_other_contact_same_conversation_blocks(self):
  self.s.db.execute('INSERT INTO contact_chat_links VALUES(?,?,?)',('wx:other',self.cid,2));self.s.db.commit();before=self.state()
  with self.assertRaisesRegex(ValueError,'CONFLICT'):link_verified_chat(self.s,'wx:one',self.cid,self.target,self.proof)
  self.assertEqual(self.state(),before)
 def test_wrong_chat_and_wrong_scope_do_not_bind(self):
  for target in (dict(self.target,avatar_sha='other'),dict(self.target,conversation_title='other')):
   before=self.state()
   with self.assertRaisesRegex(ValueError,'NOT_VERIFIED'):link_verified_chat(self.s,'wx:one',self.cid,target,self.proof)
   self.assertEqual(self.state(),before)
  self.s.db.execute('UPDATE contacts SET facts_json=?',('{"scope":"enterprise"}',));self.s.db.commit()
  with self.assertRaisesRegex(ValueError,'NOT_VERIFIED'):link_verified_chat(self.s,'wx:one',self.cid,self.target,self.proof)
 def test_alias_cycle_blocks_without_merging(self):
  self.s.db.execute('INSERT INTO group_aliases VALUES(?,?,?,?)',(self.cid,'b'*64,'bad',1));self.s.db.execute('INSERT INTO group_aliases VALUES(?,?,?,?)',('b'*64,self.cid,'bad',1));self.s.db.commit();before=self.state()
  with self.assertRaisesRegex(ValueError,'ALIAS_CYCLE'):link_verified_chat(self.s,'wx:one',self.cid,self.target,self.proof)
  self.assertEqual(self.state(),before)

