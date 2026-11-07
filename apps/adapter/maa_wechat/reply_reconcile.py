"""Converge uncertain send receipts using fresh observational evidence only.

Reconciliation reopens the conversation through the same verified navigation
as a manual command and looks for the exact outgoing text. A verified match
upgrades the receipt to sent once; absence never downgrades, never resends,
and never fabricates a terminal state — unknown stays unknown until a human
review event is recorded after bounded attempts.
"""
import hashlib
import json
from pathlib import Path
import time

FIRST_DELAY_SECONDS = 180
MAX_ATTEMPTS = 6
NAVIGATION_SECONDS = 120

# Uncertain reasons where a physical Send click may already have happened.
CLICK_REASONS = ('PROCESS_RESTART_DURING_SEND', 'SEND_OUTCOME_UNKNOWN',
                 'SEND_EVIDENCE_INCOMPLETE', 'SENDER_RESULT_INVALID')


class ReplyReconciler:
    def __init__(self, root, store):
        self.root, self.store = Path(root), store

    def due(self, now=None):
        now = time.time() if now is None else now
        for item in self.store.reply_rows(('uncertain',)):
            if not item['reported'] or not item['result_json']:
                continue
            result = json.loads(item['result_json'])
            if result.get('reason') not in CLICK_REASONS:
                continue
            state = self.store.reconcile_state(item['event_id'])
            if state['attempts'] >= MAX_ATTEMPTS or now < state['next_attempt']:
                continue
            if now - item['updated'] < FIRST_DELAY_SECONDS:
                continue
            return item, state
        return None, None

    def process_one(self, runtime, run_id, now=None):
        item, state = self.due(now)
        if item is None:
            return False
        self.attempt(runtime, run_id, item, state)
        return True

    def attempt(self, runtime, run_id, item, state):
        now = time.time()
        event = item['event_id']
        attempts = state['attempts'] + 1
        self.store.reconcile_schedule(event, attempts, now + min(120 * 2 ** (attempts - 1), 3600))
        request = json.loads(item['request_json'])
        response = json.loads(item['response_json'] or '{}')
        target = request.get('_target') or {}
        text = (response.get('reply_text') or '').strip()
        if not text or not target.get('name') or not target.get('avatar_sha'):
            self.finish(event, 'reply_reconcile_skipped', attempts, reason='INCOMPLETE_TARGET')
            return
        if self.ambiguous_duplicates(item, text):
            self.finish(event, 'reply_reconcile_ambiguous_text', attempts,
                        conversation=item['conversation_id'])
            return
        from manual_gui import find_recent
        try:
            row, _page = find_recent(runtime, self.root, target,
                                     run_id + '-reconcile-' + event[:12], now + NAVIGATION_SECONDS)
        except ValueError as exc:
            self.finish(event, 'reply_reconcile_navigation', attempts, reason=str(exc)[:120])
            return
        from watch import read_title, is_group_title
        image, path = runtime.capture(run_id + '-reconcile-chat-' + event[:12])
        try:
            title = read_title(runtime, image, row)
        except ValueError as exc:
            self.finish(event, 'reply_reconcile_navigation', attempts, reason=str(exc)[:120])
            return
        from text_copy import read_messages
        payloads, _hits = read_messages(runtime, image, group_chat=is_group_title(row, title))
        from name_match import normalized
        wanted = normalized(text)
        matches = [p for p in payloads
                   if p.get('direction') == 'outgoing' and normalized(p.get('text')) == wanted]
        if not matches:
            self.finish(event, 'reply_reconcile_absent', attempts, frame=path)
            return

        prior = []
        if item.get('snapshot_id'):
            prior = self.store.verified_snapshot_payloads(item['snapshot_id'])
        if not prior and item.get('result_json'):
            diag = json.loads(item['result_json'] or '{}').get('diagnostic') or {}
            prior = diag.get('history_before') or []
        prior_count = sum(1 for p in prior
                          if p.get('direction') == 'outgoing' and normalized(p.get('text')) == wanted)
        if len(matches) <= prior_count:
            self.finish(event, 'reply_reconcile_absent', attempts, frame=path)
            return

        conversation = self.store.group_identity(row['name'], title, row['avatar_sha'], payloads)
        snapshot = self.store.reconcile_snapshot(conversation, title, row['avatar_sha'], path, payloads)
        frame_sha = hashlib.sha256(image[82:887, 490:1885, :3].copy().tobytes()).hexdigest()
        receipt = {'status': 'sent', 'reason': 'RECONCILED_OUTGOING_OBSERVED',
                   'evidence': {'frame_sha256': frame_sha, 'snapshot_id': snapshot['snapshot_id'],
                                'title_verified': True, 'outgoing_verified': True}}
        self.store.reply_update(event, 'sent', result=receipt)
        self.store.event('reply_reconciled', {'event_id': event, 'conversation': conversation,
                                              'snapshot_id': snapshot['snapshot_id'],
                                              'attempts': attempts})

    def ambiguous_duplicates(self, item, text):
        from name_match import normalized
        wanted = normalized(text)
        for other in self.store.reply_rows(('uncertain',)):
            if other['event_id'] == item['event_id'] or other['conversation_id'] != item['conversation_id']:
                continue
            result = json.loads(other['result_json'] or '{}')
            if result.get('reason') not in CLICK_REASONS:
                continue
            response = json.loads(other['response_json'] or '{}')
            if normalized((response.get('reply_text') or '').strip()) == wanted:
                return True
        return False

    def finish(self, event, kind, attempts, **detail):
        payload = dict(detail, event_id=event, attempts=attempts)
        self.store.event(kind, payload)
        if attempts >= MAX_ATTEMPTS:
            self.store.event('reply_reconcile_review', {'event_id': event, 'attempts': attempts})
