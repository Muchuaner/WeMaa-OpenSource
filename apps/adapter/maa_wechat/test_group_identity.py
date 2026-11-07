import tempfile,unittest,json
from pathlib import Path
from store import Store,digest

class GroupIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'state.sqlite')
    def tearDown(self):self.store.db.close();self.tmp.cleanup()
    def observe(self,name,avatar,members=()):
        payloads=[{'group_sender_verified':True,'group_sender_name':m} for m in members]
        cid=self.store.group_identity(name,name+'(54)',avatar,payloads)
        self.store.manual_view(cid,name+'(54)',avatar,'frame.png',payloads)
        self.store.observe_group(cid,name,name+'(54)')
        return cid
    def test_avatar_and_count_changes_keep_same_group(self):
        cid=self.observe('群一','a')
        self.assertEqual(cid,self.observe('群一','b'))
        self.assertEqual(cid,self.store.group_identity('群一','群一(55)','c'))
    def test_five_members_allow_rename_four_do_not(self):
        cid=self.observe('旧名','a',['甲','乙','丙','丁','戊'])
        other=self.observe('别的群','b',['甲','乙','丙','丁'])
        self.assertNotEqual(cid,other)
        self.assertEqual(cid,self.observe('新名','c',['甲','乙','丙','丁','戊']))
    def test_unverified_speakers_do_not_count_and_private_unchanged(self):
        cid=self.observe('群','a',['甲','乙','丙','丁','戊'])
        p=[{'group_sender_name':m} for m in ['甲','乙','丙','丁','戊']]
        self.assertNotEqual(cid,self.store.group_identity('别群','别群(54)','b',p))
        self.assertEqual(digest(['Demo User','photo']),self.store.group_identity('Demo User','Demo User','photo'))
    def test_legacy_alias_preserves_history_and_latest_route(self):
        for avatar,seen in [('a',1),('b',2)]:
            cid=digest(['群',avatar]);self.store.manual_view(cid,'群(54)',avatar,'frame.png',[])
            self.store.observe_group(cid,'群','群(54)');self.store.route(cid,{'name':'群','avatar_sha':avatar})
            self.store.db.execute('UPDATE conversations SET last_seen=? WHERE id=?',(seen,cid));self.store.db.commit()
        self.store.reconcile_groups()
        canonical=self.store.canonical(digest(['群','b']))
        self.assertEqual(canonical,digest(['群','a']))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM conversations').fetchone()[0],2)
        route=self.store.db.execute('SELECT target_json FROM conversation_routes WHERE conversation_id=?',(canonical,)).fetchone()
        self.assertEqual(json.loads(route[0])['avatar_sha'],'b')
