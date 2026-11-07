"""持久保存出站尝试；不确定的发送只允许由真正的新来信解除阻断。"""
from pathlib import Path

import json
import sqlite3
import time


class DeliveryGuard:
    def __init__(self, path: Path, account: str):
        self.path, self.account = path, account
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY, account TEXT NOT NULL, session_id TEXT NOT NULL,
                source_event TEXT, source_time REAL, message_id TEXT NOT NULL,
                state TEXT NOT NULL, reason TEXT, command_ids TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS attempt_session ON attempts(account,session_id,id)')

    def reserve(self, session_id: str, message_id: str, source: dict | None) -> int:
        """在入队之前提交；进程中断也不能让规划器换一个输出 ID 后重发。"""
        event, observed = (source['event_id'], source['observed_at']) if source else (None, None)
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('''SELECT source_event,source_time,state FROM attempts
                WHERE account=? AND session_id=? ORDER BY id DESC LIMIT 1''',
                (self.account, session_id)).fetchone()
            if (previous and previous[2] == 'blocked' and event is not None
                    and event == previous[0]):
                raise RuntimeError('PREVIOUS_DELIVERY_FAILED_WAIT_FOR_NEW_INBOUND')
            if previous and previous[2] in {'pending', 'uncertain'}:
                # 更晚的已核验原始来信可启动新的交谈；同一事件、旧来信、
                # 无来信的主动输出都不能解除之前的不确定发送。
                if (event is None or observed is None or event == previous[0]
                        or previous[1] is not None and observed <= previous[1]):
                    raise RuntimeError('PREVIOUS_DELIVERY_UNCERTAIN_WAIT_FOR_NEW_INBOUND')
            now = time.time()
            row = db.execute('''INSERT INTO attempts
                (account,session_id,source_event,source_time,message_id,state,command_ids,created_at,updated_at)
                VALUES(?,?,?,?,?,'pending','[]',?,?)''',
                (self.account, session_id, event, observed, message_id, now, now))
            return row.lastrowid

    def finish(self, attempt: int, state: str, reason: str, command_ids: list[str]) -> None:
        if state not in {'verified', 'blocked', 'uncertain'}:
            raise ValueError('DELIVERY_GUARD_STATE_INVALID')
        with sqlite3.connect(self.path, timeout=10) as db:
            changed = db.execute('''UPDATE attempts SET state=?,reason=?,command_ids=?,updated_at=?
                WHERE id=? AND account=? AND state='pending' ''',
                (state, reason, json.dumps(command_ids), time.time(), attempt, self.account)).rowcount
            if changed != 1:
                raise RuntimeError('DELIVERY_GUARD_ATTEMPT_ALREADY_TERMINAL')
