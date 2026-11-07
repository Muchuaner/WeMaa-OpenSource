"""Operator activation cutoffs; old captures never become new reply permission."""
import json,time

KIND='auto_reply_scope_activated'
SCOPE='all_private_and_whitelisted_groups'


def global_activation(db):
    row=db.execute('SELECT detail_json,seen FROM events WHERE kind=? ORDER BY id DESC LIMIT 1',(KIND,)).fetchone()
    if not row:return 0
    body=json.loads(row[0])
    return float(row[1]) if body.get('scope')==SCOPE and body.get('historical_replay') is False else 0


def cutoff(db,conversation):
    from private_baseline import KIND as BASELINE,canonical,decoded
    value=global_activation(db)
    row=db.execute('SELECT proof_json,source_seen FROM context_baselines WHERE conversation_id=? AND source_kind=? ORDER BY snapshot_id DESC LIMIT 1',(canonical(db,conversation),BASELINE)).fetchone()
    if row and decoded(row[0]).get('receive_only') is False:
        value=max(value,float(decoded(row[0]).get('reply_enabled_at',row[1])))
    return value


def permits(db,body,source_seen=None):
    from private_baseline import receive_only
    cid=body.get('conversation_id','')
    if receive_only(db,cid):return False
    limit=cutoff(db,cid)
    if not limit:return True
    seen=body.get('observed_at')
    return (type(seen) in (int,float) and seen>=limit
            and (source_seen is None or source_seen>=limit))


def activate_all(store,params,command_id):
    row=store.db.execute('SELECT action,status,params_json,expires FROM gui_commands WHERE id=?',(command_id,)).fetchone()
    if (not row or row[0]!='activate_reply_scope' or row[1]!='executing'
            or json.loads(row[2])!=params or row[3]<=time.time()
            or params.get('scope')!=SCOPE or not params.get('reason')):
        raise ValueError('REPLY_SCOPE_WORKER_INTENT_REQUIRED')
    existing=store.db.execute('SELECT detail_json,seen FROM events WHERE kind=? AND json_extract(detail_json,\'$.command_id\')=?',(KIND,command_id)).fetchone()
    if existing:return {**json.loads(existing[0]),'enabled_at':existing[1]}
    # Global controls remain the existing Worker flags; this event only lifts
    # older receive-only baselines and establishes the new-message cutoff.
    body={'scope':SCOPE,'reason':params['reason'],'command_id':command_id,'historical_replay':False}
    store.event(KIND,body)
    return {**body,'enabled_at':global_activation(store.db)}
