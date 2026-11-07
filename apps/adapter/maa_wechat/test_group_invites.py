import json,sqlite3,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from group_invites import policy,card_candidates,page_state,initialize,accept_page,process_card,joined_evidence
from unittest.mock import patch

class InvitationTests(unittest.TestCase):
    def hit(self,text,x,y,w=100,h=22,score=.99):return {'text':text,'box':[x,y,w,h],'score':score}
    def test_default_enabled_and_explicit_disable(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir()
            self.assertIs(policy(root)['auto_accept_group_invites'],True)
            (root/'data'/'group-invite-policy.json').write_text(json.dumps({'auto_accept_group_invites':False}),encoding='utf-8-sig')
            self.assertIs(policy(root)['auto_accept_group_invites'],False)
    def test_invalid_policy_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir();(root/'data'/'group-invite-policy.json').write_text('{"auto_accept_group_invites":"false"}')
            with self.assertRaises(ValueError):policy(root)
    def test_only_complete_incoming_card(self):
        valid=self.hit('邀请你加入群聊',560,786)
        self.assertEqual(card_candidates([valid]),[valid])
        for hit in (self.hit('邀请你加入群聊',1700,786),self.hit('邀请你加入群聊',560,830),self.hit('加入群聊',560,786),self.hit('邀请你加入群聊',560,786,score=.7)):
            self.assertEqual(card_candidates([hit]),[])
    def test_page_requires_group_title_and_exact_join_button(self):
        hits=[self.hit('创作平台AI-内测001(53)',177,181),self.hit('加入群聊',256,669)]
        self.assertEqual(page_state(hits)['title'],'创作平台AI-内测001(53)')
        for values in (hits[:1],hits[1:],hits+[hits[1]],[hits[0],self.hit('进入群聊',256,669)],[self.hit('普通网页',177,181),hits[1]]):
            self.assertIsNone(page_state(values))
    def test_join_not_replayed_after_restart_or_duplicate_call(self):
        db=sqlite3.connect(':memory:');store=SimpleNamespace(db=db);initialize(store)
        for status in ('opened','join_started','accepted','blocked','uncertain'):
            db.execute('INSERT INTO group_invites VALUES(?,?,?,?)',(status,status,'{}',1))
        initialize(store)
        for key in ('opened','join_started','accepted','blocked','uncertain'):
            result=accept_page(None,store,key,'duplicate',None)
            self.assertEqual(result['join_clicks'],0);self.assertTrue(result['duplicate'])
        self.assertEqual(db.execute("SELECT status FROM group_invites WHERE id='join_started'").fetchone()[0],'uncertain')
    def test_disabled_policy_never_touches_desktop(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir();(root/'data'/'group-invite-policy.json').write_text('{"auto_accept_group_invites":false}')
            self.assertFalse(process_card(None,root,None,None,None,None,None,None))
    def test_joined_evidence_requires_two_correct_names_and_stable_header(self):
        header=self.hit('创作平台AI-内测001 (54）',496,48)
        def recognize(node,image):
            return {'ReadChatTitle':[header],'ContactSelectedRow':[self.hit('',61,81,9,65)],'InviteSidebarBlueBadge':[self.hit('',226,98,4,8)]}[node]
        runtime=SimpleNamespace(recognize_image=recognize)
        with patch('send._ocr_scaled',side_effect=[[self.hit('创作平台AI-内测001(54)',496,48)],[self.hit('创作平台AI-内测001',119,97)],[self.hit('普界AI-内测001',119,97)],[self.hit('创作平台AI-内测001',119,97)]]):
            self.assertTrue(joined_evidence(runtime,None,'创作平台AI-内测001'))
        with patch('send._ocr_scaled',side_effect=[[header]]+[[self.hit('普界AI-内测001',119,97)]]*4):
            self.assertFalse(joined_evidence(runtime,None,'创作平台AI-内测001'))

    def test_pipeline_join_is_one_click(self):
        d=json.loads((Path(__file__).parent/'resource'/'pipeline'/'navigation.json').read_text(encoding='utf-8'))
        for name in ('AcceptGroupInvite','OpenGroupInviteCard'):
            self.assertEqual(d[name]['repeat'],1);self.assertEqual(d[name]['next'],[]);self.assertEqual(d[name]['on_error'],[])
        self.assertEqual(d['AcceptGroupInvite']['expected'],['^加入群聊$'])

if __name__=='__main__':unittest.main()
