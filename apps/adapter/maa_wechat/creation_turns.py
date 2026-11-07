"""创作指令与普通回复共用持久会话队列；HTTP 接收成功不能放行。"""
import hashlib
import json
import re
import time

from reply_serial import ReplySerial


class CreationTurns:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.serial = ReplySerial(self.db)
        self.db.execute('CREATE TABLE IF NOT EXISTS creation_events '
                        '(id TEXT PRIMARY KEY,body TEXT,status TEXT,updated REAL)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS creation_turns (
            event_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
            observed REAL NOT NULL, request_sha TEXT NOT NULL, state TEXT NOT NULL,
            result_json TEXT NOT NULL, updated REAL NOT NULL)''')
        self.db.commit()
        # 回合终态与会话游标分开提交时若进程退出，以已保存的核验终态恢复游标。
        for event,state in self.db.execute("SELECT t.event_id,t.state FROM creation_turns t "
                                            "JOIN reply_serial s USING(event_id) WHERE "
                                            "t.state IN ('sent','no_reply','blocked') AND "
                                            "s.state IN ('waiting','active','blocked') AND s.state!=t.state").fetchall():
            self.serial.finish(event,'uncertain' if state=='blocked' else state)

    def item(self, event):
        row = self.db.execute('SELECT conversation_id,observed,request_sha,state,result_json '
                              'FROM creation_turns WHERE event_id=?', (event,)).fetchone()
        if row:
            return dict(zip(('conversation_id', 'observed', 'request_sha', 'state', 'result_json'), row))

    def remember(self, body):
        event, cid = body['event_id'], body['conversation_id']
        sha = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                        separators=(',', ':')).encode()).hexdigest()
        existing = self.item(event)
        if existing and (existing['conversation_id'] != cid or existing['request_sha'] != sha):
            raise ValueError('CREATION_TURN_CONTENT_CHANGED')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO creation_events VALUES(?,?,?,?)',
                            (event, json.dumps(body, ensure_ascii=False), 'pending', time.time()))
            self.db.execute('INSERT OR IGNORE INTO creation_turns VALUES(?,?,?,?,?,?,?)',
                            (event, cid, body['observed_at'], sha, 'waiting', '{}', time.time()))
            self.serial.remember(event, cid, commit=False)

    def begin(self, event):
        row = self.item(event)
        if not row or row['state'] not in ('waiting', 'active'):
            return False
        if not self.serial.acquire(event):
            return False
        # 延迟新命令只可携带已保存的前驱；后台必须独立核验前驱送达与原时间。
        if row['state'] == 'waiting' and time.time() - row['observed'] > 300:
            parent=self.serial.details(event)[1]
            if not parent or time.time()-row['observed']>86400:
                self.block(event, 'CREATION_ORIGINAL_RECEIVE_WINDOW_EXPIRED')
                return False
        self._set(event, 'active', json.loads(row['result_json']))
        return True

    def _set(self, event, state, result):
        self.db.execute('UPDATE creation_turns SET state=?,result_json=?,updated=? WHERE event_id=?',
                        (state, json.dumps(result, ensure_ascii=False), time.time(), event))
        self.db.commit()

    def block(self, event, reason):
        self._set(event, 'blocked', {'reason': reason})
        self.serial.finish(event, 'uncertain')
        self.store.event('creation_turn_blocked', {'event_id': event, 'reason': reason})

    def accepted(self, event, result):
        row = self.item(event)
        if not row or row['state'] != 'active':
            raise ValueError('CREATION_TURN_NOT_ACTIVE')
        if result.get('handled') is False:
            self._set(event, 'no_reply', {'accepted': result})
            self.serial.finish(event, 'no_reply')
            return 'no_reply'
        if result.get('handled') is not True or result.get('event_id') != event:
            raise ValueError('CREATION_TURN_RESPONSE_MISMATCH')
        self._set(event, 'awaiting_delivery', {'accepted': result})
        return 'awaiting_delivery'

    def awaiting(self):
        return self.db.execute("SELECT event_id,conversation_id FROM creation_turns "
                               "WHERE state='awaiting_delivery' ORDER BY updated,event_id").fetchall()

    def live(self):
        return self.db.execute("SELECT 1 FROM creation_turns WHERE state IN "
                               "('active','awaiting_delivery') LIMIT 1").fetchone() is not None

    def observe(self, event, result):
        row = self.item(event)
        if not row or row['state'] != 'awaiting_delivery':
            return row['state'] if row else None
        if (result.get('protocol') != 'creation_receipts_v1' or result.get('event_id') != event
                or result.get('conversation_id') != row['conversation_id']
                or result.get('observed_at') != row['observed']):
            raise ValueError('CREATION_TURN_RECEIPT_MISMATCH')
        state = result.get('status')
        deliveries = result.get('deliveries')
        if not isinstance(deliveries, list) or state not in ('waiting', 'sent', 'uncertain'):
            raise ValueError('CREATION_TURN_RECEIPT_INVALID')
        if any(not isinstance(d,dict) or not re.fullmatch('[a-f0-9]{32}',str(d.get('id','')))
               or type(d.get('verified_sent')) is not bool
               or d['verified_sent'] and d.get('status')!='sent' for d in deliveries):
            raise ValueError('CREATION_TURN_RECEIPT_INVALID')
        if state == 'uncertain':
            self.block(event, result.get('reason', 'CREATION_IMMEDIATE_OUTCOME_UNKNOWN'))
            return 'blocked'
        if state == 'sent' and (not deliveries or not all(
                d.get('status') == 'sent' and d.get('verified_sent') is True for d in deliveries)):
            raise ValueError('CREATION_TURN_SENT_WITHOUT_PROOF')
        evidence = json.loads(row['result_json'])
        evidence['receipt'] = result
        self._set(event, 'sent' if state == 'sent' else 'awaiting_delivery', evidence)
        if state == 'sent':
            self.serial.finish(event, 'sent')
            self.store.event('creation_turn_sent', {'event_id': event,
                              'delivery_ids': [d['id'] for d in deliveries]})
        return 'sent' if state == 'sent' else 'awaiting_delivery'

    def permits_delivery(self, delivery_id, conversation_id):
        # 同一回合有多项即时交付时也保持服务端原顺序；下载完成先后不是顺序。
        if self.db.execute("SELECT 1 FROM creation_turns WHERE conversation_id=? "
                           "AND state IN ('active','blocked') LIMIT 1", (conversation_id,)).fetchone():
            return False
        for event, cid in self.awaiting():
            if cid != conversation_id:
                continue
            evidence = json.loads(self.item(event)['result_json']).get('receipt')
            if not evidence:
                return False
            items = evidence['deliveries']
            ids = [d['id'] for d in items]
            if delivery_id not in ids:
                continue
            return all(d['verified_sent'] is True for d in items[:ids.index(delivery_id)])
        return True
