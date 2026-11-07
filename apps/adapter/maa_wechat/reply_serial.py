"""会话内串行上报；本地落盘后才允许请求后台，失败不会自动放行。"""
import time


class ReplySerial:
    def __init__(self, db):
        self.db = db
        db.execute('''CREATE TABLE IF NOT EXISTS reply_serial (
            event_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
            state TEXT NOT NULL, created REAL NOT NULL, started REAL,
            buffered_after TEXT)''')
        db.execute('CREATE INDEX IF NOT EXISTS reply_serial_conversation ON reply_serial(conversation_id,created)')
        db.commit()

    def remember(self, event, conversation, *, commit=True):
        previous = self.db.execute('''SELECT event_id FROM reply_serial
            WHERE conversation_id=? AND state IN ('waiting','active','blocked')
            ORDER BY created DESC,rowid DESC LIMIT 1''', (conversation,)).fetchone()
        self.db.execute('INSERT OR IGNORE INTO reply_serial VALUES(?,?,?, ?,NULL,?)',
                        (event, conversation, 'waiting', time.time(), previous[0] if previous else None))
        if commit:self.db.commit()

    def acquire(self, event):
        """同一会话只放行最早一条；active 包含网络结果丢失和重启。"""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            item = self.db.execute('SELECT conversation_id,state FROM reply_serial WHERE event_id=?', (event,)).fetchone()
            head = self.db.execute('''SELECT event_id,state FROM reply_serial
                WHERE conversation_id=? AND state IN ('waiting','active','blocked')
                ORDER BY created,rowid LIMIT 1''', (item[0],)).fetchone()
            if head != (event, 'waiting') and head != (event, 'active'):
                self.db.commit()
                return False
            self.db.execute("UPDATE reply_serial SET state='active',started=coalesce(started,?) WHERE event_id=?",
                            (time.time(), event))
            self.db.commit()
            return True
        except BaseException:
            self.db.rollback()
            raise

    def details(self, event):
        return self.db.execute('SELECT started,buffered_after FROM reply_serial WHERE event_id=?', (event,)).fetchone()

    def finish(self, event, status):
        if status not in ('sent', 'no_reply', 'failed', 'uncertain'):
            return
        state = status if status in ('sent', 'no_reply') else 'blocked'
        self.db.execute('UPDATE reply_serial SET state=? WHERE event_id=?', (state, event))
        self.db.commit()
