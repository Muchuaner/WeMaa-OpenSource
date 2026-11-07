import json
import tempfile
import unittest
import uuid
from copy import deepcopy
from pathlib import Path

from operator_media import permitted
from store import Store


class OperatorMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'wechat.sqlite3')
        self.cid = 'b' * 64
        scan = self.store.begin_scan(1)
        self.store.contact(scan, 0, 'same name', 'exact_id', 'photo', '',
                           {'scope': 'personal', 'list_avatar_sha': 'photo'}, '', [], [])
        self.store.db.execute('INSERT INTO contact_chat_links VALUES(?,?,?)',
                              ('wx:exact_id', self.cid, 1))
        self.store.db.commit()
        self.item = dict(id='a' * 32, conversation_id=self.cid, asset_id=11,
                         sha256='f' * 64, created_at=1000)
        self.item['operator_consent'] = dict(kind='single_media', request_id=str(uuid.uuid4()),
            delivery_id=self.item['id'], conversation_id=self.cid, asset_id=11,
            sha256='f' * 64, wechat_id='exact_id', expires_at=1600)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_exact_permission_is_read_only(self):
        self.store.db.execute('PRAGMA query_only=ON')
        self.assertTrue(permitted(self.store.db, self.item, now=1100))

    def test_absent_expired_or_changed_permission_never_allows_delivery(self):
        for key, value in [('kind', 'automatic'), ('request_id', 'bad'),
                ('delivery_id', 'c' * 32), ('conversation_id', 'c' * 64),
                ('asset_id', 12), ('sha256', 'e' * 64), ('wechat_id', 'other_id'),
                ('expires_at', 1100), ('expires_at', 1601), ('expires_at', float('nan'))]:
            with self.subTest(key=key, value=value):
                item = deepcopy(self.item)
                item['operator_consent'][key] = value
                self.assertFalse(permitted(self.store.db, item, now=1100))
        item = dict(self.item); item.pop('operator_consent')
        self.assertFalse(permitted(self.store.db, item, now=1100))

    def test_changed_local_contact_link_rejects_previously_valid_permission(self):
        self.store.db.execute('UPDATE contact_chat_links SET conversation_id=?', ('c' * 64,))
        self.assertFalse(permitted(self.store.db, self.item, now=1100))
