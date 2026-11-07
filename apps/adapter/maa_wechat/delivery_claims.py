"""许可响应丢失只允许重取同一许可；进入 GUI 后永不自动重放。"""
import time
import uuid

class DeliveryClaims:
    def __init__(self, store):
        self.db=store.db
        self.db.execute('CREATE TABLE IF NOT EXISTS delivery_claims (delivery_id TEXT PRIMARY KEY, token TEXT NOT NULL, phase TEXT NOT NULL, updated REAL NOT NULL)')
        self.db.commit()

    def token(self, did):
        self.db.execute('INSERT OR IGNORE INTO delivery_claims VALUES(?,?,?,?)',(did,uuid.uuid4().hex,'claiming',time.time()))
        self.db.commit()
        token,phase=self.db.execute('SELECT token,phase FROM delivery_claims WHERE delivery_id=?',(did,)).fetchone()
        if phase!='claiming':raise ValueError('DELIVERY_GUI_ALREADY_STARTED_REVIEW_REQUIRED')
        return token

    def begin_gui(self, did):
        changed=self.db.execute("UPDATE delivery_claims SET phase='gui_started',updated=? WHERE delivery_id=? AND phase='claiming'",(time.time(),did)).rowcount
        self.db.commit()
        if changed!=1:raise ValueError('DELIVERY_GUI_ALREADY_STARTED_REVIEW_REQUIRED')
