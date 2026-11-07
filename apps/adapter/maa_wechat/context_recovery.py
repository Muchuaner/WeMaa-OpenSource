"""Explicit operator review creates a new cursor without deleting gap evidence."""
import json,time
from store import digest,payload_tokens


def review_hash(payloads):
    return digest([(p.get('direction'),p.get('message_kind'),p.get('text'),
                    p.get('visual',{}).get('sha256')) for p in payloads])


def resolve_visible(store,conversation,expected,decisions,command_id):
    view=store.db.execute('SELECT frame_path,payloads_json,seen FROM manual_views WHERE conversation_id=?',(conversation,)).fetchone()
    if not view:raise ValueError('REVIEW_VIEW_MISSING')
    frame,raw,seen=view;payloads=json.loads(raw)
    if time.time()-seen>60 or review_hash(payloads)!=expected:raise ValueError('REVIEW_CONTEXT_CHANGED')
    incoming={str(i) for i,p in enumerate(payloads) if p.get('direction')=='incoming' and p.get('message_kind') not in ('time','sender_label','avatar_ocr')}
    if set(decisions)!=incoming:raise ValueError('REVIEW_INCOMING_NOT_ACCOUNTED')
    for decision in decisions.values():
        if not isinstance(decision,dict) or decision.get('disposition') not in ('no_reply_needed','sent'):raise ValueError('REVIEW_DISPOSITION_INVALID')
        if not isinstance(decision.get('reason'),str) or not decision['reason'].strip():raise ValueError('REVIEW_REASON_REQUIRED')
        if decision['disposition']=='sent':
            row=store.db.execute('SELECT status,detail_json,params_json FROM gui_commands WHERE id=?',(decision.get('delivery_id'),)).fetchone()
            if not row or row[0]!='succeeded':raise ValueError('REVIEW_SEND_NOT_VERIFIED')
            proof=json.loads(row[1]);params=json.loads(row[2])
            if proof.get('status')!='sent' or store.canonical(proof.get('conversation_id') or params.get('conversation_id',''))!=conversation:raise ValueError('REVIEW_SEND_WRONG_CONVERSATION')
    # Captured work is a separate obligation. Never swallow its saved occurrence.
    exists=store.db.execute("SELECT 1 FROM sqlite_master WHERE name='inbound_work'").fetchone()
    keys=[]
    if exists:
        from name_match import normalized,names_match
        from group_policy import is_group
        target=json.loads(store.db.execute('SELECT target_json FROM conversation_routes WHERE conversation_id=?',(conversation,)).fetchone()[0])
        for key,route,snapshot,status in store.db.execute("SELECT row_key,route_json,snapshot_id,status FROM inbound_work WHERE status IN ('pending','captured','review')"):
            row=json.loads(route);owner=store.db.execute('SELECT conversation_id FROM snapshots WHERE id=?',(snapshot,)).fetchone()
            same=(owner and store.canonical(owner[0])==conversation) or (row.get('avatar_sha')==target.get('avatar_sha') and normalized(row.get('name'))==normalized(target.get('name')))
            if not same and is_group(target):
                matches={store.canonical(c) for c,n in store.db.execute("SELECT conversation_id,name FROM conversation_profiles WHERE kind='group'") if names_match(row.get('name'),n)}
                same=matches=={conversation}
            if same:
                if status=='captured':raise ValueError('REVIEW_CAPTURE_REQUIRES_DISPATCH')
                keys.append(key)
    previous=[r[0] for r in store.db.execute("SELECT id FROM snapshots WHERE conversation_id=? AND status IN ('gap','ambiguous','review')",(conversation,))]
    cursor=store.db.execute('INSERT INTO snapshots(conversation_id,frame_path,tokens_json,status,seen) VALUES(?,?,?,?,?)',(conversation,frame,json.dumps(payload_tokens(payloads)),'baseline',seen))
    sid=cursor.lastrowid
    store.db.execute('INSERT INTO snapshot_payloads VALUES(?,?)',(sid,raw))
    proof=dict(decisions=decisions,review_hash=expected,retired_work=keys,retained_gap_snapshots=previous,historical_notifications=False)
    store.db.execute('INSERT INTO context_baselines VALUES(?,?,?,?,?,?)',(sid,conversation,'operator_review',command_id,seen,json.dumps(proof,ensure_ascii=False)))
    for key in keys:store.db.execute("UPDATE inbound_work SET status='resolved',reason='operator_reviewed_visible_context',attempted=? WHERE row_key=?",(time.time(),key))
    store.db.commit()
    store.event('operator_context_resolved',dict(conversation=conversation,snapshot_id=sid,command_id=command_id,**proof))
    return sid


def retry_source(store,conversation,source_id,command_id):
    row=store.db.execute("SELECT action,status,params_json,detail_json FROM gui_commands WHERE id=?",(source_id,)).fetchone()
    if not row or row[0]!='send_delivery' or row[1]!='failed':raise ValueError('RECOVERY_SOURCE_NOT_FAILED')
    params=json.loads(row[2]);proof=json.loads(row[3])
    # This exact reason comes from navigation before claim/import/send.
    if proof.get('status')!='failed' or proof.get('reason')!='TARGET_NOT_FOUND' or proof.get('diagnostic') or proof.get('evidence'):raise ValueError('RECOVERY_SOURCE_NOT_PROVEN_PRECLICK')
    if store.canonical(params.get('conversation_id',''))!=conversation or not params.get('asset_id') or not params.get('path') or not params.get('sha256'):raise ValueError('RECOVERY_ASSET_INVALID')
    for other,raw in store.db.execute("SELECT id,params_json FROM gui_commands WHERE action='recover_media' AND status!='pending' AND id!=?",(command_id,)):
        if json.loads(raw).get('delivery_id')==source_id:raise ValueError('RECOVERY_ALREADY_ATTEMPTED')
    return params
