"""Durable per-occurrence work, independent of historical context reviews.

The observation cursor locates future input. It is never a handled-message
receipt: each located incoming occurrence has its own persistent work row.
Unknown history establishes an observation only and cannot create permission
to replay it. All desktop work remains with the existing GUI owner.
"""
import json
import time

from message_vision import message_identity
from store import digest

REOBSERVE_IDENTITY_ERRORS = frozenset({'SENDER_PROFILE_ID_NOT_STABLE',
    'SENDER_PROFILE_CLICK_FAILED', 'MESSAGE_MOVED_BEFORE_PROFILE',
    'MESSAGE_AVATAR_NOT_VISIBLE', 'MESSAGE_AVATAR_HASH_MISMATCH'})


class InboundMessages:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS inbound_observations(
                snapshot_id INTEGER PRIMARY KEY, conversation_id TEXT,
                mapping_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbound_observation_heads(
                conversation_id TEXT PRIMARY KEY, snapshot_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS inbound_message_work(
                event_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                source_snapshot_id INTEGER NOT NULL, source_ordinal INTEGER NOT NULL,
                payload_json TEXT NOT NULL, target_json TEXT NOT NULL,
                first_seen REAL NOT NULL, status TEXT NOT NULL, reason TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                updated REAL NOT NULL, UNIQUE(source_snapshot_id,source_ordinal));
            CREATE INDEX IF NOT EXISTS inbound_message_work_due
                ON inbound_message_work(status,next_attempt);
            CREATE INDEX IF NOT EXISTS inbound_message_work_conversation
                ON inbound_message_work(conversation_id,status);
        ''')

    def head(self, conversation):
        row = self.db.execute('SELECT snapshot_id FROM inbound_observation_heads WHERE conversation_id=?',
                              (conversation,)).fetchone()
        return row[0] if row else None

    def mapping(self, snapshot):
        row = self.db.execute('SELECT mapping_json FROM inbound_observations WHERE snapshot_id=?',
                              (snapshot,)).fetchone()
        return json.loads(row[0]) if row else {}

    def record(self, conversation, target, result, payloads, *, enabled, suppress_new=False, now=None):
        """Called inside Store.snapshot's transaction; never commit here."""
        now = time.time() if now is None else now
        mapping = {}
        parent = self.mapping(result.get('previous_snapshot_id'))
        metadata = result.get('metadata') or {}
        for current, previous in metadata.get('matched_occurrences', {}).items():
            event = parent.get(str(previous))
            if event:
                mapping[str(current)] = event
                item=self.item(event)
                original=json.loads(item['payload_json']) if item else {}
                payload=payloads[int(current)]
                if (item and original.get('text_copy',{}).get('verified') is not True
                        and payload.get('text_copy',{}).get('verified') is True
                        and message_identity(original)==message_identity(payload)):
                    # Receiving recovered text is independent of reply permission.
                    # Held/review items retain their original status and first_seen.
                    raw=json.dumps(payload,ensure_ascii=False)
                    self.db.execute('UPDATE inbound_message_work SET payload_json=?,updated=? WHERE event_id=?',
                                    (raw,now,event))
                    self.db.execute('UPDATE messages SET payload_json=? WHERE id=?',(raw,event[:32]))
        new_tail = (result.get('previous_snapshot_id') is not None
                    and metadata.get('new_tail_verbatim') is True
                    and result['status'] in ('append', 'review')
                    and not suppress_new)
        registered = []
        if new_tail:
            for ordinal in range(result['overlap'], len(payloads)):
                payload = payloads[ordinal]
                if payload.get('direction') != 'incoming' or not message_identity(payload):
                    self.db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?)',
                        (digest(['maa-observed-context',conversation,result['snapshot_id'],ordinal])[:32],
                         conversation,result['snapshot_id'],ordinal,json.dumps(payload,ensure_ascii=False),now))
                    continue
                event = digest(['maa-incoming-message-v1', conversation, result['snapshot_id'], ordinal])
                self.db.execute('''INSERT OR IGNORE INTO inbound_message_work
                    (event_id,conversation_id,source_snapshot_id,source_ordinal,payload_json,target_json,
                     first_seen,status,reason,updated) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (event, conversation, result['snapshot_id'], ordinal,
                     json.dumps(payload, ensure_ascii=False), json.dumps(target, ensure_ascii=False),
                     now, 'pending' if enabled else 'held',
                     'verification_pending' if enabled else 'AUTO_REPLY_DISABLED_AT_OBSERVATION', now))
                # Observed messages stay inspectable even if native copy is not yet verified.
                self.db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?)',
                    (event[:32], conversation, result['snapshot_id'], ordinal,
                     json.dumps(payload, ensure_ascii=False), now))
                mapping[str(ordinal)] = event
                registered.append(ordinal)
        # A pending occurrence can scroll outside the observed overlap. Keep
        # its original evidence, but do not let an unlocated item monopolise
        # polling with a due timestamp that never changes.
        for event, first_seen, attempts in self.db.execute('''SELECT event_id,first_seen,attempts
                FROM inbound_message_work WHERE conversation_id=? AND status='pending'
                AND next_attempt<=?''', (conversation,now)).fetchall():
            if event in mapping.values():continue
            expired=now-first_seen>300
            self.db.execute('''UPDATE inbound_message_work SET status=?,reason=?,attempts=?,
                next_attempt=?,updated=? WHERE event_id=?''',
                ('review' if expired else 'pending','MESSAGE_OCCURRENCE_NOT_LOCATED',attempts+1,
                 now+self.retry_delay(attempts+1),now,event))
        self.db.execute('INSERT INTO inbound_observations VALUES(?,?,?)',
                        (result['snapshot_id'], conversation, json.dumps(mapping)))
        # An unresolved gap is evidence to retry, not a new receive baseline.
        # Advancing here would make the next identical frame "unchanged" and
        # permanently swallow incoming bubbles that have no durable work row.
        # Explicit history-only baselines still locate subsequent new appends.
        if suppress_new or result['status'] not in ('gap', 'ambiguous'):
            self.db.execute('''INSERT INTO inbound_observation_heads VALUES(?,?)
                ON CONFLICT(conversation_id) DO UPDATE SET snapshot_id=excluded.snapshot_id''',
                            (conversation, result['snapshot_id']))
        result['message_event_ids'] = mapping
        result['registered_message_ordinals'] = registered

    def item(self, event):
        row = self.db.execute('''SELECT event_id,conversation_id,source_snapshot_id,source_ordinal,
            payload_json,target_json,first_seen,status,reason,attempts,next_attempt
            FROM inbound_message_work WHERE event_id=?''', (event,)).fetchone()
        return dict(zip(('event_id','conversation_id','snapshot_id','ordinal','payload_json',
                         'target_json','first_seen','status','reason','attempts','next_attempt'), row)) if row else None

    def update(self, event, status, reason, *, payload=None, target=None, retry=False, now=None):
        now = time.time() if now is None else now
        item = self.item(event)
        attempts = item['attempts'] + int(retry)
        delay = self.retry_delay(attempts) if retry else 0
        self.db.execute('''UPDATE inbound_message_work SET status=?,reason=?,attempts=?,next_attempt=?,
            payload_json=coalesce(?,payload_json),target_json=coalesce(?,target_json),updated=? WHERE event_id=?''',
            (status, reason, attempts, now + delay,
             json.dumps(payload, ensure_ascii=False) if payload is not None else None,
             json.dumps(target, ensure_ascii=False) if target is not None else None, now, event))
        if payload is not None:
            self.db.execute('UPDATE messages SET payload_json=? WHERE id=?',
                            (json.dumps(payload, ensure_ascii=False), event[:32]))
        self.db.commit()

    @staticmethod
    def retry_delay(attempts):
        return (2, 5, 15, 30, 60)[min(max(attempts - 1, 0), 4)]

    def pending(self, rows, now=None):
        """Only visible routes are scheduling hints, never identity grants."""
        from name_match import names_match
        now = time.time() if now is None else now
        planned = []
        for raw, in self.db.execute('''SELECT target_json FROM inbound_message_work
                WHERE status='pending' AND next_attempt<=? ORDER BY first_seen''', (now,)):
            target = json.loads(raw)
            matches = [r for r in rows if r.get('avatar_sha') == target.get('avatar_sha')
                       and names_match(r.get('name'), target.get('name'))]
            if len(matches) == 1 and not any(r['key'] == matches[0]['key'] for r in planned):
                planned.append(dict(matches[0], schedule={'reason':'message_verification',
                    'priority':1000001, 'weight':1}))
        return planned[:1]

    def health(self):
        counts = dict(self.db.execute('SELECT status,count(*) FROM inbound_message_work GROUP BY status'))
        oldest = self.db.execute("SELECT min(first_seen) FROM inbound_message_work WHERE status='pending'").fetchone()[0]
        reasons = dict(self.db.execute("SELECT reason,count(*) FROM inbound_message_work WHERE status IN ('pending','review') GROUP BY reason"))
        reviews=[dict(event_id=event,snapshot_id=snapshot,ordinal=ordinal,reason=reason)
                 for event,snapshot,ordinal,reason in self.db.execute('''SELECT event_id,source_snapshot_id,
                     source_ordinal,reason FROM inbound_message_work WHERE status='review'
                     ORDER BY updated DESC LIMIT 5''')]
        return {'status_counts':counts, 'reason_counts':reasons,
                'reviews':reviews,
                'oldest_pending_age_seconds':max(0,round(time.time()-oldest)) if oldest is not None else None}

    def process(self, result, payloads, row, title, target, client, creation, experiment,
                runtime, image, run_id, enabled_at):
        """Verify and dispatch independent occurrences; failures do not block peers."""
        from watch import incoming_requests, dispatch_incoming, dispatched_event
        from reply_policy import permits
        now = time.time()
        accepted, rejected = [], []
        for raw_ordinal, event in result.get('message_event_ids', {}).items():
            ordinal = int(raw_ordinal)
            item = self.item(event)
            if not item or item['status'] != 'pending' or item['next_attempt'] > now:
                continue
            if dispatched_event(self.store, event):
                self.update(event, 'queued', 'durable_event_exists')
                accepted.append(ordinal)
                continue
            current = payloads[ordinal]
            original = json.loads(item['payload_json'])
            if message_identity(original) != message_identity(current):
                self.update(event, 'review', 'MESSAGE_OCCURRENCE_CHANGED')
                rejected.append(ordinal)
                continue
            if original.get('text_copy', {}).get('verified') is True:
                current = dict(current, text=original['text'], text_copy=original['text_copy'],
                               ocr=original.get('ocr', {}))
            candidate = dict(result, status='unread_recovery', ordinals=[ordinal], overlap=ordinal,
                             event_ids={ordinal:event}, event_seed=event,
                             source_ordinals={ordinal:item['ordinal']},
                             snapshot_ordinals={ordinal:item['ordinal']})
            body_seen = {'conversation_id':item['conversation_id'], 'observed_at':item['first_seen']}
            if not permits(self.db, body_seen, item['first_seen']):
                self.update(event, 'held', 'AUTO_REPLY_BEFORE_ACTIVATION')
                continue
            values = list(payloads)
            values[ordinal] = current
            requests = incoming_requests(values, candidate, row, title, item['conversation_id'],
                                         now, enabled_at)
            if not requests:
                reason = (current.get('text_copy') or {}).get('reason') or 'SENDER_OR_CONTENT_NOT_VERIFIED'
                if len(current.get('text', '')) > 2000:
                    self.update(event, 'review', 'INBOUND_TEXT_EXCEEDS_REPLY_LIMIT', payload=current, target=target)
                else:
                    self.update(event, 'pending', reason, payload=current, target=target, retry=True)
                rejected.append(ordinal)
                continue
            if now-item['first_seen']>300:
                self.update(event,'review','INBOUND_REPLY_WINDOW_EXPIRED',payload=current,target=target)
                rejected.append(ordinal)
                continue
            self.update(event, 'pending', 'identity_verification', payload=current, target=target)
            dispatch = dispatch_incoming(requests, self.store, client, creation, experiment,
                runtime, image, values, item['snapshot_id'], target, run_id,
                first_seen=item['first_seen'], result=candidate)
            if dispatch['accepted']:
                self.update(event, 'queued', 'dispatch_persisted', payload=values[ordinal], target=target)
                accepted.append(ordinal)
            else:
                reason = dispatch['rejected'][0]['reason'] if dispatch['rejected'] else 'DISPATCH_NOT_PERSISTED'
                retry=reason in REOBSERVE_IDENTITY_ERRORS and not dispatched_event(self.store,event)
                if retry:
                    # No reply/creation journal exists. Retry observation with
                    # the SAME event; never reset a business or physical send.
                    self.db.execute("UPDATE inbound_dispatch SET status='captured' WHERE event_id=? AND status='review' AND reason=?",
                                    (event,reason))
                self.update(event, 'pending' if retry else 'review', reason,
                            payload=values[ordinal], target=target, retry=retry)
                rejected.append(ordinal)
        return {'accepted':accepted, 'rejected':rejected}
