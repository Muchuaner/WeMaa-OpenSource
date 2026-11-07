"""Maa-only HTTPS reply bridge with a durable, at-most-once desktop outbox.

Slow model generation is off the GUI thread. Only the single desktop owner may
consume a ready response. A lost claim/send result is never permission to resend.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import json
import re
from pathlib import Path
import time
import urllib.request
import urllib.error
from urllib.parse import urlsplit


class ReplyClient:
    def __init__(self,root,store,*,transport=None):
        self.root,self.store=Path(root),store
        config=json.loads((self.root/'private'/'admin-agent.json').read_text(encoding='utf-8')) if transport is None else {}
        self.endpoint=config.get('endpoint','')
        self.token=config.get('token','')
        if transport is None:
            url=urlsplit(self.endpoint)
            from endpoint_config import valid_endpoint
            if not valid_endpoint(self.endpoint, config.get('trusted_host')) or len(self.token)<32:
                raise ValueError('INVALID_MAA_REPLY_CONFIG')
        self.transport=transport or self.api
        self.executor=ThreadPoolExecutor(max_workers=3,thread_name_prefix='maa-reply')
        self.futures={}
        self.ack_futures={}
        self.next_attempt={}
        from reply_serial import ReplySerial
        self.serial = ReplySerial(store.db)
        for item in store.reply_rows(('queued', 'ready', 'send_started')):
            self.serial.remember(item['event_id'], item['conversation_id'])
            if item['status'] != 'queued': self.serial.acquire(item['event_id'])
        # A previous process may have clicked Send before it died. Do not repeat.
        for item in store.reply_rows(('send_started',)):
            result={'status':'uncertain','reason':'PROCESS_RESTART_DURING_SEND','evidence':{}}
            store.reply_update(item['event_id'],'uncertain',result=result)
            self.serial.finish(item['event_id'], 'uncertain')

    def api(self,path,body,method='POST'):
        raw=json.dumps(body,ensure_ascii=False,separators=(',',':')).encode() if body is not None else None
        request=urllib.request.Request(self.endpoint+path,data=raw,method=method,
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=100 if path.endswith('/prepare') else 6) as response:
            reply=json.loads(response.read(131072))
        if not isinstance(reply,dict) or reply.get('code') not in (None,0,200):
            raise ValueError('INVALID_REPLY_API_RESPONSE')
        return reply.get('data',reply)

    def queue(self,body,snapshot,ordinal,target):
        # Persist target locally; it is not an arbitrary remote execution request.
        from group_policy import allowed
        if not allowed(self.root,target,body.get("conversation_id"),body.get("conversation_type")):
            raise ValueError("GROUP_NOT_WHITELISTED")
        from reply_policy import permits,cutoff
        if cutoff(self.store.db,body.get('conversation_id','')) and not permits(self.store.db,body):raise ValueError('AUTO_REPLY_BEFORE_ACTIVATION')
        body=dict(body,_target=target)
        queued = self.store.queue_reply(body,snapshot,ordinal)
        self.serial.remember(body['event_id'], body['conversation_id'])
        return queued

    def pump(self):
        now=time.time()
        # 本地发送与人工核对的终态也要恢复到串行记录；重启不会丢失阻断。
        for event, status in self.store.db.execute('''SELECT s.event_id,o.status
                FROM reply_serial s JOIN reply_outbox o ON o.event_id=s.event_id
                WHERE s.state IN ('active','waiting','blocked') AND o.status IN ('sent','no_reply','failed','uncertain')''').fetchall():
            self.serial.finish(event, status)
        for event,(future,item) in list(self.ack_futures.items()):
            if not future.done(): continue
            del self.ack_futures[event]
            try:
                future.result();self.store.reply_update(event,item['status'],reported=True)
            except urllib.error.HTTPError as exc:
                if exc.code in (400,404,409):
                    self.store.reply_update(event,item['status'],reported=True)
                    self.store.event('reply_ack_terminal',{'event_id':event,'http_status':exc.code})
                else: self.next_attempt[event]=now+15
            except Exception:
                self.next_attempt[event]=now+15
        for event,future in list(self.futures.items()):
            if not future.done(): continue
            del self.futures[event]
            try:
                response=future.result()
                if response.get('event_id')!=event: raise ValueError('REPLY_EVENT_MISMATCH')
                status=response.get('status')
                if status not in ('generating','ready','sending','sent','failed','uncertain','no_reply'):
                    raise ValueError('REPLY_STATUS_INVALID')
                local='queued' if status=='generating' else ('uncertain' if status=='sending' else status)
                # 核心 SENT 来自全部分段的真实微信回执，不能只凭入站已接收放行。
                self.store.reply_update(event,local,response=response,
                                        reported=response.get('serial_reply') is True)
                self.serial.finish(event, local)
                self.next_attempt[event]=now+3
            except urllib.error.HTTPError as exc:
                if exc.code in (400, 401, 403, 404, 422):
                    self.store.reply_update(event, 'uncertain', result={
                        'status': 'uncertain', 'reason': 'SERIAL_API_REQUIRES_REVIEW', 'evidence': {}})
                    self.serial.finish(event, 'uncertain')
                    self.store.event('reply_serial_review', {'event_id': event, 'http_status': exc.code})
                else:
                    self.next_attempt[event] = now + 10
            except Exception as exc:
                # Re-querying prepare uses the identical event id and cannot
                # generate twice. It does not repeat a physical send operation.
                self.store.event('reply_prepare_retry',{'event_id':event,'error_type':type(exc).__name__})
                self.next_attempt[event]=now+10
        for item in self.store.reply_rows(('queued',)):
            event=item['event_id']
            if event in self.futures or self.next_attempt.get(event,0)>now: continue
            self.serial.remember(event, item['conversation_id'])
            if not self.serial.acquire(event): continue
            started, buffered_after = self.serial.details(event)
            if now-started>1800:
                self.store.reply_update(event,'uncertain',result={'status':'uncertain','reason':'GENERATION_TIMEOUT','evidence':{}})
                self.serial.finish(event, 'uncertain')
                continue
            body=json.loads(item['request_json'])
            from reply_policy import permits
            if not permits(self.store.db,body):
                self.store.reply_update(event,'failed',result={'status':'failed','reason':'AUTO_REPLY_BEFORE_ACTIVATION','evidence':{}})
                self.serial.finish(event, 'failed')
                continue
            body.pop('_target',None)
            body.pop('_opening_chat_sha',None)
            opening=body.pop('_opening',False)
            greeting=body.pop('_greeting',False)
            path='/greetings/prepare' if greeting else '/experiment/opening/prepare' if opening else '/experiment/reply/prepare' if 'contact_id' in body else '/reply/prepare'
            response=json.loads(item['response_json'] or '{}')
            if path == '/reply/prepare':
                body['serial_reply'] = True
                if buffered_after: body['buffered_after'] = buffered_after
                if response.get('serial_reply') is True:
                    path, body, method = '/reply/'+event+'/status', None, 'GET'
                else:
                    method = 'POST'
            else:
                method = 'POST'
            self.futures[event]=self.executor.submit(self.transport,path,body,method)
        for item in self.store.reply_rows(('sent','failed','uncertain')):
            event=item['event_id']
            if item['reported'] or not item['result_json'] or self.next_attempt.get(event,0)>now or event in self.ack_futures: continue
            future=self.executor.submit(self.transport,'/reply/'+event+'/result',json.loads(item['result_json']),'PUT')
            self.ack_futures[event]=(future,item)

    def ready(self):
        from private_baseline import receive_only
        from reply_policy import permits
        return [r for r in self.store.reply_rows(('ready',)) if permits(self.store.db,json.loads(r['request_json']))]

    def record_sent_observation(self,item,target,result):
        """Keep our verified outgoing anchor; persist concurrent input first."""
        if result.get('status')!='sent':return
        diagnostic=result.get('diagnostic') or {};observations=diagnostic.get('after_payloads') or []
        if not observations:return
        last=observations[-1];frames=[f for f in diagnostic.get('frames',[]) if f['stage']==last['stage']]
        if len(frames)!=1:return
        from collections import Counter
        from message_vision import message_identity
        before=Counter(message_identity(p) for p in diagnostic.get('history_before',[]) if message_identity(p))
        seen=Counter();new=[]
        for i,p in enumerate(last['payloads']):
            key=message_identity(p)
            if not key:continue
            seen[key]+=1
            if p.get('direction')=='incoming' and seen[key]>before[key]:new.append(i)
        observed_head=None;messages=None
        if self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='inbound_observation_heads'").fetchone():
            from inbound_messages import InboundMessages
            messages=InboundMessages(self.store);observed_head=messages.head(item['conversation_id'])
        options={}
        if observed_head is not None:
            from reply_policy import permits
            now=time.time()
            options={'observation_head':observed_head,'on_observation':lambda r,p:messages.record(
                item['conversation_id'],target,r,p,enabled=permits(self.store.db,
                    {'conversation_id':item['conversation_id'],'observed_at':now},now),now=now)}
        result_snapshot=self.store.snapshot(item['conversation_id'],target['conversation_title'],target['avatar_sha'],frames[0]['path'],last['payloads'],
            activation_baseline=not new and observed_head is None,**options)
        if new and observed_head is None:
            from inbound_work import InboundWork
            from store import digest
            row=dict(target,key=digest([target['name'],target['avatar_sha']]),unread=True,unread_count=len(new))
            InboundWork(self.store).capture(row,result_snapshot,last['payloads'],new)
        self.store.event('outgoing_anchor_saved',{'event_id':item['event_id'],'snapshot_id':result_snapshot['snapshot_id'],'concurrent_incoming':len(new)})

    def send(self,item,runtime,run_id,target):
        from private_baseline import receive_only
        from reply_policy import permits
        if not permits(self.store.db,json.loads(item['request_json'])):
            result={'status':'failed','reason':'PRIVATE_RECEIVE_ONLY','evidence':{}}
            self.store.reply_update(item['event_id'],'failed',result=result)
            return result
        from group_policy import allowed
        request=json.loads(item['request_json'])
        if not allowed(self.root,target,item['conversation_id'],request.get('conversation_type')):
            result={'status':'failed','reason':'GROUP_NOT_WHITELISTED','evidence':{}}
            self.store.reply_update(item['event_id'],'failed',result=result);return result
        event=item['event_id'];response=json.loads(item['response_json'])
        text=response.get('reply_text','')
        if not isinstance(text,str) or not text.strip() or len(text)>2000:
            result={'status':'failed','reason':'REPLY_TEXT_INVALID','evidence':{}}
            self.store.reply_update(event,'failed',result=result);return result
        if response.get('conversation_id')!=item['conversation_id']:
            result={'status':'failed','reason':'REPLY_CONVERSATION_MISMATCH','evidence':{}}
            self.store.reply_update(event,'failed',result=result);return result
        # Commit before the network claim. Any ambiguity from this point must
        # be reconciled manually, never replayed by restarting the process.
        self.store.reply_update(event,'send_started')
        try:
            permit=self.transport('/reply/'+event+'/claim',{'reply_id':response['reply_id']},'POST')
            if permit.get('permit') is not True:
                result={'status':'uncertain','reason':'SEND_CLAIM_NOT_GRANTED','evidence':{}}
            else:
                from send import send_verified
                request=json.loads(item['request_json'])
                def policy_check():
                    if not permits(self.store.db,request):raise ValueError('AUTO_REPLY_POLICY_CHANGED')
                options={'refresh_row_position':True,'preflight':policy_check}
                if 'contact_id' in request:
                    def preflight():
                        policy_check()
                        prefix='/greetings/preflight/' if request.get('_greeting') else '/experiment/preflight/'
                        if self.transport(prefix+event,{},'GET').get('permit') is not True:
                            raise ValueError('EXPERIMENT_REVOKED_BEFORE_SEND')
                    options['preflight']=preflight
                    if request.get('_opening'): options['expected_chat_sha']=request.get('_opening_chat_sha')
                result=send_verified(runtime,self.root,run_id,dict(target,reply_text=text,event_id=event),**options)
        except Exception as exc:
            result={'status':'uncertain','reason':'SEND_OUTCOME_UNKNOWN','evidence':{}}
            self.store.event('reply_send_error',{'event_id':event,'error_type':type(exc).__name__})
        if result.get('status')=='blocked': result=dict(result,status='failed')
        if result.get('status') not in ('sent','failed','uncertain'):
            result={'status':'uncertain','reason':'SENDER_RESULT_INVALID','evidence':{}}
        # Only explicitly verified outgoing evidence can become sent.
        evidence=result.get('evidence') or {}
        if result['status']=='sent' and not (evidence.get('title_verified') is True and evidence.get('outgoing_verified') is True):
            result={'status':'uncertain','reason':'SEND_EVIDENCE_INCOMPLETE','evidence':evidence}
        reason=result.get('reason') or ''
        if not isinstance(reason,str) or not re.fullmatch(r'[A-Za-z0-9_.:=-]{0,160}',reason): reason='SENDER_EXCEPTION'
        receipt={'status':result['status'],'reason':reason,
            'evidence':{k:v for k,v in evidence.items() if k in ('frame_sha256','snapshot_id','title_verified','outgoing_verified')}}
        # Keep rich local diagnostics without exposing unbounded payloads in ACK.
        if result.get('diagnostic'): self.store.event('reply_send_diagnostic',{'event_id':event,'diagnostic':result['diagnostic']})
        self.store.reply_update(event,result['status'],result=receipt)
        self.serial.finish(event, result['status'])
        try:self.record_sent_observation(item,target,result)
        except Exception as exc:self.store.event('outgoing_anchor_review',{'event_id':event,'error_type':type(exc).__name__})
        self.store.event('reply_'+result['status'],{'event_id':event,'conversation':item['conversation_id'],'reason':result.get('reason'),'evidence':result.get('evidence')})
        return result

    def close(self):
        self.executor.shutdown(wait=False,cancel_futures=True)
