"""Read-only review manifests and explicit, single-Worker private baselines.

Old review snapshots/work remain evidence. No historical message/dispatch/reply
rows are created. Fresh observations must match the operator's frozen candidate.
"""
import hashlib
import json
import re
import time

from contact_identity import canonical, valid_profile_proof
from message_vision import message_identity

KIND = 'operator_private_baseline'


def decoded(raw):
    return json.loads(raw or '{}')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def rows(db, sql, params=()):
    cur = db.execute(sql, params)
    return [dict(zip((x[0] for x in cur.description), tuple(row))) for row in cur]


def content_fingerprint(payloads):
    # Copy menu frames and incidental OCR labels change between observations.
    # Message pixels, geometry, portraits, time evidence and verbatim text do not.
    return digest([{k:p.get(k) for k in ('direction','message_kind','text','visual','time_anchor')}
                   for p in payloads if message_identity(p)])


def verified_texts(payloads):
    found = []
    for ordinal,p in enumerate(payloads):
        if p.get('direction') not in ('incoming','outgoing'):
            continue
        if not message_identity(p):
            raise ValueError('BASELINE_VISUAL_NOT_VERIFIED')
        if p.get('message_kind') == 'media' and p.get('direction') == 'outgoing':
            continue
        cp = p.get('text_copy') or {}; text = p.get('text')
        if (p.get('message_kind') != 'text' or p.get('source') != 'maa_native_clipboard'
                or not isinstance(text,str) or not text.strip()
                or cp.get('verified') is not True or cp.get('method') != 'native_context_menu'
                or cp.get('sha256') != hashlib.sha256(text.encode()).hexdigest()
                or (p['direction'] == 'incoming' and p.get('reply_safe') is not True)):
            raise ValueError('BASELINE_TEXT_NOT_VERIFIED')
        if p['direction'] == 'incoming':
            found.append(dict(ordinal=ordinal,text=text,sha256=cp['sha256']))
    return found


def frame_proof(frame, payloads):
    """Read saved pixels, never capture or repair stored payloads."""
    from pathlib import Path
    from PIL import Image
    import numpy as np
    path = Path(frame)
    with Image.open(path) as image:
        if image.size != (1920,1040):
            raise ValueError('BASELINE_FRAME_NOT_VERIFIED')
        rgb = np.asarray(image.convert('RGB'))
    for p in payloads:
        if not message_identity(p):continue
        v = p['visual']; x,y,w,h = v['box']; crop = rgb[y:y+h,x:x+w,:3]
        if (x<490 or y<82 or x+w>1895 or y+h>887 or crop.shape!=(h,w,3)
                or v['sha256'] not in (hashlib.sha256(crop.tobytes()).hexdigest(),
                                      hashlib.sha256(crop[:,:,::-1].copy().tobytes()).hexdigest())):
            raise ValueError('BASELINE_FRAME_NOT_VERIFIED')
        if v.get('avatar_verified') is True:
            ax,ay,aw,ah = v.get('avatar_box',[0,0,0,0]); avatar = rgb[ay:ay+ah,ax:ax+aw,:3]
            if avatar.shape!=(ah,aw,3) or hashlib.sha256(avatar.tobytes()).hexdigest()!=v.get('avatar_sha256'):
                raise ValueError('BASELINE_FRAME_NOT_VERIFIED')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scope(db, contact_id, conversation, wechat_id):
    if not re.fullmatch('[a-f0-9]{64}',str(conversation)):
        raise ValueError('BASELINE_CONVERSATION_INVALID')
    contact = db.execute('SELECT wechat_id,identity_status,facts_json FROM contacts WHERE id=?',(contact_id,)).fetchone()
    link = db.execute('SELECT conversation_id FROM contact_chat_links WHERE contact_id=?',(contact_id,)).fetchone()
    if (not contact or contact[0]!=wechat_id or contact[1]!='ocr_wechat_id'
            or decoded(contact[2]).get('scope')!='personal' or not link
            or canonical(db,conversation)!=conversation or canonical(db,link[0])!=conversation):
        raise ValueError('BASELINE_PRIVATE_IDENTITY_MISMATCH')
    ids = {conversation}
    ids.update(r[0] for r in db.execute('SELECT alias FROM group_aliases') if canonical(db,r[0])==conversation)
    if any(canonical(db,c) == conversation for c, in db.execute("SELECT conversation_id FROM conversation_profiles WHERE kind='group'")):
        raise ValueError('BASELINE_PRIVATE_IDENTITY_MISMATCH')
    return sorted(ids)


