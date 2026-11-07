import unittest
from pathlib import Path
import unittest
from creation_client import candidate
class CreationCandidates(unittest.TestCase):
 def test_completed_event_polls_immediately_and_download_becomes_ready(self):
  import tempfile,time
  from pathlib import Path
  from concurrent.futures import Future
  from types import SimpleNamespace
  from store import Store
  from creation_client import CreationClient
  with tempfile.TemporaryDirectory() as folder:
   store=Store(Path(folder)/'state.sqlite3');c=CreationClient.__new__(CreationClient);c.store=store
   store.db.execute('CREATE TABLE creation_events(id TEXT PRIMARY KEY,body TEXT,status TEXT,updated REAL)')
   store.db.execute('CREATE TABLE media_receipts(id TEXT PRIMARY KEY,body TEXT,reported INTEGER DEFAULT 0)')
   store.db.execute("INSERT INTO creation_events VALUES('event','{}','pending',0)");store.db.commit()
   calls=[]
   def submit(*args): calls.append(args);return Future()
   c.executor=SimpleNamespace(submit=submit);c.next_event={};c.next_poll=time.time()+1000
   f=Future();f.set_result({});c.futures={'event':(f,'event',None)}
   c.pump();self.assertEqual(calls[0][1],'/creation/deliveries');self.assertTrue(c.delivery_in_flight())
   f=Future();f.set_result({'id':'a'*32,'created_at':time.time()});c.futures={'a'*32:(f,'download',None)}
   c.pump();self.assertTrue(c.delivery_ready());self.assertEqual(c.delivery_health()['pending'],1)
   store.gui_result('a'*32,'failed',{'reason':'MEDIA_EXISTING_DRAFT'})
   self.assertFalse(c.delivery_ready());self.assertEqual(c.delivery_health()['last_result']['reason'],'MEDIA_EXISTING_DRAFT')
   store.db.close()

 def test_explicit_only(self):
  for v in ('/菜单','帮我生成一张海报','/确认 aabb','把上一张背景改亮'):self.assertTrue(candidate(v))
  for v in ('生成图片需要多少积分','怎么生成图片','他说帮我生成一张海报','“帮我生成图片”','生成一张海报可以吗','普通聊天图片好看','不要生成图片','把上一张背景先别改','>帮我生成一张图片'):self.assertFalse(candidate(v))

 def test_receipt_is_small_and_preserves_uncertain(self):
  import sqlite3,json
  from types import SimpleNamespace
  from creation_client import CreationClient
  client=CreationClient.__new__(CreationClient)
  db=sqlite3.connect(':memory:');db.execute('CREATE TABLE media_receipts(id TEXT PRIMARY KEY,body TEXT)')
  client.store=SimpleNamespace(db=db)
  client.receipt('a'*32,{'status':'uncertain','reason':'lost','evidence':{'title_verified':True},'diagnostic':{'frames':['x'*1000]*100}})
  raw=db.execute('SELECT body FROM media_receipts').fetchone()[0]
  self.assertLess(len(raw.encode()),4096);self.assertEqual(json.loads(raw)['status'],'uncertain')
  client.receipt('a'*32,{'status':'sent'})
  self.assertEqual(json.loads(db.execute('SELECT body FROM media_receipts').fetchone()[0])['status'],'uncertain')
  db.close()

 def test_menu_download_uses_fixed_cache_name_and_digest(self):
  import io,tempfile,hashlib
  from pathlib import Path
  from unittest.mock import patch
  from PIL import Image
  from creation_client import CreationClient
  raw=io.BytesIO();Image.new('RGB',(1440,1920),'teal').save(raw,'PNG');content=raw.getvalue()
  response=io.BytesIO(content);response.headers={'Content-Length':str(len(content)),'X-Content-SHA256':hashlib.sha256(content).hexdigest(),'Content-Type':'image/png'}
  with tempfile.TemporaryDirectory() as folder:
   client=CreationClient.__new__(CreationClient);client.root=Path(folder);client.endpoint='https://your-service.example.com/api/v1/wechat-maa';client.token='test-only'
   item={'id':'a'*32,'media_key':'command_menu_v1','asset_id':None,'type':'image'}
   with patch('creation_client.urllib.request.urlopen',return_value=response):result=client.download(item)
   self.assertEqual(Path(result['path']),Path(folder)/'data/media-cache'/('a'*32)/'menu.png')
   self.assertEqual(result['sha256'],hashlib.sha256(content).hexdigest())

 def test_catalog_download_preserves_png_and_private_cache_name(self):
  import io,tempfile,hashlib
  from pathlib import Path
  from unittest.mock import patch
  from PIL import Image
  from creation_client import CreationClient
  raw=io.BytesIO();Image.new('RGB',(1440,1800),'white').save(raw,'PNG');content=raw.getvalue()
  response=io.BytesIO(content);response.headers={'Content-Length':str(len(content)),'X-Content-SHA256':hashlib.sha256(content).hexdigest(),'Content-Type':'image/png'}
  with tempfile.TemporaryDirectory() as folder:
   client=CreationClient.__new__(CreationClient);client.root=Path(folder);client.endpoint='https://your-service.example.com/api/v1/wechat-maa';client.token='test-only'
   item={'id':'a'*32,'media_key':'asset_catalog_v1','asset_id':None,'type':'image'}
   with patch('creation_client.urllib.request.urlopen',return_value=response):result=client.download(item)
   self.assertEqual(Path(result['path']),Path(folder)/'data/media-cache'/('a'*32)/'catalog.png')
   self.assertEqual(result['sha256'],hashlib.sha256(content).hexdigest())

 def test_catalog_cannot_masquerade_as_owned_asset_or_video(self):
  from creation_client import CreationClient
  from unittest.mock import patch
  client=CreationClient.__new__(CreationClient)
  with patch('creation_client.urllib.request.urlopen') as network:
   for item in ({'media_key':'asset_catalog_v1','asset_id':123,'type':'image'},{'media_key':'asset_catalog_v1','type':'video'}):
    with self.assertRaisesRegex(ValueError,'SYSTEM_MEDIA_KEY_INVALID'):client.download(item)
   network.assert_not_called()

 def test_unknown_system_media_is_rejected_before_network(self):
  from creation_client import CreationClient
  from unittest.mock import patch
  client=CreationClient.__new__(CreationClient)
  with patch('creation_client.urllib.request.urlopen') as network:
   for key in ('../../secret','command_menu_v2','https://example.com/x'):
    with self.assertRaisesRegex(ValueError,'SYSTEM_MEDIA_KEY_INVALID'):client.download({'media_key':key})
   network.assert_not_called()


