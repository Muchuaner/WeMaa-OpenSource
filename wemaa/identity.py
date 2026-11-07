"""持久身份与现场路由分离；从不使用昵称合并好友。"""
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

import json
import re
import sqlite3
import time
import uuid
import threading

from wemaa.config import settings

WX_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_WRITER_LOCKS: dict[str, threading.RLock] = {}
_WRITER_LOCKS_GUARD = threading.Lock()


class IdentityRegistry:
    def __init__(self, path: Path | str | None = None, account: str | None = None):
        self.path = Path(path or settings.WEMAA_DATA_DIR / "identities.sqlite3")
        self.account = account or settings.WEMAA_ACCOUNT_ID
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _WRITER_LOCKS_GUARD:
            self._writer_lock = _WRITER_LOCKS.setdefault(str(self.path.resolve()), threading.RLock())
        with self.connection(write=True) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript("""
                CREATE TABLE IF NOT EXISTS identities (
                    id TEXT PRIMARY KEY, account TEXT NOT NULL, wechat_id TEXT NOT NULL,
                    contact_id TEXT NOT NULL, name TEXT NOT NULL, avatar TEXT,
                    status TEXT NOT NULL, seen REAL NOT NULL, person_id TEXT, session_id TEXT,
                    UNIQUE(account, wechat_id)
                );
                CREATE TABLE IF NOT EXISTS routes (
                    account TEXT NOT NULL, conversation_id TEXT NOT NULL,
                    identity_id TEXT NOT NULL REFERENCES identities(id), proof TEXT NOT NULL,
                    verified_at REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    canonical_id TEXT,
                    PRIMARY KEY(account, conversation_id)
                );
                CREATE TABLE IF NOT EXISTS issues (
                    account TEXT NOT NULL, contact_id TEXT NOT NULL, reason TEXT NOT NULL,
                    seen REAL NOT NULL, PRIMARY KEY(account, contact_id)
                );
            """)
            if 'canonical_id' not in {r[1] for r in db.execute('PRAGMA table_info(routes)')}:
                db.execute('ALTER TABLE routes ADD COLUMN canonical_id TEXT')

    @contextmanager
    def connection(self, *, write=False):
        # 同一路径的身份注册、遥测同步和资料绑定只允许一个写事务；
        # WAL 让独立只读查询不必等全量遥测写入完成。
        with self._writer_lock if write else nullcontext():
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            try:
                with db:
                    yield db
            finally:
                db.close()

    def user_id(self, wechat_id: str) -> str:
        if not WX_ID.fullmatch(wechat_id):
            raise ValueError("INVALID_VERIFIED_WECHAT_ID")
        return "wx_" + uuid.uuid5(uuid.NAMESPACE_URL, f"wemaa:{self.account}:{wechat_id}").hex

    def sync(self, body: dict[str, Any]) -> dict[str, int]:
        now = time.time()
        known: dict[str, str] = {}
        with self.connection(write=True) as db:
            # 路由只由本次遥测的独立资料绑定重新激活，旧展示记录不代表可发送。
            db.execute("UPDATE routes SET active=0 WHERE account=?", (self.account,))
            for row in body.get("contacts", []):
                if row.get("scope") != "personal":
                    continue
                cid, wx = str(row.get("id", "")), str(row.get("wechat_id") or "")
                if (not WX_ID.fullmatch(wx) or row.get("identity_status") != "ocr_wechat_id"
                        or cid != "wx:" + wx or not row.get("profile_asset")):
                    db.execute("INSERT OR REPLACE INTO issues VALUES(?,?,?,?)",
                               (self.account, cid, "PROFILE_ID_NOT_VERIFIED", now))
                    continue
                uid = self.user_id(wx)
                known[cid] = uid
                db.execute("""INSERT INTO identities
                    (id,account,wechat_id,contact_id,name,avatar,status,seen)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,avatar=excluded.avatar,seen=excluded.seen,status=excluded.status""",
                    (uid, self.account, wx, cid, row.get("display_name") or row.get("list_name") or wx,
                     row.get("avatar_asset"), "profile_observed", now))
                db.execute("DELETE FROM issues WHERE account=? AND contact_id=?", (self.account, cid))
            for row in body.get("conversations", []):
                proof = row.get("private_contact") or {}
                cid, uid = str(row.get("id", "")), known.get(proof.get("contact_id"))
                if (not uid or proof.get("profile_verified") is not True or row.get("group_profile")
                        or row.get("group_profile_status") or proof.get("wechat_id") is None
                        or not WX_ID.fullmatch(str(proof["wechat_id"]))
                        or self.user_id(str(proof["wechat_id"])) != uid):
                    continue
                for route in [cid, *row.get("aliases", [])]:
                    old = db.execute("SELECT identity_id FROM routes WHERE account=? AND conversation_id=?",
                                     (self.account, route)).fetchone()
                    if old and old[0] != uid:
                        db.execute("INSERT OR REPLACE INTO issues VALUES(?,?,?,?)",
                                   (self.account, proof["contact_id"], "ROUTE_IDENTITY_CONFLICT", now))
                        continue
                    db.execute("""INSERT INTO routes VALUES(?,?,?,?,?,1,?)
                        ON CONFLICT(account,conversation_id) DO UPDATE SET
                        proof=excluded.proof,verified_at=excluded.verified_at,active=1,
                        canonical_id=excluded.canonical_id""",
                        (self.account, route, uid, json.dumps(proof, ensure_ascii=False), now, cid))
        return self.summary()

    def resolve(self, conversation_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("""SELECT i.*,r.conversation_id,r.verified_at FROM identities i
                JOIN routes r ON r.identity_id=i.id WHERE r.account=? AND r.conversation_id=?
                AND r.active=1""", (self.account, conversation_id)).fetchone()
            if not row:
                raise ValueError("PRIVATE_IDENTITY_NOT_VERIFIED")
            return dict(row)

    def route_for(self, user_id: str) -> str:
        with self.connection() as db:
            row = db.execute("""SELECT COALESCE(canonical_id,conversation_id) FROM routes WHERE account=? AND identity_id=?
                AND active=1 ORDER BY verified_at DESC,conversation_id LIMIT 1""",
                (self.account, user_id)).fetchone()
            if not row:
                raise ValueError("PRIVATE_ROUTE_NOT_VERIFIED")
            return row[0]

    def identity_for(self, user_id: str) -> dict:
        """按当前微信账号读取持久身份，不依赖是否已经打开过聊天。"""
        with self.connection() as db:
            row = db.execute('SELECT * FROM identities WHERE account=? AND id=?',
                             (self.account, user_id)).fetchone()
            if not row or row['contact_id'] != 'wx:' + row['wechat_id']:
                raise ValueError('PRIVATE_IDENTITY_NOT_VERIFIED')
            return dict(row)

    def register_sender(self, proof: dict, conversation_id: str, *, private: bool) -> dict:
        """消息头像对应的资料页核验可补全好友路由，群成员使用相同微信身份。"""
        uid = self.user_id(proof['wechat_id'])
        now = time.time()
        with self.connection(write=True) as db:
            db.execute('''INSERT INTO identities
                (id,account,wechat_id,contact_id,name,status,seen) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET seen=excluded.seen''',
                (uid,self.account,proof['wechat_id'],'wx:'+proof['wechat_id'],
                 proof['display_name'],'message_profile_verified',now))
            if private:
                old = db.execute('SELECT identity_id FROM routes WHERE account=? AND conversation_id=?',
                                 (self.account,conversation_id)).fetchone()
                if old and old[0] != uid:
                    raise ValueError('ROUTE_IDENTITY_CONFLICT')
                db.execute('''INSERT INTO routes VALUES(?,?,?,?,?,1,?)
                    ON CONFLICT(account,conversation_id) DO UPDATE SET proof=excluded.proof,
                    verified_at=excluded.verified_at,active=1,
                    canonical_id=COALESCE(routes.canonical_id,excluded.canonical_id)''',
                    (self.account,conversation_id,uid,json.dumps(proof),now,conversation_id))
            return dict(db.execute('SELECT * FROM identities WHERE id=?',(uid,)).fetchone())

    def items(self) -> list[dict]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM identities WHERE account=? ORDER BY name,id", (self.account,))]

    def summary(self) -> dict[str, int]:
        with self.connection() as db:
            return {
                "identified": db.execute("SELECT count(*) FROM identities WHERE account=?", (self.account,)).fetchone()[0],
                "registered": db.execute("SELECT count(*) FROM identities WHERE account=? AND person_id IS NOT NULL", (self.account,)).fetchone()[0],
                "routable": db.execute("SELECT count(DISTINCT identity_id) FROM routes WHERE account=? AND active=1", (self.account,)).fetchone()[0],
                "needs_review": db.execute("SELECT count(*) FROM issues WHERE account=?", (self.account,)).fetchone()[0],
            }

    def bind_core(self, identity_id: str, person_id: str, session_id: str) -> None:
        with self.connection(write=True) as db:
            db.execute("UPDATE identities SET person_id=?,session_id=? WHERE id=? AND account=?",
                       (person_id, session_id, identity_id, self.account))
