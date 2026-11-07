"""Low-priority experiment work inside the sole Maa GUI owner.

Control polling is asynchronous. Contact navigation may be interrupted before
any send; claimed/uncertain replies are never replayed.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time


def verified_occurrences(current, original):
    from message_vision import message_identity
    token=message_identity(original)
    if not token or not original.get('reply_safe'):return []
    # The original stable text remains bound to EXACT unchanged glyph pixels.
    # A later OCR punctuation variant must not rewrite that verified request.
    return [p for p in current if message_identity(p)==token and p.get('reply_safe')]


class ExperimentClient:
    def __init__(self, root, store, replies):
        self.root, self.store, self.replies = Path(root), store, replies
        self.path = self.root/'data'/'experiment-control.json'
        try: self.control = json.loads(self.path.read_text(encoding='utf-8'))
        except (FileNotFoundError, ValueError): self.control = {'enabled': False, 'items': []}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='maa-experiment-control')
        self.future = None; self.next_poll = 0; self.retry_after = {}; self.last_poll = 0
        self.store.db.execute('''CREATE TABLE IF NOT EXISTS experiment_attempts
            (event_id TEXT PRIMARY KEY,contact_id TEXT,status TEXT,seen REAL)''')
        self.store.db.commit()
        self.store.db.execute('''CREATE TABLE IF NOT EXISTS experiment_inbound_identity
            (event_id TEXT PRIMARY KEY,body_json TEXT,snapshot_id INTEGER,ordinal INTEGER,
             target_json TEXT,status TEXT,attempts INTEGER,next_at REAL,reason TEXT)''')
        self.store.db.commit()
        self.store.db.execute('''CREATE TABLE IF NOT EXISTS experiment_navigation
            (event_id TEXT PRIMARY KEY,attempts INTEGER,last_at REAL,next_at REAL,
             reason TEXT,reported INTEGER DEFAULT 0)''')
        self.store.db.commit()

    def poll(self, reports):
        acknowledged = []
        for event, attempts, reason in reports:
            try:
                result = self.replies.transport('/experiment/navigation',
                    {'event_id': event, 'attempts': attempts, 'reason': reason}, 'POST')
                if result.get('recorded'): acknowledged.append(event)
            except Exception:
                pass  # Keep the durable report; fresh control still takes priority.
        value = self.replies.transport('/experiment/plan', {}, 'GET')
        return dict(value, _navigation_acks=acknowledged)

    def choose_opening(self, now):
        eligible = []
        for item in self.control['items']:
            if not (item.get('status') == 'pending' and item['scheduled_at'] <= now < item['expires_at']): continue
            # Exclude claimed local work BEFORE choosing a target. A stale
            # server pending flag must not repeatedly mask every other item.
            if self.store.db.execute('SELECT 1 FROM experiment_attempts WHERE event_id=?', (item['id'],)).fetchone(): continue
            retry = self.store.db.execute('SELECT attempts,last_at,next_at FROM experiment_navigation WHERE event_id=?', (item['id'],)).fetchone()
            if retry and (retry[0] >= 3 or now < retry[2]): continue
            eligible.append((retry[1] if retry else 0, item['scheduled_at'], item['id'], item))
        return min(eligible, key=lambda row: row[:3])[-1] if eligible else None

    def navigation_failed(self, event, reason, now):
        # Preemption yields to incoming; it is not a failed contact identity.
        preempted = reason == 'CONTACT_NAVIGATION_PREEMPTED'
        code = reason.split(':', 1)[0]
        import re
        if not re.fullmatch(r'[A-Za-z0-9_.:=-]{1,160}', code): code = 'NAVIGATION_VERIFICATION_FAILED'
        self.store.db.execute('''INSERT INTO experiment_navigation VALUES(?,?,?,?,?,0)
            ON CONFLICT(event_id) DO UPDATE SET attempts=attempts+excluded.attempts,
            last_at=excluded.last_at,next_at=excluded.next_at,reason=excluded.reason,reported=0''',
            (event, 0 if preempted else 1, now, now + (30 if preempted else 120), code))
        self.store.db.commit()

    @property
    def enabled(self): return self.control.get('enabled') is True

    def applies_to(self, body):
        return body['conversation_type']=='private' and (self.enabled or
            (self.control.get('testing_contact') and body['sender_name']==self.control.get('testing_name')))

    def pump(self):
        now = time.time()
        if self.future and self.future.done():
            future = self.future; self.future = None
            try:
                value = future.result()
                for event in value.pop('_navigation_acks', []):
                    self.store.db.execute('UPDATE experiment_navigation SET reported=1 WHERE event_id=?', (event,))
                self.store.db.commit()
                if type(value.get('enabled')) is not bool or not isinstance(value.get('items'), list):
                    raise ValueError('EXPERIMENT_CONTROL_INVALID')
                self.control = value; self.last_poll = now
                from watch import atomic
                atomic(self.path, value)
            except Exception as exc:
                self.store.event('experiment_poll_failed', {'error_type': type(exc).__name__})
        if self.replies and not self.future and now >= self.next_poll:
            self.next_poll = now+5
            reports = self.store.db.execute('SELECT event_id,attempts,reason FROM experiment_navigation WHERE attempts>=3 AND reported=0 LIMIT 4').fetchall()
            self.future = self.executor.submit(self.poll, reports)

    def queue_incoming(self, body, snapshot, ordinal, target, runtime, image, payload):
        if body['conversation_type'] != 'private': return False
        self.store.db.execute('INSERT OR IGNORE INTO experiment_inbound_identity VALUES(?,?,?,?,?,?,0,0,NULL)',
            (body['event_id'],json.dumps(body,ensure_ascii=False),snapshot,ordinal,json.dumps(target,ensure_ascii=False),'pending'))
        self.store.db.commit()
        status=self.store.db.execute('SELECT status FROM experiment_inbound_identity WHERE event_id=?',(body['event_id'],)).fetchone()[0]
        if status!='pending': return False
        try:
            proof = self.verify_private_contact(body,target,runtime,image,payload)
            cid = 'wx:'+proof['wechat_id']
            contact = self.store.db.execute('SELECT facts_json FROM contacts WHERE id=?', (cid,)).fetchone()
            if not contact or json.loads(contact[0]).get('scope') != 'personal':
                raise ValueError('EXPERIMENT_CONTACT_NOT_PERSONAL')
            self.store.db.execute('INSERT INTO contact_chat_links VALUES(?,?,?) ON CONFLICT(contact_id) DO UPDATE SET conversation_id=excluded.conversation_id,seen=excluded.seen', (cid,body['conversation_id'],time.time()))
            self.store.db.commit()
            queued=self.replies.queue(dict(body,observed_at=time.time(),contact_id=cid,identity=proof), snapshot, ordinal, target)
            self.store.db.execute("UPDATE experiment_inbound_identity SET status='queued' WHERE event_id=?",(body['event_id'],));self.store.db.commit()
            return queued
        except Exception as exc:
            # Do not fall back to a customer persona after an identity failure.
            self.store.event('experiment_identity_review', {'event_id': body['event_id'], 'reason': str(exc)[:160]})
            self.store.db.execute("UPDATE experiment_inbound_identity SET attempts=attempts+1,status=CASE WHEN attempts>=2 THEN 'review' ELSE 'pending' END,next_at=?,reason=? WHERE event_id=?",
                (time.time()+30,str(exc)[:160],body['event_id']));self.store.db.commit()
            return False

    def verify_private_contact(self,body,target,runtime,image,payload):
        """Profile navigation is a hint; profile ID AND returned chat must agree."""
        import hashlib
        import numpy as np
        from send import _target
        from message_vision import message_identity
        from manual_gui import open_contact
        if not _target(runtime,image,target,enforce_row_y=False)['verified']:raise ValueError('EXPERIMENT_INBOUND_TARGET_CHANGED')
        if not payload.get('reply_safe') or not message_identity(payload):raise ValueError('EXPERIMENT_INBOUND_NOT_VERIFIED')
        links=self.store.db.execute('SELECT contact_id FROM contact_chat_links WHERE conversation_id=?',(body['conversation_id'],)).fetchall()
        if not links:
            # Nickname is used only to locate a candidate. Opening its actual
            # profile and comparing the returned chat blocks a wrong namesake.
            links=self.store.db.execute('SELECT DISTINCT contact_id FROM contact_positions WHERE list_name=? AND contact_id LIKE ?',(target['name'],'wx:%')).fetchall()
        if len(links)!=1:raise ValueError('EXPERIMENT_CONTACT_ROUTE_AMBIGUOUS')
        expected=links[0][0];known=self.store.db.execute('SELECT facts_json FROM contacts WHERE id=?',(expected,)).fetchone()
        if not known or json.loads(known[0]).get('scope')!='personal':raise ValueError('EXPERIMENT_CONTACT_NOT_PERSONAL')
        before,_=runtime.capture(body['event_id'][:16]+'-inbound-before-profile')
        x,y,w,h=payload['visual']['box']
        if not np.array_equal(before[y:y+h,x:x+w],image[y:y+h,x:x+w]):raise ValueError('EXPERIMENT_MESSAGE_MOVED')
        proof={};actual,opened=open_contact(runtime,self.root,self.store,expected,body['event_id'][:16]+'-inbound-contact',time.time()+120,proof=proof)
        if actual!=body['conversation_id'] or proof.get('wechat_id')!=expected.removeprefix('wx:'):raise ValueError('EXPERIMENT_CONTACT_CHAT_MISMATCH')
        after,_=runtime.capture(body['event_id'][:16]+'-inbound-after-profile')
        if not _target(runtime,after,target,enforce_row_y=False)['verified'] or not np.array_equal(after[y:y+h,x:x+w],before[y:y+h,x:x+w]):raise ValueError('EXPERIMENT_MESSAGE_MOVED_DURING_PROFILE')
        proof.update(message_frame_sha256=hashlib.sha256(before.tobytes()).hexdigest(),message_identity=hashlib.sha256(str(message_identity(payload)).encode()).hexdigest())
        return proof

    def process_incoming_one(self,runtime,run_id):
        """Retry identity checks, never a physical send or a historical replay."""
        if not self.replies or time.time()-self.last_poll>30:return False
        row=self.store.db.execute("SELECT event_id,body_json,snapshot_id,ordinal,target_json FROM experiment_inbound_identity WHERE status='pending' AND next_at<=? ORDER BY snapshot_id LIMIT 1",(time.time(),)).fetchone()
        if not row:return False
        event,raw,snapshot,ordinal,route=row;body=json.loads(raw);target=json.loads(route)
        if not self.applies_to(body):return False
        try:
            epoch=json.loads((self.root/'data/reply-runtime.json').read_text(encoding='utf-8'))['enabled_at']
            if body['observed_at']<epoch or time.time()-body['observed_at']>3600:raise ValueError('EXPERIMENT_IDENTITY_REVIEW_EXPIRED')
            from manual_gui import find_recent
            from watch import consensus
            from message_vision import message_identity,extract_message_payloads
            find_recent(runtime,self.root,target,run_id+'-identity-retry',time.time()+90)
            image,_=runtime.capture(run_id+'-identity-retry-message')
            old=json.loads(self.store.db.execute('SELECT payloads_json FROM snapshot_payloads WHERE snapshot_id=?',(snapshot,)).fetchone()[0])[ordinal]
            from text_copy import read_messages
            current,_=read_messages(runtime,image)
            matches=verified_occurrences(current,old)
            self.store.event('experiment_identity_retry_evidence',{'event_id':event,
                'exact_pixel_occurrences':sum(message_identity(p)==message_identity(old) for p in current),
                'stable_occurrences':len(matches),'ocr_passes':len(passes)+1})
            if len(matches)!=1:raise ValueError('EXPERIMENT_RETRY_MESSAGE_NOT_UNIQUE')
            self.queue_incoming(body,snapshot,ordinal,target,runtime,image,matches[0])
        except Exception as exc:
            self.store.db.execute("UPDATE experiment_inbound_identity SET status='review',reason=? WHERE event_id=?",(str(exc)[:160],event));self.store.db.commit()
            self.store.event('experiment_identity_review',{'event_id':event,'reason':str(exc)[:160]})
        return True

    def process_one(self, runtime, run_id, *, urgent):
        if (urgent or not self.enabled or not self.replies or time.time()-self.last_poll > 30
                or self.store.reply_rows(('queued','ready','send_started'))
                or self.store.db.execute("SELECT 1 FROM experiment_inbound_identity WHERE status='pending' LIMIT 1").fetchone()): return False
        now = time.time()
        item = self.choose_opening(now)
        if not item: return False
        event = item['id']
        def preempt():
            # Each contacts page yields a real recent-list badge check. If the
            # deadline is due, leave the low-priority navigation and let watch
            # establish a reliable incoming delta before attempting an opening.
            runtime.node('OpenRecent')
            from watch import read_rows
            from flows import scroll_top
            scroll_top(runtime, run_id+'-experiment-yield')
            rows, _, _ = read_rows(runtime,self.root,run_id+'-experiment-badges')
            return any(r['unread'] for r in rows)
        try:
            from manual_gui import open_contact
            proof = {}
            cid, target = open_contact(runtime,self.root,self.store,item['contact_id'],run_id+'-experiment-open',
                                       min(item['expires_at'],now+120),proof=proof,interrupt=preempt)
            from watch import read_rows, read_title, consensus, incoming_requests
            from message_vision import extract_message_payloads
            rows, image, path = read_rows(runtime,self.root,run_id+'-experiment-current')
            selected = [r for r in rows if r['name'] == target['name'] and r['avatar_sha'] == target['avatar_sha']]
            if len(selected) != 1: raise ValueError('EXPERIMENT_TARGET_CHANGED')
            row = selected[0]; title = read_title(runtime,image,row)
            from text_copy import read_messages
            payloads,_=read_messages(runtime,image,group_chat=False)
            result=self.store.snapshot(cid,title,row['avatar_sha'],path,payloads)
            incoming=incoming_requests(payloads,result,row,title,cid,time.time(),0)
            if incoming:
                for ordinal,body in incoming:
                    self.queue_incoming(body,result['snapshot_id'],ordinal,target,runtime,image,payloads[ordinal])
                self.store.db.execute('INSERT OR IGNORE INTO experiment_attempts VALUES(?,?,?,?)', (event,item['contact_id'],'cancelled_inbound',time.time()));self.store.db.commit()
                return True
            if row['unread'] or result['status'] in ('gap','ambiguous','review'):
                raise ValueError('EXPERIMENT_INBOUND_NOT_RESOLVED')
            self.store.db.execute('INSERT OR IGNORE INTO experiment_attempts VALUES(?,?,?,?)', (event,item['contact_id'],'queued',time.time()));self.store.db.commit()
            body={'gateway_id':'win-wechat-maa','event_id':event,'conversation_id':cid,
                  'conversation_title':title,'conversation_type':'private','sender_name':title,
                  'sender_verified':True,'direction':'incoming','stable_ocr':True,'observed_at':time.time(),
                  'text':'实验主动开场任务','context':[],'contact_id':item['contact_id'],'identity':proof,'_opening':True,
                  '_opening_chat_sha':__import__('hashlib').sha256(image[82:887,490:1885,:3].tobytes()).hexdigest()}
            if getattr(self,'greeting',False):body['_greeting']=True
            self.replies.queue(body,result['snapshot_id'],-1,target)
            self.store.event('experiment_opening_queued',{'event_id':event,'contact_id':item['contact_id']})
        except Exception as exc:
            self.navigation_failed(event, str(exc), time.time())
            self.store.event('experiment_navigation_yield',{'event_id':event,'reason':str(exc)[:160]})
        finally:
            runtime.node('OpenRecent')
        return True

    def close(self): self.executor.shutdown(wait=False,cancel_futures=True)
