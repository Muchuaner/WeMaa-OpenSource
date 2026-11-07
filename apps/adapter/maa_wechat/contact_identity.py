"""Bind private routes only after fresh full-account and chat verification."""
import json
import re
import time
from group_policy import is_group


def valid_profile_proof(proof, wechat_id):
    return (isinstance(proof, dict) and proof.get('stable') is True
            and proof.get('wechat_id') == wechat_id
            and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,256}', str(wechat_id or '')))
            and all(re.fullmatch(r'[a-f0-9]{64}', str(proof.get(k, '')))
                    for k in ('profile_sha256', 'observation_sha256')))


def canonical(db, conversation):
    seen = set()
    while True:
        if conversation in seen:
            raise ValueError('PRIVATE_ALIAS_CYCLE')
        seen.add(conversation)
        row = db.execute('SELECT canonical FROM group_aliases WHERE alias=?', (conversation,)).fetchone()
        if not row:
            return conversation
        conversation = row[0]


def link_verified_chat(store, contact_id, conversation, target, proof):
    db = store.db
    before = []
    try:
        row = db.execute('SELECT wechat_id,identity_status,facts_json FROM contacts WHERE id=?', (contact_id,)).fetchone()
        chat = db.execute('SELECT title,avatar_sha FROM conversations WHERE id=?', (conversation,)).fetchone()
        if (not row or row[1] != 'ocr_wechat_id' or json.loads(row[2] or '{}').get('scope') != 'personal'
                or not valid_profile_proof(proof, row[0]) or is_group(target)
                or not re.fullmatch('[a-f0-9]{64}', str(conversation)) or not chat
                or tuple(chat) != (target.get('conversation_title'), target.get('avatar_sha'))):
            raise ValueError('PRIVATE_PROFILE_BINDING_NOT_VERIFIED')
        if (proof.get('contact_id', contact_id) != contact_id
                or proof.get('conversation_id', conversation) != conversation):
            raise ValueError('PRIVATE_PROFILE_BINDING_CONFLICT')
        db.execute('SAVEPOINT verified_private_link')
        try:
            db.execute('CREATE TABLE IF NOT EXISTS contact_chat_history(contact_id TEXT,conversation_id TEXT,seen REAL,proof_json TEXT,PRIMARY KEY(contact_id,conversation_id))')
            previous = db.execute('SELECT conversation_id,seen FROM contact_chat_links WHERE contact_id=?', (contact_id,)).fetchone()
            history = db.execute('SELECT conversation_id,seen,proof_json FROM contact_chat_history WHERE contact_id=? ORDER BY seen,conversation_id', (contact_id,)).fetchall()
            before = [dict(conversation_id=c, seen=s, proof=json.loads(p)) for c,s,p in history]
            current = canonical(db, conversation)
            owner = canonical(db, previous[0] if previous else history[0][0] if history else conversation)
            for item in before:
                old = item['proof']
                if (not isinstance(old, dict) or old.get('wechat_id', row[0]) != row[0]
                        or old.get('contact_id', contact_id) != contact_id
                        or old.get('conversation_id', item['conversation_id']) != item['conversation_id']
                        or canonical(db, item['conversation_id']) not in {owner, current}):
                    raise ValueError('PRIVATE_PROFILE_BINDING_CONFLICT')
            if current != owner and not any(
                    canonical(db, item['conversation_id']) == owner
                    and valid_profile_proof(item['proof'], row[0])
                    and item['proof'].get('contact_id') == contact_id
                    and item['proof'].get('conversation_id') == item['conversation_id']
                    for item in before):
                raise ValueError('PRIVATE_PROFILE_ROUTE_CHANGE_REQUIRES_VERIFIED_HISTORY')
            for other, cid in db.execute('SELECT contact_id,conversation_id FROM contact_chat_links'):
                if other != contact_id and canonical(db, cid) in {owner, current}:
                    raise ValueError('PRIVATE_PROFILE_BINDING_CONFLICT')
            for other, cid in db.execute('SELECT contact_id,conversation_id FROM contact_chat_history'):
                if other != contact_id and canonical(db, cid) in {owner, current}:
                    raise ValueError('PRIVATE_PROFILE_BINDING_CONFLICT')
            if db.execute("SELECT 1 FROM conversation_profiles WHERE conversation_id IN (?,?) AND kind='group'", (owner,current)).fetchone():
                raise ValueError('PRIVATE_PROFILE_BINDING_CONFLICT')
            # The prior row is historical evidence, never a source of account proof.
            if previous:
                db.execute('INSERT OR IGNORE INTO contact_chat_history VALUES(?,?,?,?)',
                           (contact_id, *previous, json.dumps({'source':'previous_verified_profile_link'})))
            verified = dict(proof, contact_id=contact_id, conversation_id=conversation)
            old = db.execute('SELECT seen,proof_json FROM contact_chat_history WHERE contact_id=? AND conversation_id=?', (contact_id,conversation)).fetchone()
            changed = not old or json.loads(old[1]) != verified
            now = time.time()
            if changed:
                db.execute('''INSERT INTO contact_chat_history VALUES(?,?,?,?)
                    ON CONFLICT(contact_id,conversation_id) DO UPDATE SET seen=excluded.seen,proof_json=excluded.proof_json''',
                           (contact_id, conversation, now, json.dumps(verified)))
            if current != owner:
                db.execute('INSERT OR REPLACE INTO group_aliases VALUES(?,?,?,?)', (current,owner,'same_reverified_private_wechat_id',now))
                db.execute('UPDATE conversations SET title=?,avatar_sha=?,last_seen=? WHERE id=?', (*chat,now,owner))
                db.execute('INSERT OR REPLACE INTO manual_views SELECT ?,frame_path,payloads_json,seen FROM manual_views WHERE conversation_id=?', (owner,conversation))
                db.execute('INSERT OR REPLACE INTO conversation_routes VALUES(?,?,?)', (owner,json.dumps(target,ensure_ascii=False),now))
            if not previous or previous[0] != owner or changed:
                db.execute('INSERT OR REPLACE INTO contact_chat_links VALUES(?,?,?)', (contact_id,owner,now))
            db.execute('RELEASE verified_private_link')
            db.commit()
        except Exception:
            db.execute('ROLLBACK TO verified_private_link')
            db.execute('RELEASE verified_private_link')
            raise
    except (ValueError, TypeError) as exc:
        store.event('private_profile_binding_blocked', dict(contact_id=contact_id,visual_conversation=conversation,
                    reason=str(exc),previous=before,proof=proof))
        raise
    if changed:
        store.event('private_profile_route_linked', dict(contact_id=contact_id,visual_conversation=conversation,
                    canonical=owner,proof=verified,previous=before))
    return owner
