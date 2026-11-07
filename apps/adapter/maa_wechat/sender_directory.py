"""Message avatar identities, separate from the conversation's list avatar.

Visual keys are provisional. A WeChat ID needs this message's profile proof,
or a verified occurrence match back to that proof; names alone never bind.
"""
import hashlib
import json
import re
import time
import numpy as np


def visual_sender(conversation, payload, title=None):
    visual = payload.get('visual') or {}
    avatar = visual.get('avatar_sha256')
    direction = payload.get('direction')
    if (direction not in ('incoming', 'outgoing') or visual.get('avatar_verified') is not True
            or not isinstance(avatar, str) or not re.fullmatch(r'[a-f0-9]{64}', avatar)):
        return None
    name = (payload.get('group_sender_name') if payload.get('group_sender_verified') else None)
    name = name or ('本机账号' if direction == 'outgoing' else title or '身份待核验')
    key = hashlib.sha256(json.dumps([conversation, direction, avatar, name],
                                  ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return {'key': key, 'avatar': avatar, 'name': name, 'direction': direction}


class SenderDirectory:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS message_senders
            (identity_key TEXT PRIMARY KEY, conversation_id TEXT, direction TEXT,
             avatar_sha TEXT, display_name TEXT, wechat_id TEXT, proof_json TEXT,
             verified_at REAL)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS message_sender_attempts
            (identity_key TEXT PRIMARY KEY, attempted REAL, reason TEXT)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS message_sender_observations
            (observation_key TEXT PRIMARY KEY,identity_key TEXT,display_name TEXT,
             wechat_id TEXT,proof_json TEXT,verified_at REAL)''')
        self.db.commit()

    def remember(self, conversation, payload, proof, title=None):
        sender = visual_sender(conversation, payload, title)
        if (not sender or proof.get('stable') is not True
                or proof.get('message_avatar_sha256') != sender['avatar']
                or not re.fullmatch(r'[A-Za-z0-9_-]{3,128}', str(proof.get('wechat_id', '')))):
            raise ValueError('MESSAGE_SENDER_PROOF_NOT_BOUND')
        old = self.db.execute('SELECT wechat_id FROM message_senders WHERE identity_key=?',
                              (sender['key'],)).fetchone()
        if old and old[0] != proof['wechat_id']:
            raise ValueError('MESSAGE_SENDER_PROFILE_CONFLICT')
        self.db.execute('''INSERT INTO message_senders VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(identity_key) DO UPDATE SET proof_json=excluded.proof_json,
            verified_at=excluded.verified_at''', (sender['key'], conversation, sender['direction'],
            sender['avatar'], proof.get('display_name') or sender['name'], proof['wechat_id'],
            json.dumps(proof, ensure_ascii=False), time.time()))
        observation=payload.get('sender_observation_key')
        if isinstance(observation,str) and re.fullmatch(r'[a-f0-9]{64}',observation):
            self.db.execute('INSERT INTO message_sender_observations VALUES(?,?,?,?,?,?) '
                'ON CONFLICT(observation_key) DO UPDATE SET display_name=excluded.display_name,'
                'wechat_id=excluded.wechat_id,proof_json=excluded.proof_json,verified_at=excluded.verified_at',
                (observation,sender['key'],proof.get('display_name') or sender['name'],proof['wechat_id'],
                 json.dumps(proof,ensure_ascii=False),time.time()))
        self.db.commit()

    def enrich(self, conversation, payloads, title=None, frame_sha256=None):
        result = []
        for payload in payloads:
            item = dict(payload)
            for key in ('sender_name','sender_wechat_id','sender_identity_key','sender_identity_status',
                        'sender_identity_verified','sender_identity_verified_at'):
                item.pop(key,None)
            sender = visual_sender(conversation, item, title)
            if sender:
                if frame_sha256:
                    item['sender_observation_key']=hashlib.sha256(json.dumps([frame_sha256,sender['key'],
                        item['visual'].get('box'),item['visual'].get('avatar_box')],sort_keys=True).encode()).hexdigest()
                    item['sender_observation_frame_sha256']=frame_sha256
                item.update(sender_name=sender['name'], sender_identity_key=sender['key'],
                            sender_identity_status='visual_provisional', sender_identity_verified=False)
                row = self.db.execute('SELECT display_name,wechat_id,verified_at FROM message_sender_observations '
                                      'WHERE observation_key=? AND identity_key=?',
                                      (item.get('sender_observation_key'),sender['key'])).fetchone()
                if row:
                    item.update(sender_name=row[0], sender_wechat_id=row[1],
                                sender_identity_status='profile_verified', sender_identity_verified=True,
                                sender_identity_verified_at=row[2])
            result.append(item)
        return result

    def transfer_occurrences(self, conversation, payloads, reconciliation, title=None):
        """Carry a profile proof only through a Store-verified message occurrence.

        The source is a stored snapshot's observation, never the visual member
        directory. Recompute the ordered match, retain the original profile
        evidence, and bind the new observation to its current pixels and frame.
        Newly appended, ambiguous, changed, or unobserved messages never inherit
        a wxid; their own current-frame profile proof remains authoritative.
        """
        result = self.enrich(conversation, payloads, title)
        metadata = reconciliation.get('metadata') or {}
        matches = metadata.get('matched_occurrences')
        source_snapshot = reconciliation.get('previous_snapshot_id')
        if (reconciliation.get('status') not in ('append', 'unchanged')
                or type(source_snapshot) is not int or source_snapshot <= 0
                or not isinstance(matches, dict) or not matches):
            return result
        row = self.db.execute('''SELECT p.payloads_json FROM snapshots s
            JOIN snapshot_payloads p ON p.snapshot_id=s.id
            WHERE s.id=? AND s.conversation_id=? AND s.status IN ('baseline','append','unchanged')''',
            (source_snapshot, conversation)).fetchone()
        if not row:
            return result
        try:
            # Use the same original-frame upgrade as Store reconciliation.
            # Legacy source observations need no new frame field; only their
            # timestamp pixels are upgraded, after bubble/avatar SHA checks.
            loader = getattr(self.store, 'verified_snapshot_payloads', None)
            previous = loader(source_snapshot) if callable(loader) else json.loads(row[0])
        except (OSError, ValueError, TypeError):
            return result
        if __package__:
            from .message_vision import message_identity, reconcile_visual
        else:
            from message_vision import message_identity, reconcile_visual
        recovered = reconcile_visual(previous, result)
        recovered_metadata = (recovered or {}).get('metadata') or {}
        if (not recovered or recovered.get('status') != reconciliation.get('status')
                or recovered_metadata.get('matched_occurrences') != matches
                or recovered.get('overlap') != reconciliation.get('overlap')):
            return result
        indexes = []
        for current_index, previous_index in matches.items():
            if (not isinstance(current_index, str) or not current_index.isdigit()
                    or type(previous_index) is not int):
                return result
            current_index = int(current_index)
            if not (0 <= current_index < len(result) and 0 <= previous_index < len(previous)
                    and current_index < reconciliation['overlap']):
                return result
            indexes.append((current_index, previous_index))
        if len({old for _, old in indexes}) != len(indexes):
            return result
        for current_index, previous_index in indexes:
            item, original = result[current_index], previous[previous_index]
            current_sender = visual_sender(conversation, item, title)
            previous_sender = visual_sender(conversation, original, title)
            if (not current_sender or current_sender != previous_sender
                    or message_identity(item) != message_identity(original)
                    or not message_identity(item)):
                continue
            if item['direction'] == 'incoming' and not (
                    item.get('group_sender_verified') is True and original.get('group_sender_verified') is True
                    and item.get('group_sender_name') == original.get('group_sender_name')):
                continue
            visual, old_visual = item['visual'], original['visual']
            if any(v.get('avatar_source') != 'same_frame_message_avatar'
                   or v.get('avatar_color_order', 'RGB') != 'RGB'
                   or not isinstance(v.get('avatar_box'), list) or len(v['avatar_box']) != 4
                   for v in (visual, old_visual)):
                continue
            if visual['avatar_box'][2:] != old_visual['avatar_box'][2:]:
                continue
            frame_sha = item.get('sender_observation_frame_sha256')
            observation = item.get('sender_observation_key')
            source_observation = original.get('sender_observation_key')
            if (not isinstance(frame_sha, str) or not re.fullmatch(r'[a-f0-9]{64}', frame_sha)
                    or not isinstance(observation, str) or not re.fullmatch(r'[a-f0-9]{64}', observation)
                    or not isinstance(source_observation, str) or not re.fullmatch(r'[a-f0-9]{64}', source_observation)):
                continue
            expected = hashlib.sha256(json.dumps([frame_sha, current_sender['key'],
                visual.get('box'), visual.get('avatar_box')], sort_keys=True).encode()).hexdigest()
            if observation != expected:
                continue
            proof_row = self.db.execute('''SELECT display_name,wechat_id,proof_json,verified_at
                FROM message_sender_observations WHERE observation_key=? AND identity_key=?''',
                (source_observation, current_sender['key'])).fetchone()
            if not proof_row:
                continue
            try:
                proof = json.loads(proof_row[2])
            except (ValueError, TypeError):
                continue
            if (proof.get('stable') is not True
                    or proof.get('message_avatar_sha256') != current_sender['avatar']
                    or proof.get('wechat_id') != proof_row[1]
                    or not re.fullmatch(r'[A-Za-z0-9_-]{3,128}', str(proof_row[1]))):
                continue
            existing = self.db.execute('SELECT wechat_id FROM message_sender_observations WHERE observation_key=?',
                                       (observation,)).fetchone()
            if existing and existing[0] != proof_row[1]:
                continue
            previous_transfer = proof.get('occurrence_transfer') or {}
            proof = dict(proof, occurrence_transfer={
                'strategy': recovered_metadata.get('strategy'),
                'source_snapshot_id': source_snapshot, 'source_ordinal': previous_index,
                'source_observation_key': source_observation,
                'profile_observation_key': previous_transfer.get('profile_observation_key', source_observation),
                'current_ordinal': current_index, 'current_observation_key': observation,
                'current_frame_sha256': frame_sha, 'bubble_identity': list(message_identity(item)),
                'bubble_box': visual.get('box'), 'avatar_box': visual['avatar_box'],
                'avatar_sha256': current_sender['avatar'],
            })
            self.db.execute('''INSERT OR IGNORE INTO message_sender_observations
                VALUES(?,?,?,?,?,?)''', (observation, current_sender['key'], proof_row[0], proof_row[1],
                json.dumps(proof, ensure_ascii=False), proof_row[3]))
        self.db.commit()
        return self.enrich(conversation, result, title)

    def resolve_visible(self, runtime, image, conversation, payloads, target, label, limit=2):
        """Read new visible members' profiles through the one existing worker."""
        attempted=set();count=0
        frame_sha=hashlib.sha256(np.ascontiguousarray(image[82:887,490:1885,:3]).tobytes()).hexdigest()
        enriched=self.enrich(conversation,payloads,target['name'],frame_sha)
        for payload in reversed(enriched):
            sender=visual_sender(conversation,payload,target['name'])
            if (not sender or payload.get('sender_observation_key') in attempted or payload.get('sender_identity_verified')
                    or (payload.get('direction')=='incoming' and not payload.get('reply_safe'))):
                continue
            attempted.add(payload['sender_observation_key'])
            previous=self.db.execute('SELECT attempted FROM message_sender_attempts WHERE identity_key=?',
                                     (payload['sender_observation_key'],)).fetchone()
            if previous and time.time()-previous[0]<300:continue
            reason='profile_verified';count+=1
            try:
                from sender_identity import verify_member
                proof=verify_member(runtime,image,label+'-'+sender['key'][:10],target,payload)
                self.remember(conversation,payload,proof,target['name'])
            except Exception as exc:
                reason=str(exc)[:160]
                self.store.event('message_sender_identity_review',{'conversation':conversation,
                    'sender_identity_key':sender['key'],'reason':reason})
                if reason in ('SENDER_EXISTING_POPUP','SENDER_PROFILE_NOT_RESTORED','SENDER_PROFILE_NOT_DISMISSED','SENDER_CHAT_NOT_RESTORED',
                              'MESSAGE_MOVED_DURING_PROFILE_CHECK','SENDER_PROFILE_WINDOW_NOT_UNIQUE'):
                    self.db.execute('INSERT INTO message_sender_attempts VALUES(?,?,?) '
                        'ON CONFLICT(identity_key) DO UPDATE SET attempted=excluded.attempted,reason=excluded.reason',
                        (payload['sender_observation_key'],time.time(),reason));self.db.commit()
                    raise
            self.db.execute('INSERT INTO message_sender_attempts VALUES(?,?,?) '
                'ON CONFLICT(identity_key) DO UPDATE SET attempted=excluded.attempted,reason=excluded.reason',
                (payload['sender_observation_key'],time.time(),reason));self.db.commit()
            if count>=limit:break
        return self.enrich(conversation,enriched,target['name'])