class CreationEventRejections(unittest.TestCase):
 def setUp(self):
  import tempfile,json,time
  from pathlib import Path
  from types import SimpleNamespace
  from concurrent.futures import Future
  from store import Store
  from creation_client import CreationClient
  self.folder=tempfile.TemporaryDirectory();self.root=Path(self.folder.name)
  (self.root/'private').mkdir()
  (self.root/'private/admin-agent.json').write_text(json.dumps({'endpoint':'https://your-service.example.com/api/v1/wechat-maa','token':'test-only'}))
  self.store=Store(self.root/'state.sqlite3');self.client=CreationClient(self.root,self.store)
  self.client.executor.shutdown(wait=True);self.calls=[]
  def submit(*args):self.calls.append(args);return Future()
  self.client.executor=SimpleNamespace(submit=submit,shutdown=lambda **kw:None)
  self.client.next_poll=time.time()+1000
  self.body={'event_id':'a'*64,'conversation_id':'b'*64,'text':'/菜单','observed_at':time.time(),'identity':{'wechat_id':'Tom_verified'}}
  self.client.queue_event(self.body)

 def tearDown(self):
  self.client.close();self.store.db.close();self.folder.cleanup()

 def completed(self,exc,kind='event',key=None):
  from concurrent.futures import Future
  future=Future();future.set_exception(exc)
  self.client.futures={key or self.body['event_id']:(future,kind,None)}
  self.client.pump()

 def http_error(self,status,value):
  import io,json,urllib.error
  raw=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode()
  error=urllib.error.HTTPError('https://your-service.example.com/api/v1/wechat-maa/creation/events',status,'rejected',{},io.BytesIO(raw))
  self.addCleanup(error.close)
  return error

 def api_rejection(self,error):
  from unittest.mock import patch
  from creation_client import CreationEventRejected
  with patch('creation_client.urllib.request.urlopen',side_effect=error):
   with self.assertRaises(CreationEventRejected) as caught:self.client.api('/creation/events',self.body,'POST')
  return caught.exception

 def test_expired_http400_preserves_event_and_request_without_retry_after_restart(self):
  import json,time
  from creation_client import CreationClient
  from concurrent.futures import Future
  from types import SimpleNamespace
  for nested in (True,False):
   value={'code':40001,'message':'只接受最近五分钟的已验证新消息'}
   error=self.http_error(400,{'detail':value} if nested else value)
   rejection=self.api_rejection(error)
   self.assertEqual(rejection.detail['reason'],'CREATION_EVENT_EXPIRED')
   self.assertTrue(rejection.detail['rejected_before_effects']);self.assertTrue(error.fp.closed)
  self.completed(rejection)
  eid=self.body['event_id']
  status,body=self.store.db.execute('SELECT status,body FROM creation_events WHERE id=?',(eid,)).fetchone()
  self.assertEqual(status,'review');self.assertEqual(json.loads(body),self.body);self.assertEqual(self.calls,[])
  evidence=json.loads(self.store.db.execute('SELECT detail_json FROM creation_event_reviews WHERE id=?',(eid,)).fetchone()[0])
  self.assertEqual(evidence['http_status'],400);self.assertEqual(evidence['server_code'],40001)
  self.assertEqual(evidence['rejection_scope'],'this_attempt');self.assertFalse(evidence['retry'])
  self.client.queue_event(dict(self.body,observed_at=time.time()))
  self.assertEqual(json.loads(self.store.db.execute('SELECT body FROM creation_events WHERE id=?',(eid,)).fetchone()[0]),self.body)
  restarted=CreationClient(self.root,self.store);restarted.executor.shutdown(wait=True)
  restarted.executor=SimpleNamespace(submit=lambda *args:self.calls.append(args) or Future(),shutdown=lambda **kw:None)
  restarted.next_poll=time.time()+1000;restarted.pump();restarted.close()
  self.assertEqual(self.calls,[]);self.assertFalse(restarted.delivery_in_flight())
  self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE kind='creation_event_review'").fetchone()[0],1)

 def test_unknown_http400_stops_for_review_without_claiming_no_effects(self):
  import json
  secret='sensitive-response-value'
  rejection=self.api_rejection(self.http_error(400,{'detail':{'code':40001,'message':secret}}))
  self.assertFalse(rejection.detail['rejected_before_effects']);self.assertNotIn('server_message',rejection.detail)
  self.completed(rejection)
  raw=self.store.db.execute('SELECT detail_json FROM creation_event_reviews').fetchone()[0]
  self.assertNotIn(secret,raw);self.assertEqual(json.loads(raw)['response_bytes'],len(json.dumps({'detail':{'code':40001,'message':secret}},ensure_ascii=False).encode()))
  self.assertEqual(self.calls,[])

 def test_html_truncated_or_unreadable_http400_is_retained_for_review(self):
  from creation_client import MAX_REJECTION_BODY
  from unittest.mock import patch
  values=(b'<html>bad request</html>',b'x'*(MAX_REJECTION_BODY+10),b'{"detail":{"code":40001,"message":[]}}')
  for raw in values:
   rejection=self.api_rejection(self.http_error(400,raw))
   self.assertFalse(rejection.detail['rejected_before_effects'])
   self.assertLessEqual(rejection.detail['response_bytes'],MAX_REJECTION_BODY)
   self.assertEqual(rejection.detail['response_truncated'],len(raw)>MAX_REJECTION_BODY)
  self.assertFalse(rejection.detail['response_truncated'])
  error=self.http_error(400,b'')
  with patch.object(error,'read',side_effect=OSError('cannot read')):rejection=self.api_rejection(error)
  self.assertFalse(rejection.detail['response_body_read']);self.completed(rejection)
  self.assertEqual(self.store.db.execute('SELECT status FROM creation_events').fetchone()[0],'review')

 def test_nonretryable_client_errors_stop_but_transient_errors_preserve_backoff(self):
  import urllib.error
  for status in (400,401,403,404,409,410,413,422):
   self.store.db.execute("UPDATE creation_events SET status='pending'");self.store.db.commit()
   # Raw mocked transports are also terminal, without reading on the GUI owner.
   error=self.http_error(status,{'detail':{'code':40001,'message':'只接受最近五分钟的已验证新消息'}})
   self.completed(error)
   self.assertEqual(self.store.db.execute('SELECT status FROM creation_events').fetchone()[0],'review')
   self.assertEqual(error.fp.tell(),0);self.assertNotIn(self.body['event_id'],self.client.next_event)
  for status in (408,425,429,500,502,503):
   self.store.db.execute("UPDATE creation_events SET status='pending'");self.store.db.commit()
   self.completed(self.http_error(status,b''))
   self.assertEqual(self.store.db.execute('SELECT status FROM creation_events').fetchone()[0],'pending')
   self.assertIn(self.body['event_id'],self.client.next_event)
  self.completed(urllib.error.URLError('lost response'))
  self.assertEqual(self.store.db.execute('SELECT status FROM creation_events').fetchone()[0],'pending')
  self.assertEqual(self.calls,[])

 def test_failed_uncertain_and_review_are_never_requeued_or_overwritten(self):
  import json,time
  from creation_client import CreationEventRejected
  from concurrent.futures import Future
  for status in ('failed','uncertain','review'):
   self.store.db.execute('UPDATE creation_events SET status=?',(status,));self.store.db.commit()
   self.client.queue_event(dict(self.body,observed_at=time.time()))
   self.completed(CreationEventRejected({'reason':'CREATION_EVENT_EXPIRED','http_status':400}))
   self.assertEqual(self.store.db.execute('SELECT status FROM creation_events').fetchone()[0],status)
   future=Future();future.set_result({});self.client.futures={self.body['event_id']:(future,'event',None)}
   self.client.pump()
   retained=self.store.db.execute('SELECT status,body FROM creation_events').fetchone()
   self.assertEqual(retained[0],status);self.assertEqual(json.loads(retained[1]),self.body)
  self.assertEqual(self.calls,[])
  self.assertEqual(self.store.db.execute('SELECT count(*) FROM creation_event_reviews').fetchone()[0],0)

 def test_http400_on_receipt_keeps_existing_retry_behavior(self):
  self.completed(self.http_error(400,b'bad receipt'),kind='receipt',key='ack:delivery')
  self.assertEqual(self.store.db.execute('SELECT status FROM creation_events').fetchone()[0],'pending')
  self.assertIn('ack:delivery',self.client.next_event)

 def test_non_event_api_does_not_reclassify_http_error(self):
  import urllib.error
  from unittest.mock import patch
  error=self.http_error(400,{'detail':{'code':40001,'message':'只接受最近五分钟的已验证新消息'}})
  with patch('creation_client.urllib.request.urlopen',side_effect=error):
   with self.assertRaises(urllib.error.HTTPError) as caught:self.client.api('/creation/deliveries/a/claim',{},'POST')
  self.assertIs(caught.exception,error);self.assertEqual(error.fp.tell(),0)

