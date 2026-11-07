"""Background downloads and durable inbound/receipt journals; no GUI ownership."""
from concurrent.futures import ThreadPoolExecutor
import hashlib,json,re,time,urllib.request,urllib.error
from pathlib import Path
from urllib.parse import urlsplit
from PIL import Image
MAX_MEDIA=100*1024*1024
MAX_REJECTION_BODY=8192
# These errors are raised before the backend creates its event journal. Other
# client errors still require review, but do not prove an effect-free rejection.
PRE_JOURNAL_REJECTIONS=frozenset({
    '只接受最近五分钟的已验证新消息','微信身份凭证无效','未核验微信号',
    '身份未关联消息与资料证据','请求体必须为对象','自动回复请求含未知字段',
    'Maa 机器标识无效','event_id 无效','视觉会话 ID 无效','会话类型无效',
    '发送者或 OCR 尚未验证，不能自动回复','只接受已验证的新入站文字，不回复自己的消息',
    '媒体收件标记或固定内容无效','参考上下文超过限制','参考上下文字段无效',
    '参考上下文不能含系统或工具指令',
    *(name+suffix for name in ('上下文','会话标题','发送者','入站消息')
      for suffix in (' 文本无效',' 包含非法字符')),
})


class CreationEventRejected(Exception):
    def __init__(self,detail):
        self.detail=detail
        super().__init__(detail['reason'])


def terminal_event_http_status(status):
    return 400<=status<500 and status not in (408,425,429)


def event_http_rejection(status,raw=None):
    """Keep bounded response evidence without retaining echoed request/secrets."""
    detail={'reason':'CREATION_EVENT_HTTP_REJECTED_REVIEW','http_status':status,
            'retry':False,'rejected_before_effects':False,'rejection_scope':'this_attempt',
            'response_body_read':raw is not None}
    if raw is None:return detail
    truncated=len(raw)>MAX_REJECTION_BODY;raw=raw[:MAX_REJECTION_BODY]
    detail.update(response_sha256=hashlib.sha256(raw).hexdigest(),
                  response_bytes=len(raw),response_truncated=truncated)
    try:
        value=json.loads(raw) if not truncated else None
    except (ValueError,UnicodeDecodeError,RecursionError):value=None
    error=value.get('detail',value) if isinstance(value,dict) else None
    if not isinstance(error,dict):return detail
    code=error.get('code');message=error.get('message')
    if type(code) is int and 0<=code<=99999:detail['server_code']=code
    if status==400 and type(code) is int and code==40001 and isinstance(message,str) and message in PRE_JOURNAL_REJECTIONS:
        detail.update(reason='CREATION_EVENT_EXPIRED' if message=='只接受最近五分钟的已验证新消息'
                      else 'CREATION_EVENT_VALIDATION_REJECTED',server_message=message,
                      rejected_before_effects=True,rejection_stage='pre_journal_validation')
    return detail


def candidate(value):
    value=value.strip()
    if value.startswith('/'):return True
    if value.startswith(('>','引用：','转发：','“','「','"')) or re.search(r'(?:不要|不用|无需|别|禁止)(?:再|帮我|给我|立即|直接)?\s*(?:生成|生图|绘制|画|制作|修改|调整|改)',value):return False
    if re.search(r'[？?]|(?:能否|可以吗|怎么|如何|是否|为什么|多少钱|多少积分|支持吗|讨论|例如)',value):return False
    if re.match(r'^(?:你)?(?:帮我|请|给我)?\s*(?:生成|制作|做|生视频|用[^。！？]{1,50}(?:制作|做|生成))[^。！？]{0,160}视频',value):return True
    return bool(re.match(r'^(?:帮我|请|给我)?\s*(?:生成|制作|做|绘制|生图|生视频|画)[^。！？]{0,80}(?:图片|图像|海报|插画|照片|头像|四视图|猫|图|视频)',value) or (re.match(r'^(?:请|帮我)?(?:把|将|修改|调整).*(?:上一张|这张|图片|背景|亮|颜色|画风|图)',value) and re.search(r'改|调整|修改',value)))


