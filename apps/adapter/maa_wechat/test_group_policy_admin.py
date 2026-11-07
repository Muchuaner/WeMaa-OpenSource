import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from group_policy import configuration,apply_configuration,allowed
from store import Store
class PolicyAdminTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);(self.root/'data').mkdir()
  self.store=Store(self.root/'data/wechat.sqlite3')
  self.cid='a'*64;self.target={'name':'开发组','conversation_title':'开发组(4)','avatar_sha':'b'*64}
  self.store.route(self.cid,self.target);self.store.observe_group(self.cid,'开发组','开发组(4)')
  self.path=self.root/'data/group-whitelist.json';self.path.write_text(json.dumps({'enabled':True,'groups':[dict(self.target,conversation_id=self.cid)],'private_chats_allowed':True}))
 def tearDown(self):self.store.db.close();self.temp.cleanup()
 def apply(self,ids,revision=None):return apply_configuration(self.root,{'conversation_ids':ids,'expected_revision':revision or configuration(self.root)['revision']})
 def test_live_identity_is_resolved_locally(self):
  result=self.apply([self.cid]);g=result['group_whitelist']['groups'][0]
  self.assertEqual(g,{'name':'开发组','conversation_id':self.cid,'avatar_sha':'b'*64})
  self.assertTrue(allowed(self.root,self.target,self.cid))
 def test_empty_policy_blocks_groups_preserves_private(self):
  result=self.apply([]);self.assertEqual(result['group_whitelist']['groups'],[])
  self.assertFalse(allowed(self.root,self.target,self.cid));self.assertTrue(allowed(self.root,{'name':'Demo User'}))
 def test_unknown_and_private_groups_preserve_existing_file(self):
  original=self.path.read_bytes();private='c'*64;self.store.route(private,{'name':'Demo User','avatar_sha':'b'*64,'conversation_title':'Demo User'})
  for ids in [[private],['d'*64],[self.cid,self.cid],['bad']]:
   with self.assertRaises(ValueError):self.apply(ids)
   self.assertEqual(self.path.read_bytes(),original)
 def test_stale_revision_cannot_overwrite(self):
  revision=configuration(self.root)['revision'];self.apply([])
  with self.assertRaisesRegex(ValueError,'CHANGED_REFRESH'):self.apply([self.cid],revision)
  self.assertEqual(configuration(self.root)['groups'],[])
 def test_apply_from_admin_agent_while_worker_running_zero_gui(self):
  import admin_agent,time
  command={'id':'d'*32,'run_id':'maa-web-test','expires_at':time.time()+90,'action':'set_group_whitelist','params':{'conversation_ids':[self.cid],'expected_revision':configuration(self.root)['revision']}}
  with patch('admin_agent.ROOT',self.root),patch('admin_agent.runtime_state',return_value={'old_gateway_state':'Disabled','worker_state':'Running','trial_state':'Ready'}),patch('admin_agent.powershell') as shell:
   result=admin_agent.execute(command)
  shell.assert_not_called();self.assertEqual(result['status'],'applied')
