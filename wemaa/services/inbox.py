"""Verified private incoming text, stable occurrences and a bounded cursor feed."""
from __future__ import annotations

import hashlib
import json
import math
import re

from wemaa.services import maa as maa

KEY = maa.PREFIX + 'received-api:feed'
SEQ_KEY = maa.PREFIX + 'received-api:seq'
MAX_RECORDS = 5000
MAX_SEEN = 20000


def valid_time(value, low, high):
    return type(value) in (int,float) and math.isfinite(value) and low <= value <= high


def individually_observed(message):
    """A partial batch needs the immutable per-occurrence receive journal."""
    receipt=message.get('message_receipt')
    if not isinstance(receipt,dict) or message.get('source_snapshot_status') not in ('append','review'):
        return False
    event=receipt.get('event_id')
    return (receipt.get('kind')=='unique_visual_append'
            and isinstance(event,str) and re.fullmatch(r'[a-f0-9]{64}',event) is not None
            and event[:32]==message.get('id')
            and type(receipt.get('snapshot_id')) is int and receipt['snapshot_id']>0
            and receipt['snapshot_id']==message.get('snapshot_id')
            and type(receipt.get('ordinal')) is int and receipt['ordinal']>=0
            and receipt['ordinal']==message.get('ordinal')
            and type(receipt.get('first_seen')) in (int,float)
            and math.isfinite(receipt['first_seen']) and receipt['first_seen']==message.get('seen'))


def sources(body, now):
    contacts={r['id']:r for r in body.get('contacts',[]) if r.get('scope')=='personal'
              and r.get('identity_status')=='ocr_wechat_id' and isinstance(r.get('facts'),dict)
              and r['facts'].get('scope')=='personal'}
    for conversation in body.get('conversations',[]):
        binding=conversation.get('private_contact') or {}
        if not isinstance(binding,dict) or conversation.get('group_profile') or conversation.get('group_profile_status'):continue
        if not isinstance(binding.get('contact_id'),str):continue
        contact=contacts.get(binding['contact_id'])
        if (not contact or binding.get('profile_verified') is not True
                or binding.get('wechat_id')!=contact.get('wechat_id')
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',str(binding.get('wechat_id','')))
                or not valid_time(binding.get('verified_at'),0,now+30)):continue
        allowed_ids={conversation['id'],*conversation.get('aliases',[])}
        for message in conversation.get('messages',[]):
            payload=message.get('payload') or {}
            if not isinstance(payload,dict):continue
            text=payload.get('text');proof=payload.get('text_copy') or {};visual=payload.get('visual') or {}
            if ((message.get('source_snapshot_status')!='append' and not individually_observed(message))
                    or message.get('conversation_id') not in allowed_ids
                    or not re.fullmatch(r'[a-f0-9]{32}',str(message.get('id','')))
                    or not valid_time(message.get('seen'),now-maa.HISTORY_TTL,now+30)
                    or payload.get('direction')!='incoming' or payload.get('message_kind')!='text'
                    or payload.get('source')!='maa_native_clipboard'
                    or not isinstance(text,str) or not text.strip() or len(text)>16384
                    or not isinstance(proof,dict) or proof.get('verified') is not True
                    or proof.get('method')!='native_context_menu'
                    or proof.get('sha256')!=hashlib.sha256(text.encode('utf-8')).hexdigest()
                    or not isinstance(visual,dict) or visual.get('verified') is not True):continue
            yield {'id':message['id'],'conversation_id':conversation['id'],
                   'sender':{'contact_id':binding['contact_id'],'wechat_id':binding['wechat_id']},
                   'direction':'incoming','text':text,'observed_at':message['seen']}


def merge(feed, body, now, sequence):
    """Repeated sync keeps the original occurrence ID, cursor and receipt time."""
    sequence=max(sequence,feed.get('seq',0))
    seen={k:v for k,v in feed.get('seen',{}).items() if v >= now-maa.HISTORY_TTL}
    items=[r for r in feed.get('items',[]) if r['observed_at'] >= now-maa.HISTORY_TTL]
    removed=[r['cursor'] for r in feed.get('items',[]) if r['observed_at'] < now-maa.HISTORY_TTL]
    ignore_through=feed.get('ignore_through',0)
    for row in sorted(sources(body,now),key=lambda r:(r['observed_at'],r['id'])):
        if row['id'] in seen or row['observed_at'] <= ignore_through:continue
        sequence+=1
        items.append(dict(row,cursor=sequence,received_at=now))
        seen[row['id']]=row['observed_at']
    if len(seen)>MAX_SEEN:
        old=sorted(seen,key=seen.get)[:-MAX_SEEN]
        ignore_through=max(ignore_through,max(seen[k] for k in old))
        for k in old:del seen[k]
    removed.extend(r['cursor'] for r in items[:-MAX_RECORDS])
    trimmed=max([feed.get('trimmed_through',0),*removed])
    return {'seq':sequence,'items':items[-MAX_RECORDS:],'seen':seen,
            'trimmed_through':trimmed,'ignore_through':ignore_through},sequence


def read(sender, after, limit, *, since=0, ready=True):
    cache=maa.client()
    raw,sequence=cache.mget([KEY,SEQ_KEY])
    feed=json.loads(raw or '{}')
    sequence=int(sequence or 0)
    latest_seq=feed.get('seq',sequence)
    if after>latest_seq:raise maa.invalid('after 超过当前收件游标')
    now=maa.time.time()
    retained=[r for r in feed.get('items',[]) if r['observed_at'] >= now-maa.HISTORY_TTL]
    removed=[r['cursor'] for r in feed.get('items',[]) if r['observed_at'] < now-maa.HISTORY_TTL]
    trimmed=max([feed.get('trimmed_through',sequence if not feed else 0),*removed])
    rows=[r for r in retained if ready and r['sender']==sender and r['cursor']>after and r['observed_at']>=since]
    selected=rows[:limit]
    next_cursor=(selected[-1]['cursor'] if len(rows)>limit else latest_seq) if ready else after
    record=maa.latest();body=record['telemetry'] if record else {}
    return {'items':[{k:v for k,v in row.items() if k!='conversation_id'} for row in selected],
            'next_cursor':next_cursor,'has_more':len(rows)>limit,'latest_cursor':latest_seq,
            'cursor_expired':bool(after and after<trimmed),'history_truncated':bool(trimmed),
            'retention_seconds':maa.HISTORY_TTL,'max_records':MAX_RECORDS,
            'coverage':'observed_only','stale':maa.is_stale(record),
            'collected_at':body.get('collected_at'),
            'polling_enabled':body.get('runtime',{}).get('progress',{}).get('polling_enabled')}
