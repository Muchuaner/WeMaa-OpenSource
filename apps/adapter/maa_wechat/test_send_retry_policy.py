import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from send import send_verified, retryable_send
from group_policy import allowed

class RetryPolicyTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);(self.root/"data").mkdir()
 def tearDown(self):self.temp.cleanup()
 def blocked(self,reason='MAA_BLACK_FRAME',click=False,draft=False):
  return {'status':'blocked','reason':reason,'diagnostic':{'send_click_attempted':click,'draft_retained':draft}}
 def test_exactly_five_retries_and_durable_attempts(self):
  with patch('send._send_verified_once',side_effect=lambda *a,**k:self.blocked()) as once,patch('time.sleep'):
   result=send_verified(None,self.root,'test',{'event_id':'a','name':'Demo User'})
  self.assertEqual(once.call_count,6)
  self.assertEqual(len(result['diagnostic']['attempts']),6)
  self.assertEqual(len((self.root/'data/send-attempts.jsonl').read_text().splitlines()),6)
 def test_success_stops_without_extra_send(self):
  sent={'status':'sent','evidence':{'outgoing_verified':True},'diagnostic':{}}
  with patch('send._send_verified_once',side_effect=[self.blocked(),sent]) as once,patch('time.sleep'):
   result=send_verified(None,self.root,'test',{'event_id':'a','name':'Demo User'})
  self.assertEqual(once.call_count,2);self.assertEqual(result['status'],'sent')
 def test_uncertain_identity_and_drafts_never_retry(self):
  for result in [dict(self.blocked(),status='uncertain'),self.blocked(click=True),self.blocked(draft=True),self.blocked('target_not_verified'),self.blocked('existing_or_unreadable_draft'),{'status':'blocked','reason':'MAA_BLACK_FRAME'}]:
   self.assertFalse(retryable_send(result))
 def test_nonwhitelisted_group_zero_actions(self):
  with patch('send._send_verified_once') as once:
   result=send_verified(None,self.root,'test',{'name':'其他群','conversation_title':'其他群(4)'})
  once.assert_not_called();self.assertEqual(result['reason'],'GROUP_NOT_WHITELISTED')
 def test_default_and_identity_bound_config(self):
  target={'name':'开发组','conversation_title':'开发组(4)','avatar_sha':'a'*64}
  self.assertTrue(allowed(self.root,target));self.assertTrue(allowed(self.root,{'name':'Demo User'}))
  path=self.root/'data/group-whitelist.json'
  path.write_text(json.dumps({'enabled':True,'groups':[{'name':'开发组','conversation_id':'cid','avatar_sha':'a'*64}]}))
  self.assertTrue(allowed(self.root,target,'cid'));self.assertTrue(allowed(self.root,target,'other'))
  self.assertTrue(allowed(self.root,dict(target,avatar_sha='b'*64),'cid'))
  self.assertFalse(allowed(self.root,dict(target,name='其他群'),'other'))
  path.write_text('{}');self.assertFalse(allowed(self.root,target,'cid'))

class MediaRetryTests(RetryPolicyTests):
 def test_media_retries_before_import_only(self):
  from media_send import send_media_verified
  with patch('media_send._send_media_once',side_effect=lambda *a,**k:self.blocked()) as once,patch('time.sleep'):
   result=send_media_verified(None,self.root,'test',{'name':'Demo User'},{'id':'media'})
  self.assertEqual(once.call_count,6)
  with patch('media_send._send_media_once',return_value=self.blocked(draft=True)) as once:
   send_media_verified(None,self.root,'test',{'name':'Demo User'},{'id':'media'})
  self.assertEqual(once.call_count,1)