class CreationPostSendContexts(unittest.TestCase):
 @unittest.skipUnless((Path(__file__).parent / 'post_send_context_fixture.json').is_file(), "私有视觉夹具不随开源版发布")
 def test_media_concurrent_input_uses_message_journal_and_current_activation(self):
  import json,tempfile,hashlib
  import numpy as np
  from pathlib import Path
  from PIL import Image
  from store import Store
  from inbound_work import InboundWork
  from inbound_messages import InboundMessages
  from creation_client import CreationClient
  fixture=json.loads((Path(__file__).parent/'post_send_context_fixture.json').read_text(encoding='utf8'))
  for active in (True,False):
   with self.subTest(active=active),tempfile.TemporaryDirectory() as folder:
    root=Path(folder);(root/'data').mkdir()
    (root/'data/reply-runtime.json').write_text(json.dumps({'auto_reply_enabled':active}))
    store=Store(root/'state.sqlite');messages=InboundMessages(store);InboundWork(store)
    cid=fixture['conversation_id'];target={'name':'开发组','conversation_title':'开发组(4)','avatar_sha':'avatar'}
    store.snapshot(cid,target['conversation_title'],'avatar','old.png',fixture['old'],
     on_observation=lambda r,p:messages.record(cid,target,r,p,enabled=active))
    frame=root/'after.png';image=np.full((1040,1920,3),249,dtype=np.uint8)
    Image.fromarray(image[:,:,::-1]).save(frame);sha=hashlib.sha256(image.tobytes()).hexdigest()
    after=fixture['manual']+[fixture['current'][-1]]
    result={'status':'sent','evidence':{'title_verified':True,'outgoing_verified':True,'frame_sha256':sha},
     'diagnostic':{'history_before':fixture['old'],'outgoing':fixture['manual'][-1],
      'frames':[{'stage':'after-1','path':str(frame),'sha256':sha}],
      'after_payloads':[{'stage':'after-1','payloads':after}]}}
    item={'id':'a'*32,'conversation_id':cid,'media_key':'command_menu_v1'}
    store.queue_gui({'id':item['id'],'action':'send_delivery','params':item,'expires_at':99999999999})
    store.gui_result(item['id'],'succeeded',result)
    client=CreationClient.__new__(CreationClient);client.store=store;client.root=root
    client.record_sent_observation(item,target,result)
    self.assertEqual(dict(store.db.execute('SELECT status,count(*) FROM inbound_message_work GROUP BY status')),
                     {'pending' if active else 'held':1})
    self.assertEqual(store.db.execute('SELECT count(*) FROM inbound_work').fetchone()[0],0)
    event=next(iter(messages.mapping(messages.head(cid)).values()))
    reread=store.snapshot(cid,target['conversation_title'],'avatar',str(frame),after,
     observation_head=messages.head(cid),on_observation=lambda r,p:messages.record(cid,target,r,p,enabled=active))
    self.assertIn(event,reread['message_event_ids'].values())
    self.assertEqual(store.db.execute('SELECT count(*) FROM inbound_message_work').fetchone()[0],1)
    self.assertEqual(store.db.execute('SELECT status FROM gui_commands WHERE id=?',(item['id'],)).fetchone()[0],'succeeded')
    store.db.close()

 @unittest.skipUnless((Path(__file__).parent / 'post_send_context_fixture.json').is_file(), "私有视觉夹具不随开源版发布")
 def test_actual_after_frame_keeps_full_context_and_concurrent_input_without_reusing_queued_seed(self):
  import json,tempfile,hashlib
  import numpy as np
  from pathlib import Path
  from PIL import Image
  from store import Store,digest
  from inbound_work import InboundWork
  from creation_client import CreationClient
  fixture=json.loads((Path(__file__).parent/'post_send_context_fixture.json').read_text(encoding='utf8'))
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);store=Store(root/'state.sqlite');cid=fixture['conversation_id']
   old=store.snapshot(cid,'开发组(4)','avatar','old.png',fixture['old'])
   target={'name':'开发组','conversation_title':'开发组(4)','avatar_sha':'avatar'}
   row=dict(target,key=digest(['开发组','avatar']),unread=True,unread_count=1)
   work=InboundWork(store);work.capture(row,old,fixture['old'],[19]);old_seed=work.get(row['key'])['seed'];work.update(row['key'],'queued','reply_queued')
   frame=root/'after.png';image=np.full((1040,1920,3),249,dtype=np.uint8);Image.fromarray(image[:,:,::-1]).save(frame)
   sha=hashlib.sha256(image.tobytes()).hexdigest();after=fixture['manual']+[fixture['current'][-1]]
   result={'status':'sent','reason':'MEDIA_NEW_RIGHT_BUBBLE_AND_EMPTY_DRAFT_VERIFIED',
    'evidence':{'title_verified':True,'outgoing_verified':True,'frame_sha256':sha},
    'diagnostic':{'history_before':fixture['old'],'outgoing':fixture['manual'][-1],
     'frames':[{'stage':'after-1','path':str(frame),'sha256':sha}],
     'after_payloads':[{'stage':'after-1','payloads':after}]}}
   item={'id':'a'*32,'conversation_id':cid,'media_key':'command_menu_v1'}
   store.queue_gui({'id':item['id'],'action':'send_delivery','params':item,'expires_at':99999999999});store.gui_result(item['id'],'succeeded',result)
   client=CreationClient.__new__(CreationClient);client.store=store;client.record_sent_observation(item,target,result)
   state=work.get(row['key']);self.assertEqual(state['status'],'captured');self.assertNotEqual(state['seed'],old_seed)
   captures=store.db.execute('SELECT seed,source_ordinal,payload_json FROM inbound_captures ORDER BY rowid').fetchall()
   self.assertEqual([r[1] for r in captures],[19,19]);self.assertEqual(json.loads(captures[-1][2])['visual']['box'],[545,828,63,36])
   values=json.loads(store.db.execute('SELECT payloads_json FROM snapshot_payloads ORDER BY snapshot_id DESC LIMIT 1').fetchone()[0])
   self.assertEqual(len(values),20);self.assertIn('sender_observation_key',values[-1]);self.assertIn('sender_observation_key',values[-2])
   self.assertEqual(values[-2]['sender_identity_status'],'visual_provisional')
   self.assertEqual(store.db.execute("SELECT status FROM gui_commands WHERE id=?",(item['id'],)).fetchone()[0],'succeeded')
   store.db.close()

 def test_changed_after_frame_cannot_become_context_baseline(self):
  import tempfile,hashlib
  import numpy as np
  from pathlib import Path
  from PIL import Image
  from store import Store
  from creation_client import CreationClient
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);store=Store(root/'state.sqlite');client=CreationClient.__new__(CreationClient);client.store=store
   image=np.full((1040,1920,3),249,dtype=np.uint8);sha=hashlib.sha256(image.tobytes()).hexdigest()
   image[600,600]=0;path=root/'after.png';Image.fromarray(image[:,:,::-1]).save(path)
   result={'evidence':{'frame_sha256':sha},'diagnostic':{'frames':[{'stage':'after-1','path':str(path),'sha256':sha}],
      'after_payloads':[{'stage':'after-1','payloads':[]}]}}
   with self.assertRaisesRegex(ValueError,'POST_SEND_FRAME_PIXELS_CHANGED'):
    client.record_sent_observation({'id':'a'*32,'conversation_id':'cid'},{'conversation_title':'group'},result)
   self.assertEqual(store.db.execute('SELECT count(*) FROM snapshots').fetchone()[0],0);store.db.close()

class VideoCandidates(unittest.TestCase):
    def test_colloquial_video(self):
        self.assertTrue(candidate('你帮我做一个 创作平台AI祝福大家 国庆快乐的视频，'))
        self.assertTrue(candidate('帮我用照片制作视频'))
        self.assertFalse(candidate('怎么制作视频'))
        self.assertFalse(candidate('不要帮我制作视频'))