class CreationClient:
    def __init__(self,root,store):
        self.root=Path(root);self.store=store;config=json.loads((self.root/'private'/'admin-agent.json').read_text(encoding='utf-8'))
        self.endpoint=config['endpoint'];self.token=config['token']
        u=urlsplit(self.endpoint)
        from endpoint_config import valid_endpoint
        if not valid_endpoint(self.endpoint, config.get('trusted_host')):raise ValueError('INVALID_CREATION_ENDPOINT')
        self.executor=ThreadPoolExecutor(max_workers=3,thread_name_prefix='maa-creation');self.futures={};self.next_poll=0;self.next_event={};self.delivery_wait_until=0
        store.db.execute('CREATE TABLE IF NOT EXISTS creation_events(id TEXT PRIMARY KEY,body TEXT,status TEXT,updated REAL)')
        store.db.execute('CREATE TABLE IF NOT EXISTS creation_event_reviews(id TEXT PRIMARY KEY,detail_json TEXT,created REAL,updated REAL)')
        store.db.execute('CREATE TABLE IF NOT EXISTS media_receipts(id TEXT PRIMARY KEY,body TEXT,reported INTEGER DEFAULT 0)');store.db.commit()
        from creation_turns import CreationTurns
        self.turns=CreationTurns(store)
        for cid,status,detail in store.db.execute("SELECT id,status,detail_json FROM gui_commands WHERE action='send_delivery' AND status='failed'").fetchall():
            value=json.loads(detail)
            if value.get('status')=='uncertain':self.receipt(cid,value)

    def api(self,path,body=None,method='GET'):
        data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None
        req=urllib.request.Request(self.endpoint+path,data=data,method=method,headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=110 if path.endswith('/events') else 15) as resp:
                raw=resp.read(256*1024)
        except urllib.error.HTTPError as exc:
            if path=='/creation/events' and method=='POST' and terminal_event_http_status(exc.code):
                # Read the error on this network thread, never on the GUI owner.
                raw=None
                try:raw=exc.read(MAX_REJECTION_BODY+1)
                except Exception:pass
                finally:exc.close()
                raise CreationEventRejected(event_http_rejection(exc.code,raw)) from None
            raise
        value=json.loads(raw)
        if value.get('code') not in (None,0,200):raise ValueError('CREATION_API_REJECTED')
        return value.get('data',value)

    def queue_event(self,body):
        if self.store.db.execute('SELECT 1 FROM creation_events WHERE id=?',(body['event_id'],)).fetchone():return
        if getattr(self,'turns',None):
            self.turns.remember(body)
            return
        self.store.db.execute('INSERT OR IGNORE INTO creation_events VALUES(?,?,?,?)',(body['event_id'],json.dumps(body,ensure_ascii=False),'pending',time.time()));self.store.db.commit()

    def review_event(self,eid,detail):
        """Retain the original request and stop resubmitting a rejected event."""
        now=time.time()
        if getattr(self,'turns',None) and self.turns.item(eid):self.turns.block(eid,detail['reason'])
        self.store.db.execute('CREATE TABLE IF NOT EXISTS creation_event_reviews(id TEXT PRIMARY KEY,detail_json TEXT,created REAL,updated REAL)')
        changed=self.store.db.execute("UPDATE creation_events SET status='review',updated=? WHERE id=? AND status='pending'",(now,eid)).rowcount
        if changed:
            value={**detail,'id':eid,'event_id':eid,'status':'review'}
            self.store.db.execute('INSERT OR IGNORE INTO creation_event_reviews VALUES(?,?,?,?)',
                                  (eid,json.dumps(value,ensure_ascii=False),now,now))
        self.store.db.commit()
        if changed:self.store.event('creation_event_review',value)
        return bool(changed)

    def download(self,item):
        media_key=item.get('media_key')
        if media_key and (media_key not in ('command_menu_v1','asset_catalog_v1') or item.get('asset_id') or item.get('type')!='image'):raise ValueError('SYSTEM_MEDIA_KEY_INVALID')
        if not item.get('asset_id') and not media_key:return item
        did=item['id']
        if not re.fullmatch('[a-f0-9]{32}',did):raise ValueError('MEDIA_DELIVERY_ID_INVALID')
        req=urllib.request.Request(self.endpoint+'/creation/deliveries/'+did+'/media',headers={'Authorization':'Bearer '+self.token})
        folder=self.root/'data'/'media-cache'/did;folder.mkdir(parents=True,exist_ok=True)
        pending=folder/(did+'.part')
        try:
            with urllib.request.urlopen(req,timeout=60) as resp, pending.open('wb') as out:
                size=int(resp.headers.get('Content-Length','0'));sha=resp.headers.get('X-Content-SHA256','');mime=resp.headers.get('Content-Type','').split(';')[0]
                if not 0<size<=MAX_MEDIA or not re.fullmatch('[a-f0-9]{64}',sha):raise ValueError('MEDIA_HEADERS_INVALID')
                h=hashlib.sha256();count=0
                while True:
                    raw=resp.read(128*1024)
                    if not raw:break
                    count+=len(raw)
                    if count>size:raise ValueError('MEDIA_LENGTH_INVALID')
                    h.update(raw);out.write(raw)
                if count!=size or h.hexdigest()!=sha:raise ValueError('MEDIA_DIGEST_INVALID')
            if item['type']=='image':
                with Image.open(pending) as im:
                    actual=Image.MIME.get(im.format)
                    if actual!=mime or im.width*im.height>40_000_000:raise ValueError('MEDIA_IMAGE_INVALID')
                    im.verify()
                with Image.open(pending) as decoded:decoded.load()
            else:
                with pending.open('rb') as f:
                    if mime!='video/mp4' or f.read(12)[4:8]!=b'ftyp':raise ValueError('MEDIA_VIDEO_INVALID')
            ext={'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp','video/mp4':'.mp4'}.get(mime)
            if not ext:raise ValueError('MEDIA_MIME_UNSUPPORTED')
            if media_key and mime!='image/png':raise ValueError('SYSTEM_MEDIA_MIME_INVALID')
            basename=('catalog' if media_key=='asset_catalog_v1' else 'menu') if media_key else 'v'+did[:8] if mime=='video/mp4' else str(int(item['asset_id']))
            path=folder/(basename+ext);pending.replace(path)
            return {**item,'path':str(path),'sha256':sha,'mime':mime,'size':size}
        finally:pending.unlink(missing_ok=True)

    def receipt(self,did,value):
        receipt={'status':value['status'],'reason':str(value.get('reason',''))[:160],'evidence':{k:v for k,v in value.get('evidence',{}).items() if k in ('title_verified','outgoing_verified','media_sha256','frame_sha256')}}
        self.store.db.execute('INSERT OR IGNORE INTO media_receipts(id,body) VALUES(?,?)',(did,json.dumps(receipt,ensure_ascii=False)));self.store.db.commit()

    def pending_deliveries(self):
        return self.store.db.execute("SELECT count(*) FROM gui_commands WHERE action='send_delivery' AND status='pending'").fetchone()[0]

    def delivery_ready(self):
        if not getattr(self,'turns',None):return bool(self.pending_deliveries())
        return any(self.turns.permits_delivery(did,json.loads(raw)['conversation_id']) for did,raw in
                   self.store.db.execute("SELECT id,params_json FROM gui_commands WHERE action='send_delivery' AND status='pending'"))

    def delivery_health(self):
        row=self.store.db.execute("SELECT id,status,detail_json,updated FROM gui_commands WHERE action='send_delivery' ORDER BY updated DESC LIMIT 1").fetchone()
        oldest=self.store.db.execute("SELECT min(updated) FROM gui_commands WHERE action='send_delivery' AND status='pending'").fetchone()[0]
        return {'pending':self.pending_deliveries(),'oldest_pending_seconds':max(0,time.time()-oldest) if oldest is not None else 0,
                'last_result':{'id':row[0],'status':row[1],'reason':json.loads(row[2] or '{}').get('reason'),'updated':row[3]} if row else None}

    def delivery_in_flight(self):
        return (time.time()<getattr(self,'delivery_wait_until',0) or
                any(kind in ('event','download') for _,kind,_ in self.futures.values()) or
                bool(getattr(self,'turns',None) and self.turns.live()) or
                bool(not getattr(self,'turns',None) and self.store.db.execute("SELECT 1 FROM creation_events WHERE status='pending' LIMIT 1").fetchone()))

    def pump(self):
        now=time.time()
        for key,(future,kind,value) in list(self.futures.items()):
            if not future.done():continue
            del self.futures[key]
            try:
                result=future.result()
                if kind=='event':
                    state=self.turns.accepted(key,result) if getattr(self,'turns',None) else 'no_reply'
                    changed=self.store.db.execute("UPDATE creation_events SET status=?,body='{}',updated=? WHERE id=? AND status='pending'",('dispatched' if state=='awaiting_delivery' else 'done',now,key)).rowcount
                    if changed:
                        self.next_poll=0;self.delivery_wait_until=now+15
                        self.store.event('creation_event_accepted',{'id':key})
                elif kind=='turn_status':
                    state=self.turns.observe(value,result)
                    if state in ('sent','blocked'):
                        self.store.db.execute('UPDATE creation_events SET status=?,updated=? WHERE id=?',
                                             ('done' if state=='sent' else 'review',now,value))
                elif kind=='poll':
                    for item in result.get('items',[]):
                        did=item['id']
                        if self.store.db.execute('SELECT 1 FROM gui_commands WHERE id=?',(did,)).fetchone() or did in self.futures or now<self.next_event.get(did,0):continue
                        self.futures[did]=(self.executor.submit(self.download,item),'download',item)
                elif kind=='download':
                    self.store.queue_gui({'id':result['id'],'action':'send_delivery','params':result,'expires_at':now+(86400 if result.get('binding_contact') else 300)})
                    self.delivery_wait_until=0
                    self.store.event('creation_delivery_ready',{'id':result['id'],'cloud_created_at':result.get('created_at')})
                elif kind=='receipt':self.store.db.execute('UPDATE media_receipts SET reported=1 WHERE id=?',(value,))
                self.store.db.commit()
            except Exception as exc:
                rejection=exc.detail if isinstance(exc,CreationEventRejected) else (
                    event_http_rejection(exc.code) if isinstance(exc,urllib.error.HTTPError)
                    and terminal_event_http_status(exc.code) else None)
                if kind=='event' and rejection is not None:
                    self.review_event(key,rejection)
                    self.next_event.pop(key,None)
                    continue
                self.next_event[key]=now+15
                self.store.event('creation_transport_retry',{'id':key,'kind':kind,'error_type':type(exc).__name__})
        event_inflight=sum(kind=='event' for _,kind,_ in self.futures.values())
        for eid,body in self.store.db.execute("SELECT id,body FROM creation_events WHERE status='pending' ORDER BY updated,id LIMIT 1000").fetchall():
            if event_inflight>=3:break
            if eid not in self.futures and now>=self.next_event.get(eid,0):
                if getattr(self,'turns',None):
                    if not self.turns.item(eid):
                        self.turns.remember(json.loads(body))
                        self.turns.block(eid,'CREATION_LEGACY_TURN_REQUIRES_REVIEW')
                    if not self.turns.begin(eid):
                        if self.turns.item(eid)['state']=='blocked':
                            self.store.db.execute("UPDATE creation_events SET status='review',updated=? WHERE id=?",(now,eid));self.store.db.commit()
                        continue
                wire=json.loads(body)
                if getattr(self,'turns',None):
                    wire['serial_reply']=True
                    parent=self.turns.serial.details(eid)[1]
                    if parent:wire['buffered_after']=parent
                self.futures[eid]=(self.executor.submit(self.api,'/creation/events',wire,'POST'),'event',None)
                event_inflight+=1
        if getattr(self,'turns',None):
            for eid,cid in self.turns.awaiting():
                key='turn:'+eid
                if key not in self.futures and now>=self.next_event.get(key,0):
                    self.next_event[key]=now+3
                    self.futures[key]=(self.executor.submit(self.api,'/creation/events/'+eid+'/status?conversation_id='+cid),
                                       'turn_status',eid)
        for did,body in self.store.db.execute('SELECT id,body FROM media_receipts WHERE reported=0 LIMIT 3').fetchall():
            key='ack:'+did
            if key not in self.futures and now>=self.next_event.get(key,0):self.futures[key]=(self.executor.submit(self.api,'/creation/deliveries/'+did+'/result',json.loads(body),'PUT'),'receipt',did)
        if now>=self.next_poll and 'poll' not in self.futures:
            self.next_poll=now+5;self.futures['poll']=(self.executor.submit(self.api,'/creation/deliveries'),'poll',None)

    def process_one(self,runtime,run_id):
        from private_baseline import receive_only
        from operator_media import permitted
        for did,raw in self.store.db.execute("SELECT id,params_json FROM gui_commands WHERE action='send_delivery' AND status='pending' ORDER BY updated").fetchall():
            item=json.loads(raw)
            if receive_only(self.store.db,item.get('conversation_id','')) and not permitted(self.store.db,item):
                result={'status':'failed','reason':'PRIVATE_RECEIVE_ONLY','evidence':{}}
                self.store.gui_result(did,'failed',result);self.receipt(did,result)
                self.store.event('creation_delivery_failed',{'id':did,'conversation_id':item.get('conversation_id'),'reason':result['reason']})
                return True
        from binding_contacts import process_binding_contact
        if process_binding_contact(self,runtime,run_id):return True
        rows=self.store.db.execute("SELECT id,params_json,expires FROM gui_commands WHERE action='send_delivery' AND status='pending' ORDER BY updated").fetchall()
        row=next((r for r in rows if time.time()>=self.next_event.get('claim:'+r[0],0) and
                  (not getattr(self,'turns',None) or self.turns.permits_delivery(r[0],json.loads(r[1])['conversation_id']))),None)
        if not row:return False
        if json.loads(row[1]).get('binding_contact'):return False
        did,raw,expires=row;item=json.loads(raw);result={'status':'failed','reason':'DELIVERY_NOT_STARTED','evidence':{}}
        try:
            if time.time()>expires:raise ValueError('DELIVERY_LOCAL_EXPIRED')
            from manual_gui import find_recent,observe
            saved=self.store.db.execute('SELECT target_json FROM conversation_routes WHERE conversation_id=?',(item['conversation_id'],)).fetchone()
            if not saved:raise ValueError('DELIVERY_ROUTE_NOT_OBSERVED')
            from group_policy import allowed
            if not allowed(self.root,json.loads(saved[0]),item['conversation_id']):raise ValueError('GROUP_NOT_WHITELISTED')
            selected,page=find_recent(runtime,self.root,json.loads(saved[0]),run_id+'-delivery-route',expires)
            cached=self.store.db.execute('SELECT payloads_json FROM manual_views WHERE conversation_id=?',(item['conversation_id'],)).fetchone()
            cache_options={'previous_payloads':json.loads(cached[0])} if cached else {}
            cid,target=observe(runtime,self.root,self.store,selected,run_id+'-delivery-open',page,**cache_options)
            if cid!=self.store.canonical(item['conversation_id']):raise ValueError('DELIVERY_TARGET_CHANGED')
            self.store.gui_result(did,'executing',{'status':'executing'})
            from delivery_claims import DeliveryClaims
            claims=DeliveryClaims(self.store)
            token=claims.token(did)
            try:
                permit=self.api('/creation/deliveries/'+did+'/claim',{'claim_token':token},'POST')
            except (OSError,urllib.error.URLError) as exc:
                if isinstance(exc,urllib.error.HTTPError):raise
                # Durable token exists before HTTP; no sender has been entered.
                self.store.gui_result(did,'pending',{'status':'pending','reason':'DELIVERY_CLAIM_RESPONSE_LOST','send_click_attempted':False})
                self.next_event['claim:'+did]=time.time()+15
                self.store.event('creation_claim_deferred',{'id':did,'error_type':type(exc).__name__,'gui_started':False})
                return True
            if permit.get('permit') is True:claims.begin_gui(did)
            if permit.get('permit') is not True:result={'status':'uncertain','reason':'DELIVERY_CLAIM_NOT_GRANTED','evidence':{}}
            elif item.get('asset_id') or item.get('media_key'):
                from media_send import send_media_verified
                def before_click(value):
                    if receive_only(self.store.db,item['conversation_id']) and not permitted(self.store.db,item):
                        raise ValueError('PRIVATE_SINGLE_MEDIA_AUTHORIZATION_EXPIRED')
                    if self.api('/creation/deliveries/'+did+'/preflight').get('permit') is not True:raise ValueError('DELIVERY_REVOKED_BEFORE_SEND')
                    self.store.gui_result(did,'executing',value)
                result=send_media_verified(runtime,self.root,run_id+'-'+did[:12],target,item,persist=before_click,refresh_row_position=True)
            else:
                from send import send_verified
                cached=self.store.db.execute('SELECT payloads_json FROM manual_views WHERE conversation_id=?',(item['conversation_id'],)).fetchone()
                cache_options={'previous_payloads':json.loads(cached[0])} if cached else {}
                result=send_verified(runtime,self.root,run_id,dict(target,reply_text=item['text'],event_id=did),refresh_row_position=True,**cache_options)
        except Exception as exc:
            started=self.store.db.execute('SELECT status FROM gui_commands WHERE id=?',(did,)).fetchone()[0]=='executing'
            result={'status':'uncertain' if started else 'failed','reason':str(exc)[:160],'evidence':{}}
        if result['status']=='blocked':result['status']='failed'
        if result['status']=='sent' and not all(result.get('evidence',{}).get(k) is True for k in ('title_verified','outgoing_verified')):result['status']='uncertain'
        self.store.gui_result(did,'succeeded' if result['status']=='sent' else 'failed',result);self.receipt(did,result)
        if result['status']=='sent' and (item.get('asset_id') or item.get('media_key')):
            try:self.record_sent_observation(item,target,result)
            except Exception as exc:self.store.event('outgoing_anchor_review',{'event_id':did,'reason':str(exc)[:160]})
        self.store.event('creation_delivery_'+result['status'],{'id':did,'conversation_id':item['conversation_id'],'asset_id':item.get('asset_id'),'reason':result.get('reason')})
        return True

    def record_sent_observation(self,item,target,result):
        """Persist the genuine post-send frame; concurrent input stays pending."""
        diagnostic=result.get('diagnostic',{});observations=diagnostic.get('after_payloads') or []
        if not observations:raise ValueError('POST_SEND_PAYLOADS_MISSING')
        last=observations[-1];frames=[f for f in diagnostic.get('frames',[]) if f.get('stage')==last['stage']]
        if len(frames)!=1 or frames[0].get('sha256')!=result.get('evidence',{}).get('frame_sha256'):
            raise ValueError('POST_SEND_FRAME_NOT_BOUND')
        payloads=last['payloads']
        from sender_directory import SenderDirectory
        directory=SenderDirectory(self.store)
        import numpy as np
        with Image.open(frames[0]['path']) as source:image=np.asarray(source.convert('RGB'))[:,:,::-1].copy()
        if image.shape!=(1040,1920,3) or hashlib.sha256(image.tobytes()).hexdigest()!=frames[0]['sha256']:
            raise ValueError('POST_SEND_FRAME_PIXELS_CHANGED')
        frame_sha=hashlib.sha256(image[82:887,490:1885,:3].copy().tobytes()).hexdigest()
        payloads=directory.enrich(item['conversation_id'],payloads,target['conversation_title'],frame_sha)
        observed_head=None;options={}
        if self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='inbound_observation_heads'").fetchone():
            from inbound_messages import InboundMessages
            messages=InboundMessages(self.store);observed_head=messages.head(item['conversation_id'])
            if observed_head is not None:
                from reply_policy import permits
                now=time.time()
                try:active=json.loads((self.root/'data/reply-runtime.json').read_text(encoding='utf-8')).get('auto_reply_enabled') is True
                except (AttributeError,OSError,ValueError):active=False
                options={'observation_head':observed_head,'on_observation':lambda r,p:messages.record(
                    item['conversation_id'],target,r,p,enabled=active and permits(self.store.db,
                        {'conversation_id':item['conversation_id'],'observed_at':now},now),now=now)}
        snapshot=self.store.record_verified_send_context(item['conversation_id'],target['conversation_title'],
            target['avatar_sha'],frames[0]['path'],payloads,result,
            delivery_id=item['id'],
            occurrence_enricher=lambda r,p:directory.transfer_occurrences(item['conversation_id'],p,r,target['conversation_title']),**options)
        if snapshot['incoming_ordinals'] and observed_head is None:
            from inbound_work import InboundWork
            from store import digest
            row=dict(target,key=digest([target['name'],target['avatar_sha']]),unread=True,unread_count=len(snapshot['incoming_ordinals']))
            work=InboundWork(self.store)
            if snapshot['status']=='append':
                if not work.capture(row,snapshot,payloads,snapshot['incoming_ordinals']):
                    raise ValueError('POST_SEND_CONCURRENT_CAPTURE_REVIEW')
            else:work.retry_unstable_append(row,snapshot)
        self.store.event('outgoing_anchor_saved',{'event_id':item['id'],'snapshot_id':snapshot['snapshot_id'],
                         'concurrent_incoming':len(snapshot['incoming_ordinals'])})

    def close(self):self.executor.shutdown(wait=False,cancel_futures=True)