def review_manifest(db, contact_id, conversation, wechat_id, snapshot_id, *, receive_only=True):
    """SELECT + saved-file reads only; suitable for mode=ro/query_only."""
    ids = scope(db,contact_id,conversation,wechat_id); marks=','.join('?' for _ in ids)
    candidate = rows(db,'''SELECT s.*,p.payloads_json FROM snapshots s JOIN snapshot_payloads p
                          ON p.snapshot_id=s.id WHERE s.id=?''',(snapshot_id,))
    if not candidate or candidate[0]['conversation_id'] not in ids or candidate[0]['status'] not in ('review','gap','ambiguous'):
        raise ValueError('BASELINE_CANDIDATE_NOT_REVIEW')
    candidate = candidate[0]; payloads=decoded(candidate['payloads_json'])
    incoming = verified_texts(payloads)
    if not incoming or len({message_identity(p) for p in payloads if message_identity(p)})<2:raise ValueError('BASELINE_ANCHORS_INSUFFICIENT')
    frame_sha=frame_proof(candidate['frame_path'],payloads)
    reliable = rows(db,f"SELECT id,seen,status FROM snapshots WHERE conversation_id IN ({marks}) AND status IN ('baseline','append','unchanged') ORDER BY seen DESC,id DESC LIMIT 1",ids)
    if reliable and reliable[0]['id']>=snapshot_id:
        raise ValueError('BASELINE_CANDIDATE_SUPERSEDED')
    work=[]
    if 'inbound_work' in tables(db):
        for row in rows(db,"SELECT * FROM inbound_work WHERE status IN ('pending','captured','review')"):
            owner=db.execute('SELECT conversation_id FROM snapshots WHERE id=?',(row['snapshot_id'],)).fetchone()
            if row['row_key'] in ids or (owner and canonical(db,owner[0])==conversation):work.append(row)
    work.sort(key=lambda r:r['row_key'])
    blocked=[dict(kind='inbound_work',id=r['row_key'],status=r['status']) for r in work if r['status']!='review']
    if 'inbound_captures' in tables(db):
        keys={r['row_key'] for r in work}|set(ids)
        blocked.extend(dict(kind='inbound_capture',id=k,status='captured') for k, in db.execute('SELECT DISTINCT row_key FROM inbound_captures') if k in keys)
    for table,statuses in [('reply_outbox',('queued','ready','send_started')),
                           ('inbound_dispatch',('pending','dispatching','registered'))]:
        if table in tables(db):
            for row in rows(db,f'SELECT * FROM {table} WHERE conversation_id IN ({marks})',ids):
                if row['status'] in statuses:blocked.append(dict(kind=table,id=row['event_id'],status=row['status']))
    if 'creation_events' in tables(db):
        for row in rows(db,"SELECT id,body,status FROM creation_events WHERE status='pending'"):
            if decoded(row['body']).get('conversation_id') in ids:blocked.append(dict(kind='creation_event',id=row['id'],status=row['status']))
    for row in rows(db,"SELECT id,action,params_json,status FROM gui_commands WHERE status IN ('pending','executing')"):
        params=decoded(row['params_json'])
        if row['action'] in ('accept_private_baseline','activate_private_replies'):continue
        if params.get('contact_id')==contact_id or params.get('conversation_id') in ids:
            blocked.append(dict(kind='gui_command',id=row['id'],status=row['status']))
    cutoff=reliable[0]['id'] if reliable else 0
    history=rows(db,f'SELECT id,status,seen FROM snapshots WHERE conversation_id IN ({marks}) AND id>? AND id<=? ORDER BY id',[*ids,cutoff,snapshot_id])
    counts={}
    for r in history:counts[r['status']]=counts.get(r['status'],0)+1
    manifest=dict(contact_id=contact_id,wechat_id=wechat_id,conversation_id=conversation,aliases=ids,
        candidate=dict(snapshot_id=snapshot_id,seen=candidate['seen'],status=candidate['status'],
                       frame_sha256=frame_sha,payload_sha256=digest(payloads),content_sha256=content_fingerprint(payloads)),
        last_reliable=reliable[0] if reliable else None,
        skip_history=dict(first=history[0] if history else None,last=history[-1] if history else None,
                          count=len(history),status_counts=counts,snapshot_ids_sha256=digest([r['id'] for r in history])),
        incoming_history=incoming,reviewed_work=work,blocked_by=blocked,
        effects=dict(receive_only=receive_only,historical_notifications=False,historical_messages_imported=False,
                     old_snapshots_preserved=True,old_work_preserved=True,scope='selected_private_conversation'))
    return dict(manifest=manifest,review_hash=digest(manifest),identity_reverification_required=True)


