"""Persist unread work before clicking. A cleared badge is never a receipt."""
import json,time,uuid
from message_vision import message_identity

def tail_for_badge(payloads,count):
    if type(count) is not int or not 1<=count<=99:return None
    tail=[]
    for i,p in reversed([(i,p) for i,p in enumerate(payloads) if message_identity(p)]):
        if p.get('direction')!='incoming':break
        tail.append((i,p))
        if len(tail)==count:break
    if len(tail)!=count:return None
    tail.reverse();keys=[message_identity(p) for _,p in tail]
    if len(keys)!=len(set(keys)):return None
    if any(p.get('message_kind')!='media' and not p.get('reply_safe') for _,p in tail):return None
    return tail

class InboundWork:
    def __init__(self,store):
        self.store=store;self.db=store.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS inbound_work
          (row_key TEXT PRIMARY KEY,seed TEXT,route_json TEXT,count INTEGER,status TEXT,
           snapshot_id INTEGER,reason TEXT,seen REAL,attempted REAL DEFAULT 0)''');self.db.commit()
        self.db.execute('''CREATE TABLE IF NOT EXISTS inbound_captures
            (row_key TEXT,seed TEXT,source_ordinal INTEGER,payload_json TEXT,
             PRIMARY KEY(row_key,seed,source_ordinal))''');self.db.commit()

    def observe(self,rows):
        for r in rows:
            if r.get('unread'):
                self.db.execute('''INSERT OR IGNORE INTO inbound_work
                  (row_key,seed,route_json,count,status,reason,seen) VALUES(?,?,?,?,'pending','unread',?)''',
                  (r['key'],uuid.uuid4().hex,json.dumps(r,ensure_ascii=False),r.get('unread_count'),time.time()))
        self.db.commit()

    def get(self,key):
        row=self.db.execute('SELECT seed,count,status,snapshot_id,reason,seen FROM inbound_work WHERE row_key=?',(key,)).fetchone()
        if not row:return None
        value=dict(zip(('seed','count','status','snapshot_id','reason','seen'),row))
        if value['status']=='review' and value['snapshot_id'] is not None:
            from private_baseline import baseline_for_review
            sid=baseline_for_review(self.db,key,value['seed'],value['snapshot_id'])
            if sid:value=dict(value,status='resolved',reason='operator_private_baseline',original_status='review',superseded_by=sid)
        return value

    def pending(self,rows):
        found=[];now=time.time()
        for r in rows:
            state=self.get(r['key'])
            if not state:continue
            gap_retry=state['status']=='review' and state['reason']=='context_gap_not_verified'
            if state['status'] not in ('pending','captured') and not gap_retry:continue
            if gap_retry:
                if now-state['seen']>240 or self.seed_dispatched(state['seed']):continue
            if state['reason']=='unstable_append':
                seen=self.db.execute('SELECT seen FROM inbound_work WHERE row_key=?',(r['key'],)).fetchone()[0]
                if now-seen>240:
                    self.update(r['key'],'review','verification_retry_expired');continue
            tried=self.db.execute('SELECT attempted FROM inbound_work WHERE row_key=?',(r['key'],)).fetchone()[0]
            if now-tried>=15:found.append((tried,dict(r,schedule={'reason':'unread_recovery','priority':1000000,'weight':1})))
        return [r for _,r in sorted(found,key=lambda x:x[0])[:1]]

    def visible_pending(self,rows):
        return any((self.get(r['key']) or {}).get('status') in ('pending','captured') or
                   ((self.get(r['key']) or {}).get('reason')=='context_gap_not_verified'
                    and time.time()-self.get(r['key'])['seen']<=240
                    and not self.seed_dispatched(self.get(r['key'])['seed'])) for r in rows)

    def seed_dispatched(self,seed):
        exists=self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inbound_dispatch'").fetchone()
        return bool(exists and self.db.execute('SELECT 1 FROM inbound_dispatch WHERE event_seed=? LIMIT 1',(seed,)).fetchone())

    def review_context_gap(self,row,result):
        work=self.get(row['key'])
        if not work or work['status']!='pending' or self.seed_dispatched(work['seed']):return False
        if result['status'] not in ('gap','ambiguous'):return False
        self.update(row['key'],'review','context_gap_not_verified',result['snapshot_id'])
        return True

    def record_context_gap(self,row,result):
        """Retain unhandled history even when a manual read cleared its badge."""
        if result.get('status') not in ('gap','ambiguous'):return False
        work=self.get(row['key'])
        if work and work['status']=='pending':
            return self.review_context_gap(row,result)
        # Never replace canonical captures or an existing unresolved review.
        if work and work['status'] not in ('queued','resolved'):return False
        seed=uuid.uuid4().hex
        self.db.execute('''INSERT INTO inbound_work
            (row_key,seed,route_json,count,status,snapshot_id,reason,seen,attempted)
            VALUES(?,?,?,?,'review',?,'context_gap_not_verified',?,?)
            ON CONFLICT(row_key) DO UPDATE SET seed=excluded.seed,route_json=excluded.route_json,
            count=excluded.count,status=excluded.status,snapshot_id=excluded.snapshot_id,
            reason=excluded.reason,seen=excluded.seen,attempted=excluded.attempted''',
            (row['key'],seed,json.dumps(row,ensure_ascii=False),row.get('unread_count'),
             result['snapshot_id'],time.time(),time.time()))
        self.db.commit()
        self.store.event('inbound_context_gap_review',{'row_key':row['key'],'name':row.get('name'),
            'snapshot_id':result['snapshot_id'],'event_seed':seed,'previous_work':work,
            'reason':'context_gap_not_verified','historical_messages_handled':False})
        return True

    def repair_review_append(self,row,result,payloads):
        """A recent, never-dispatched gap may recover only its exact occurrence."""
        from message_vision import message_occurrence_identity
        work=self.get(row['key'])
        if (not work or work['status']!='review' or work['reason']!='context_gap_not_verified'
                or time.time()-work['seen']>240 or self.seed_dispatched(work['seed'])
                or result.get('status')!='append' or not result.get('metadata',{}).get('new_tail_verbatim')):return None
        originals=self.store.verified_snapshot_payloads(work['snapshot_id'])
        indices=[i for i in range(result['overlap'],len(payloads)) if payloads[i].get('direction')=='incoming']
        if not indices:return None
        mapping={};canonical=[]
        for i in indices:
            token=message_occurrence_identity(payloads[i])
            matches=[j for j,p in enumerate(originals) if token and message_occurrence_identity(p)==token]
            if len(matches)!=1 or not originals[matches[0]].get('reply_safe'):return None
            source=matches[0];original=originals[source]
            mapping[i]=source
            canonical.append((i,dict(original,_inbound_source={'snapshot_id':work['snapshot_id'],
                'source_ordinal':source,'first_seen':work['seen']})))
        for i,original in canonical:
            payloads[i]=dict(payloads[i],text=original['text'],ocr=original.get('ocr',{}),
                             _inbound_source=original['_inbound_source'])
        return dict(result,status='unread_recovery',ordinals=indices,source_ordinals=mapping,event_seed=work['seed'])

    def guard_review_append(self,row,result,payloads):
        """Partition fresh input after old gap evidence before saving a baseline."""
        work=self.get(row['key'])
        if (not work or work['status']!='review' or work['reason']!='context_gap_not_verified'
                or result.get('status') not in ('append','gap','ambiguous')):return result
        if self.repair_review_append(row,result,payloads):return result
        from message_vision import reconcile_visual
        from store import payload_tokens
        strict=reconcile_visual(self.store.verified_snapshot_payloads(work['snapshot_id']),payloads)
        if (strict and strict.get('status')=='append' and strict.get('metadata',{}).get('new_tail_verbatim')
                and any(p.get('direction')=='incoming' for p in payloads[strict['overlap']:])):
            if any(p.get('direction')=='incoming' and not p.get('reply_safe') and
                    not (p.get('message_kind')=='media' and p.get('visual',{}).get('verified') is True)
                    for p in payloads[strict['overlap']:]):
                return dict(result,status='review',new=[],metadata=dict(strict['metadata'],reason='GAP_FRESH_INPUT_NOT_VERIFIED'))
            return dict(result,status='append',overlap=strict['overlap'],new=payload_tokens(payloads)[strict['overlap']:],
                        metadata=dict(strict['metadata'],independent_review_append=True,
                                      reviewed_snapshot_id=work['snapshot_id'],reviewed_seed=work['seed']))
        return dict(result,status='review',new=[],metadata=dict(result.get('metadata',{}),reason='GAP_REVIEW_REQUIRES_OPERATOR'))

    def eligible(self,row):
        item=self.db.execute("SELECT attempted FROM inbound_work WHERE row_key=? AND status IN ('pending','captured')",(row['key'],)).fetchone()
        return not item or time.time()-item[0]>=15

    def update(self,key,status,reason,snapshot=None):
        self.db.execute('UPDATE inbound_work SET status=?,reason=?,snapshot_id=coalesce(?,snapshot_id),attempted=? WHERE row_key=?',
          (status,reason,snapshot,time.time(),key));self.db.commit()

    def navigation_failed(self,key):
        # Captured ordinals and their source snapshot must survive a failed click.
        self.db.execute('UPDATE inbound_work SET attempted=? WHERE row_key=?',(time.time(),key));self.db.commit()

    def retry_unstable_append(self,row,result):
        # Store retained a verified visual overlap, but one new incoming message
        # lacks OCR/sender consensus. Re-read it even after selection clears red.
        # No send has been queued, and the original seed/first-seen time survive.
        if result.get('status')!='review' or not result.get('metadata',{}).get('new_tail_verbatim'):return False
        self.observe([dict(row,unread=True)])
        seen=self.db.execute('SELECT seen FROM inbound_work WHERE row_key=?',(row['key'],)).fetchone()[0]
        if time.time()-seen>240:return False
        self.update(row['key'],'pending','unstable_append',result['snapshot_id'])
        return True

    def count(self):
        return sum(self.get(k)['status'] in ('pending','captured','review') for k, in self.db.execute("SELECT row_key FROM inbound_work WHERE status IN ('pending','captured','review')").fetchall())

    def health(self):
        values=[dict(row_key=k,**self.get(k),attempted=a) for k,a in self.db.execute("SELECT row_key,attempted FROM inbound_work WHERE status IN ('pending','captured','review')").fetchall()]
        counts={}
        for r in values:
            if r['status']!='resolved':counts[r['status']]=counts.get(r['status'],0)+1
        return {'status_counts':counts,'reviews':[dict(row_key=r['row_key'],reason=r['reason'],snapshot_id=r['snapshot_id']) for r in sorted(values,key=lambda r:r['attempted'],reverse=True) if r['status']=='review'][:5],
                'preserved_superseded_reviews':sum(r['status']=='resolved' for r in values)}

    def capture(self,row,result,payloads,ordinals):
        self.observe([dict(row,unread=True)])
        mapping=result.get('source_ordinals') or {i:i for i in ordinals}
        work=self.get(row['key'])
        strict_new=(result.get('status')=='append' and result.get('metadata',{}).get('new_tail_verbatim') is True
                    and result.get('previous_snapshot_id') is not None and ordinals
                    and all(i>=result['overlap'] and payloads[i].get('direction')=='incoming' for i in ordinals))
        if work['status'] in ('queued','resolved') and not strict_new:return False
        rotate=strict_new and (work['status'] in ('queued','resolved') or (work['status']=='review' and
            (work['reason']!='context_gap_not_verified' or result.get('metadata',{}).get('independent_review_append') is True)))
        if rotate:
            if not strict_new:return False
            previous=dict(work)
            seed=uuid.uuid4().hex
            self.db.execute("UPDATE inbound_work SET seed=?,route_json=?,count=?,status='pending',snapshot_id=NULL,reason='unread',seen=?,attempted=0 WHERE row_key=? AND seed=? AND status=?",
                (seed,json.dumps(row,ensure_ascii=False),len(ordinals),time.time(),row['key'],work['seed'],work.get('original_status',work['status'])))
            self.db.commit()
            self.store.event('inbound_new_occurrence_seed',{'row_key':row['key'],'previous_work':previous,'previous_seed':work['seed'],'event_seed':seed,'snapshot_id':result['snapshot_id']})
            work=self.get(row['key'])
        from message_vision import message_occurrence_identity
        for index in ordinals:
            source=mapping.get(index,mapping.get(str(index),index))
            saved=self.db.execute('SELECT payload_json FROM inbound_captures WHERE row_key=? AND seed=? AND source_ordinal=?',
                (row['key'],work['seed'],source)).fetchone()
            if not saved:continue
            original=json.loads(saved[0]);current=payloads[index]
            old_occurrence=message_occurrence_identity(original);new_occurrence=message_occurrence_identity(current)
            old_visual=original.get('visual') or {};new_visual=current.get('visual') or {}
            if (message_identity(original)!=message_identity(current) or
                    ((old_occurrence or new_occurrence) and old_occurrence!=new_occurrence) or
                    (old_visual.get('avatar_verified') is True and
                     (new_visual.get('avatar_verified') is not True or old_visual.get('avatar_sha256')!=new_visual.get('avatar_sha256'))) or
                    (original.get('group_sender_verified') is True and current.get('group_sender_verified') is True
                     and original.get('group_sender_name')!=current.get('group_sender_name'))):
                self.store.review_snapshot(result['snapshot_id'],'INBOUND_CAPTURE_SOURCE_COLLISION')
                self.update(row['key'],'review','capture_source_collision')
                self.store.event('inbound_capture_collision',{'row_key':row['key'],'event_seed':work['seed'],
                    'source_ordinal':source,'snapshot_id':result['snapshot_id']})
                return False
        for index in ordinals:
            source=mapping.get(index,mapping.get(str(index),index))
            self.db.execute('INSERT OR IGNORE INTO inbound_captures VALUES(?,?,?,?)',
                (row['key'],work['seed'],source,json.dumps(payloads[index],ensure_ascii=False)))
        self.db.commit()
        self.update(row['key'],'captured',json.dumps({'ordinals':list(ordinals),'source_ordinals':mapping}),result['snapshot_id'])
        return True

    def source_ordinals(self,row,recovered):
        work=self.get(row['key'])
        if not work or work['status']!='captured':return {i:i for i,_ in recovered}
        detail=json.loads(work['reason']);indices=detail.get('ordinals',[])
        mapping=detail.get('source_ordinals',{})
        return {current:mapping.get(str(original),mapping.get(original,original))
                for (current,_),original in zip(recovered,indices)}

    def recover(self,row,payloads):
        work=self.get(row['key'])
        if not work or work['status'] in ('queued','resolved'):return None
        if work['status']=='captured' and work['snapshot_id']:
            source=self.db.execute('SELECT payloads_json FROM snapshot_payloads WHERE snapshot_id=?',(work['snapshot_id'],)).fetchone()
            originals=self.store.verified_snapshot_payloads(work['snapshot_id']) if source else []
            indices=json.loads(work['reason']).get('ordinals',[])
            pairs=[]
            for index in indices:
                detail=json.loads(work['reason']);mapping=detail.get('source_ordinals',{})
                source=mapping.get(str(index),mapping.get(index,index))
                saved=self.db.execute('SELECT payload_json FROM inbound_captures WHERE row_key=? AND seed=? AND source_ordinal=?',
                    (row['key'],work['seed'],source)).fetchone()
                original=json.loads(saved[0]) if saved else originals[index];token=message_identity(original)
                matches=[i for i,p in enumerate(payloads) if token and message_identity(p)==token]
                if len(matches)>1:
                    from message_vision import message_occurrence_identity
                    occurrence=message_occurrence_identity(original)
                    if not occurrence and index<len(originals) and message_identity(originals[index])==token:
                        # Timestamp evidence may be derived from the original
                        # source frame; canonical text/identity stay untouched.
                        candidate=dict(original,time_anchor=originals[index].get('time_anchor'))
                        if candidate.get('visual',{}).get('box')==originals[index].get('visual',{}).get('box'):
                            occurrence=message_occurrence_identity(candidate)
                    matches=[i for i in matches if occurrence and message_occurrence_identity(payloads[i])==occurrence]
                if len(matches)!=1:return None
                original_visual=original.get('visual') or {};current_visual=payloads[matches[0]].get('visual') or {}
                if (original_visual.get('avatar_verified') is True and
                        (current_visual.get('avatar_verified') is not True or
                         current_visual.get('avatar_sha256')!=original_visual.get('avatar_sha256'))):return None
                pairs.append((matches[0],original))
            return pairs
        return tail_for_badge(payloads,work['count'])

    def retire_badges(self,rows):
        for r in rows:
            if not r.get('unread'):
                self.db.execute("DELETE FROM inbound_work WHERE row_key=? AND status='queued'",(r['key'],))
        self.db.commit()
