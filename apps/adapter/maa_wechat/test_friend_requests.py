import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import numpy as np
from friend_requests import policy, initialize, dialog_state, pending_rows, record, confirm_dialog, FriendRequests


def hit(text, x, y, w=50, h=20, score=.99):
    return {'text': text, 'box': [x, y, w, h], 'score': score}


def dialog_hits():
    return [hit('通过朋友验证', 130, 16, 95), hit('备注', 31, 63), hit('Ariel', 31, 103),
            hit('朋友权限', 31, 248, 60), hit('确定', 93, 593, 34), hit('取消', 229, 590, 36)]


class FriendRequestTests(unittest.TestCase):
    def setUp(self):
        self.store = SimpleNamespace(db=sqlite3.connect(':memory:'), event=Mock())
        initialize(self.store)

    def dialog(self):
        image=np.zeros((640,360,3),dtype=np.uint8)
        return SimpleNamespace(observe=Mock(return_value=(image,dialog_hits())),
                               click=Mock(),visible=Mock(return_value=False))

    def test_policy_defaults_enabled_and_explicit_false(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertTrue(policy(root)['auto_accept_friend_requests'])
            p=Path(root)/'data';p.mkdir();p.joinpath('friend-request-policy.json').write_text('{"auto_accept_friend_requests":false}')
            self.assertFalse(policy(root)['auto_accept_friend_requests'])
            p.joinpath('friend-request-policy.json').write_text('{"auto_accept_friend_requests":"true"}')
            with self.assertRaises(ValueError):policy(root)

    def test_pending_requires_category_and_correct_column(self):
        base=[hit('新的朋友',75,140,80),hit('公众号、服务号',75,515,125)]
        self.assertEqual(pending_rows(base+[hit('等待验证',423,185,48)]),[195])
        for extra in ([hit('已添加',433,185,38)],[hit('等待验证',1043,185,60)],
                      [hit('等待验证',423,600,48)],[hit('等待验证',423,185,48,score=.7)]):
            self.assertEqual(pending_rows(base+extra),[])
        self.assertEqual(pending_rows([hit('等待验证',423,185,48)]),[])

    def test_dialog_requires_exact_name_title_buttons_regions_and_scores(self):
        self.assertEqual(dialog_state(dialog_hits(),'Ariel'),((110,603),(247,600)))
        for index in range(len(dialog_hits())):
            hits=dialog_hits();hits.pop(index)
            with self.assertRaises(ValueError):dialog_state(hits,'Ariel')
        with self.assertRaises(ValueError):dialog_state(dialog_hits(),'Other')
        with self.assertRaises(ValueError):dialog_state(dialog_hits()+[hit('确定',94,593,34)],'Ariel')
        hits=dialog_hits();hits[4]['score']=.8
        with self.assertRaises(ValueError):dialog_state(hits,'Ariel')

    def test_commit_precedes_single_confirmation(self):
        record(self.store,'a','opened',{})
        dialog=self.dialog()
        def click(point):
            self.assertEqual(self.store.db.execute("SELECT status FROM friend_requests WHERE id='a'").fetchone()[0],'accept_started')
            self.assertFalse(self.store.db.in_transaction)
        dialog.click.side_effect=click
        result=confirm_dialog(self.store,'a','Ariel',dialog,sleep=lambda _:None)
        self.assertEqual(result['status'],'confirmation_closed');dialog.click.assert_called_once()
        self.assertEqual(confirm_dialog(self.store,'a','Ariel',dialog)['confirm_clicks'],0)
        dialog.click.assert_called_once()

    def test_changed_frames_do_not_click(self):
        record(self.store,'a','opened',{})
        dialog=self.dialog();other=np.ones((640,360,3),dtype=np.uint8)
        dialog.observe.side_effect=[dialog.observe.return_value,(other,dialog_hits())]
        with self.assertRaisesRegex(ValueError,'DIALOG_CHANGED'):
            confirm_dialog(self.store,'a','Ariel',dialog,sleep=lambda _:None)
        dialog.click.assert_not_called()

    def test_failed_click_or_no_disappearance_becomes_uncertain_and_never_replays(self):
        for failure in (True,False):
            key=str(failure);record(self.store,key,'opened',{});dialog=self.dialog()
            if failure:dialog.click.side_effect=RuntimeError('click result unknown')
            else:dialog.visible.return_value=True
            self.assertEqual(confirm_dialog(self.store,key,'Ariel',dialog,sleep=lambda _:None)['status'],'uncertain')
            self.assertEqual(confirm_dialog(self.store,key,'Ariel',dialog)['confirm_clicks'],0)
            dialog.click.assert_called_once()

    def test_restart_preserves_terminal_states_and_blocks_interrupted_actions(self):
        for state in ('opened','accept_started','accepted','uncertain','blocked'):
            record(self.store,state,state,{})
        initialize(self.store)
        for key in ('opened','accept_started','accepted','uncertain','blocked'):
            expected='uncertain' if key in ('opened','accept_started') else key
            self.assertEqual(self.store.db.execute('SELECT status FROM friend_requests WHERE id=?',(key,)).fetchone()[0],expected)
            self.assertEqual(confirm_dialog(self.store,key,'Ariel',None)['confirm_clicks'],0)

    def test_disabled_policy_never_navigates(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'data';p.mkdir();p.joinpath('friend-request-policy.json').write_text('{"auto_accept_friend_requests":false}')
            manager=FriendRequests(root,self.store);runtime=Mock()
            self.assertFalse(manager.process(runtime,'test',force=True));runtime.node.assert_not_called()

    def test_active_send_defers_navigation(self):
        self.store.db.execute('CREATE TABLE reply_outbox(status TEXT)')
        for status in ('queued','ready','send_started'):
            self.store.db.execute('DELETE FROM reply_outbox');self.store.db.execute('INSERT INTO reply_outbox VALUES(?)',(status,))
            with tempfile.TemporaryDirectory() as root:
                manager=FriendRequests(root,self.store);runtime=Mock()
                self.assertFalse(manager.process(runtime,'test',force=True));runtime.node.assert_not_called()

if __name__=='__main__':unittest.main()