def baseline_for_review(db, row_key, seed, snapshot_id):
    owner=db.execute('SELECT conversation_id FROM snapshots WHERE id=?',(snapshot_id,)).fetchone()
    if not owner:return None
    conversation=canonical(db,owner[0])
    for sid,raw in db.execute('SELECT snapshot_id,proof_json FROM context_baselines WHERE conversation_id=? AND source_kind=? ORDER BY snapshot_id DESC',(conversation,KIND)):
        for r in decoded(raw).get('approved_manifest',{}).get('reviewed_work',[]):
            if (r['row_key'],r['seed'],r['snapshot_id'],r['status'])==(row_key,seed,snapshot_id,'review'):return sid
    return None


def receive_only(db, conversation):
    row=db.execute('SELECT proof_json,source_seen FROM context_baselines WHERE conversation_id=? AND source_kind=? ORDER BY snapshot_id DESC LIMIT 1',(canonical(db,conversation),KIND)).fetchone()
    from reply_policy import global_activation
    if global_activation(db)>(row[1] if row else 0):return False
    if not row:return False
    proof=decoded(row[0]);expiry=proof.get('auto_reply_expires_at')
    return proof.get('receive_only') is True or (expiry is not None and time.time()>=expiry)


def accept_baseline(store, params, command_id, fresh_profile):
    """Only the GUI owner's durably claimed intent may commit an approved baseline."""
    db=store.db; own=False
    try:
        journal=db.execute('SELECT action,status,params_json,expires FROM gui_commands WHERE id=?',(command_id,)).fetchone()
        if not journal or journal[0] not in ('accept_private_baseline','activate_private_replies') or decoded(journal[2])!=params:
            raise ValueError('BASELINE_WORKER_INTENT_REQUIRED')
        previous=db.execute('SELECT snapshot_id,proof_json FROM context_baselines WHERE source_kind=? AND source_id=?',(KIND,command_id)).fetchone()
        if previous:
            if decoded(previous[1]).get('approved_hash')!=params['review_hash']:raise ValueError('BASELINE_APPROVAL_CONFLICT')
            return previous[0]
        if journal[3]<=time.time():raise ValueError('MANUAL_COMMAND_EXPIRED')
        if journal[1]!='executing' or params.get('receive_only') is not (journal[0]=='accept_private_baseline') or not str(params.get('reason','')).strip():
            raise ValueError('BASELINE_CONFIRMATION_REQUIRED')
        if not valid_profile_proof(fresh_profile,params['wechat_id']):
            raise ValueError('BASELINE_FRESH_PROFILE_REQUIRED')
        db.execute('BEGIN IMMEDIATE');own=True
        preview=review_manifest(db,params['contact_id'],params['conversation_id'],params['wechat_id'],params['snapshot_id'],receive_only=params['receive_only'])
        m=preview['manifest']
        if preview['review_hash']!=params['review_hash']:raise ValueError('BASELINE_REVIEW_CHANGED')
        if m['blocked_by']:raise ValueError('BASELINE_PENDING_OBLIGATION')
        stored=db.execute('SELECT conversation_id,proof_json FROM contact_chat_history WHERE contact_id=?',(params['contact_id'],)).fetchall()
        if not any(valid_profile_proof(decoded(p),params['wechat_id']) and decoded(p).get('profile_sha256')==fresh_profile['profile_sha256'] and decoded(p).get('observation_sha256')==fresh_profile['observation_sha256'] and canonical(db,c)==params['conversation_id'] and decoded(p).get('contact_id')==params['contact_id'] and decoded(p).get('conversation_id')==c for c,p in stored):
            raise ValueError('BASELINE_PROFILE_NOT_PERSISTED')
        view=db.execute('SELECT frame_path,payloads_json,seen FROM manual_views WHERE conversation_id=?',(params['conversation_id'],)).fetchone()
        if not view or not 0<=time.time()-view[2]<=60:raise ValueError('BASELINE_FRESH_VIEW_REQUIRED')
        payloads=decoded(view[1]);verified_texts(payloads);frame_proof(view[0],payloads)
        if content_fingerprint(payloads)!=m['candidate']['content_sha256']:raise ValueError('BASELINE_CONTEXT_CHANGED')
        ids=m['aliases'];marks=','.join('?' for _ in ids)
        repeats=rows(db,f'''SELECT s.id,p.payloads_json FROM snapshots s JOIN snapshot_payloads p ON p.snapshot_id=s.id
                           WHERE s.conversation_id IN ({marks}) AND s.id>?''',[*ids,params['snapshot_id']])
        if any(content_fingerprint(decoded(r['payloads_json']))!=m['candidate']['content_sha256'] for r in repeats):
            raise ValueError('BASELINE_UNREVIEWED_OBSERVATION')
        if journal[3]<=time.time():raise ValueError('MANUAL_COMMAND_EXPIRED')
        from store import payload_tokens
        sid=db.execute('INSERT INTO snapshots(conversation_id,frame_path,tokens_json,status,seen) VALUES(?,?,?,?,?)',
                       (params['conversation_id'],view[0],json.dumps(payload_tokens(payloads)),'baseline',view[2])).lastrowid
        db.execute('INSERT INTO snapshot_payloads VALUES(?,?)',(sid,view[1]))
        proof=dict(approved_hash=params['review_hash'],approved_manifest=m,reason=params['reason'],
                   fresh_profile=fresh_profile,receive_only=params['receive_only'],historical_notifications=False,
                   repeated_observation_ids=[r['id'] for r in repeats])
        if journal[0]=='activate_private_replies':
            expiry=params.get('auto_reply_expires_at')
            if type(expiry) not in (int,float) or not time.time()<expiry<=time.time()+3600:raise ValueError('REPLY_ACTIVATION_EXPIRY_INVALID')
            proof.update(reply_enabled_at=time.time(),auto_reply_expires_at=expiry)
        db.execute('INSERT INTO context_baselines VALUES(?,?,?,?,?,?)',(sid,params['conversation_id'],KIND,command_id,view[2],json.dumps(proof,ensure_ascii=False)))
        db.execute('INSERT INTO events(kind,detail_json,seen) VALUES(?,?,?)',('private_baseline_accepted',json.dumps(dict(conversation_id=params['conversation_id'],snapshot_id=sid,command_id=command_id,**proof),ensure_ascii=False),time.time()))
        db.commit();own=False
        return sid
    except Exception as exc:
        if own:db.rollback()
        store.event('private_baseline_confirmation_blocked',dict(command_id=command_id,conversation_id=params.get('conversation_id'),reason=str(exc)))
        raise
