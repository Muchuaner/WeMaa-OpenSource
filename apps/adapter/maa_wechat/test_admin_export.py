import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from PIL import Image, ImageDraw
from admin_export import Assets, export, payload_assets
from store import Store


class ExportTests(unittest.TestCase):
    def private_link(self, cid, wxid='test_user', *, stable=True, scope='personal'):
        self.store.db.execute('INSERT INTO contacts VALUES(?,?,?,?,?,?,?,?)',
            ('wx:'+wxid,wxid,'same name','avatar',str(self.avatar),json.dumps({'scope':scope}),time.time(),'ocr_wechat_id'))
        self.store.db.execute('INSERT INTO contact_chat_links VALUES(?,?,?)',('wx:'+wxid,cid,time.time()))
        self.store.db.execute('CREATE TABLE IF NOT EXISTS contact_chat_history(contact_id TEXT,conversation_id TEXT,seen REAL,proof_json TEXT,PRIMARY KEY(contact_id,conversation_id))')
        self.store.db.execute('INSERT INTO contact_chat_history VALUES(?,?,?,?)',
            ('wx:'+wxid,cid,time.time(),json.dumps({'wechat_id':wxid,'stable':stable,'profile_sha256':'e'*64})))
        self.store.db.commit()

    def test_private_binding_has_exact_profile_proof_and_message_snapshot_status(self):
        cid='b'*64
        self.store.snapshot(cid,'same name','avatar',str(self.avatar),[{'text':'old'}])
        self.store.snapshot(cid,'same name','avatar',str(self.avatar),[{'text':'old'},{'text':'new'}])
        self.private_link(cid)
        report,_=export(self.root,{'worker_state':'Ready'})
        row=report['conversations'][0]
        self.assertEqual(row['private_contact']['wechat_id'],'test_user')
        self.assertTrue(row['private_contact']['profile_verified'])
        self.assertNotIn('profile_sha256',row['private_contact'])
        self.assertEqual(row['messages'][0]['source_snapshot_status'],'append')

    def test_partial_native_receipts_are_bound_to_exact_message_snapshot_and_ordinal(self):
        from inbound_messages import InboundMessages
        cid='b'*64;messages=InboundMessages(self.store)
        def payload(n,safe=True):
            return {'text':str(n) if safe else '', 'direction':'incoming','message_kind':'text',
                    'reply_safe':safe,'visual':{'verified':True,'box':[545,100+n*40,100,36],'sha256':str(n)*64}}
        old=[payload(1),payload(2)]
        target={'name':'same name','conversation_title':'same name','avatar_sha':'avatar'}
        def observe(values):
            return self.store.snapshot(cid,'same name','avatar',str(self.avatar),values,
                observation_head=messages.head(cid),on_observation=lambda r,p:messages.record(cid,target,r,p,enabled=True))
        observe(old);result=observe(old+[payload(3),payload(4,False)])
        self.private_link(cid)
        report,_=export(self.root,{'worker_state':'Ready'})
        rows=report['conversations'][0]['messages']
        self.assertEqual(len(rows),2)
        for row in rows:
            receipt=row['message_receipt']
            self.assertEqual(receipt['event_id'][:32],row['id'])
            self.assertEqual(receipt['snapshot_id'],result['snapshot_id'])
            self.assertEqual(receipt['ordinal'],row['ordinal'])
            self.assertEqual(receipt['first_seen'],row['seen'])
            self.assertEqual(row['source_snapshot_status'],'review')

    def test_unverified_or_wrong_scope_link_never_exports_identity(self):
        for stable,scope in ((False,'personal'),(True,'enterprise')):
            with self.subTest(stable=stable,scope=scope):
                cid='c'*64
                self.store.snapshot(cid,'same name','avatar',str(self.avatar),[])
                self.store.db.execute('DELETE FROM contacts');self.store.db.execute('DELETE FROM contact_chat_links')
                self.store.db.execute('DROP TABLE IF EXISTS contact_chat_history');self.store.db.commit()
                self.private_link(cid,stable=stable,scope=scope)
                report,_=export(self.root,{'worker_state':'Ready'})
                self.assertNotIn('private_contact',report['conversations'][0])

    def test_ambiguous_links_and_groups_never_export_private_identity(self):
        cid='d'*64
        self.store.snapshot(cid,'same name','avatar',str(self.avatar),[])
        self.private_link(cid);self.private_link(cid,'other_user')
        report,_=export(self.root,{'worker_state':'Ready'})
        self.assertNotIn('private_contact',report['conversations'][0])
        self.store.db.execute('DELETE FROM contact_chat_links WHERE contact_id=?',('wx:other_user',))
        self.store.observe_group(cid,'same name','same name (4)')
        report,_=export(self.root,{'worker_state':'Ready'})
        self.assertNotIn('private_contact',report['conversations'][0])

    def test_private_profile_alias_keeps_canonical_account_binding(self):
        alias='a'*64;cid='f'*64
        self.store.snapshot(cid,'same name','avatar',str(self.avatar),[])
        self.private_link(alias)
        self.store.db.execute('INSERT INTO group_aliases VALUES(?,?,?,?)',(alias,cid,'same_reverified_private_wechat_id',time.time()))
        self.store.db.commit()
        report,_=export(self.root,{'worker_state':'Ready'})
        self.assertEqual(report['conversations'][0]['private_contact']['contact_id'],'wx:test_user')

    def test_mismatched_group_profile_is_unverified_and_raw_evidence_preserved(self):
        cid='a'*64
        self.store.snapshot(cid,'开发组 (4)','avatar',str(self.avatar),[])
        profile=(cid,'group','开发组',4,'开发组旧名','开发组 (4)',time.time())
        self.store.db.execute('INSERT INTO conversation_profiles VALUES(?,?,?,?,?,?,?)',profile);self.store.db.commit()
        report,_=export(self.root,{'worker_state':'Ready'})
        row=report['conversations'][0]
        self.assertNotIn('group_profile',row)
        self.assertEqual(row['group_profile_status'],'unverified')
        self.assertEqual(tuple(self.store.db.execute('SELECT * FROM conversation_profiles').fetchone()),profile)
        self.store.db.execute('UPDATE conversation_profiles SET row_name=?',('开发组',));self.store.db.commit()
        report,_=export(self.root,{'worker_state':'Ready'})
        self.assertEqual(report['conversations'][0]['group_profile']['member_count'],4)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data' / 'wechat.sqlite3')
        self.avatar = self.root / 'data' / 'avatar.png'
        Image.new('RGB', (24, 24), (10, 20, 30)).save(self.avatar)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def scan(self, scope, name, complete=True):
        scan = self.store.begin_scan(1)
        contact = self.store.contact(scan, 0, name, None, 'raw-pixel-sha', str(self.avatar),
                                     {'scope': scope}, None, [], [scan, 0])
        self.store.contact_position(scan, 0, contact, name, 'raw-pixel-sha', '')
        self.store.finish_scan(scan, 1, 1, complete, {'scope': scope, 'bottom_verified': complete,
                                                  'coverage_grain': 'aligned visible list positions'})
        return scan

    def test_paused_worker_does_not_claim_sending_is_enabled(self):
        progress = self.root/'data'/'progress.json'
        progress.write_text(json.dumps({'run_id': 'active', 'status': 'waiting_for_window'}))
        report, _ = export(self.root, {'run_id': 'active', 'worker_state': 'Running',
            'auto_reply_enabled': True, 'sending_enabled': True, 'continuous': True})
        self.assertFalse(report['runtime']['sending_enabled'])
        self.assertFalse(report['runtime']['auto_reply_enabled'])
        # A previous run's failed progress must not disable the new worker.
        report, _ = export(self.root, {'run_id': 'new', 'worker_state': 'Running',
            'auto_reply_enabled': True, 'sending_enabled': True})
        self.assertTrue(report['runtime']['sending_enabled'])

    def test_input_desktop_pause_preserves_read_only_evidence_and_disables_send(self):
        evidence = {'run_id': 'active', 'checked_at': 1, 'available': False,
                    'ready': False, 'cursor_readable': False, 'reason': 'CURSOR_ACCESS_DENIED'}
        (self.root / 'data' / 'progress.json').write_text(json.dumps({
            'run_id': 'active', 'status': 'waiting_for_input', 'desktop_input': evidence}))
        report, _ = export(self.root, {'run_id': 'active', 'worker_state': 'Running',
            'auto_reply_enabled': True, 'sending_enabled': True, 'continuous': True})
        self.assertFalse(report['runtime']['sending_enabled'])
        self.assertFalse(report['runtime']['auto_reply_enabled'])
        self.assertEqual(report['runtime']['progress']['desktop_input'], evidence)
        # A stale blocked run cannot override a different active worker.
        report, _ = export(self.root, {'run_id': 'new', 'worker_state': 'Running',
            'auto_reply_enabled': True, 'sending_enabled': True})
        self.assertTrue(report['runtime']['sending_enabled'])
        self.assertTrue(report['runtime']['auto_reply_enabled'])

    def test_only_fresh_current_desktop_file_updates_progress(self):
        progress_path = self.root / 'data' / 'progress.json'
        progress_path.write_text(json.dumps({'run_id': 'active', 'status': 'running'}))
        path = self.root / 'data' / 'desktop-input.json'
        runtime = {'run_id': 'active', 'worker_state': 'Running',
                   'auto_reply_enabled': True, 'sending_enabled': True}
        evidence = {'run_id': 'active', 'checked_at': time.time(), 'available': False, 'ready': False}
        path.write_text(json.dumps(evidence))
        report, _ = export(self.root, runtime)
        self.assertEqual(report['runtime']['progress']['desktop_input'], evidence)
        self.assertFalse(report['runtime']['sending_enabled'])
        for updates in ({'checked_at': time.time()-30}, {'run_id': 'previous'}, {'checked_at': time.time()+60}):
            path.write_text(json.dumps(dict(evidence, **updates)))
            report, _ = export(self.root, runtime)
            self.assertNotIn('desktop_input', report['runtime']['progress'])
            self.assertTrue(report['runtime']['sending_enabled'])
        evidence.update(checked_at=time.time(), available=True, ready=True)
        path.write_text(json.dumps(evidence))
        report, _ = export(self.root, runtime)
        self.assertTrue(report['runtime']['progress']['desktop_input']['ready'])
        self.assertTrue(report['runtime']['sending_enabled'])

    def test_only_latest_complete_partition_not_failed_global_contacts(self):
        old = self.scan('personal', 'old')
        latest = self.scan('personal', 'current')
        self.scan('personal', 'failed', False)
        self.scan('enterprise', 'enterprise')
        report, images = export(self.root, {'worker_state': 'Ready'})
        self.assertEqual({item['list_name'] for item in report['contacts']}, {'current', 'enterprise'})
        personal = next(item for item in report['contacts'] if item['scope'] == 'personal')
        self.assertEqual(personal['scan_id'], latest)
        self.assertNotIn(old, [item['id'] for item in report['scans']])
        self.assertTrue(report['runtime']['sending_enabled'] is False)
        for sha, raw in images.items():
            self.assertEqual(hashlib.sha256(raw).hexdigest(), sha)

    def test_visual_history_and_incremental_rows_are_separate(self):
        cid = 'a' * 64
        first = self.store.snapshot(cid, 'Demo User', 'x', str(self.avatar), [{'text': 'old', 'sender': 'unresolved'}])
        second = self.store.snapshot(cid, 'Demo User', 'x', str(self.avatar), [{'text': 'old', 'sender': 'unresolved'}, {'text': 'new', 'sender': 'unresolved'}])
        report, _ = export(self.root, {'worker_state': 'Ready'})
        row = report['conversations'][0]
        self.assertEqual(row['latest_snapshot']['status'], 'append')
        self.assertEqual(len(row['latest_snapshot']['payloads']), 2)
        self.assertEqual(len(row['messages']), 1)
        self.assertEqual(report['summary']['message_row_count'], 1)
        self.assertEqual(row['identity_status'], 'visual_provisional')
        self.assertIsNone(row['unread_at_observation'])
        self.assertIsNone(row['unread_observed_at'])

    def test_historical_red_dot_has_observation_time(self):
        cid = 'b' * 64
        self.store.snapshot(cid, 'Demo User', 'x', str(self.avatar), [{'text': 'old'}])
        self.store.event('chat_observed', {'conversation': cid, 'unread': True})
        report, _ = export(self.root, {'worker_state': 'Ready'})
        row = report['conversations'][0]
        self.assertTrue(row['unread_at_observation'])
        self.assertIsInstance(row['unread_observed_at'], float)

    def test_old_screenshot_bytes_are_not_kept_forever(self):
        cid = 'c' * 64
        first = self.root / 'data' / 'first.png'
        second = self.root / 'data' / 'second.png'
        Image.new('RGB', (100, 100), (200, 100, 30)).save(first)
        Image.new('RGB', (100, 100), (10, 10, 30)).save(second)
        assets = Assets(self.root)
        self.store.snapshot(cid, 'Demo User', 'x', str(first), [{'text': 'old'}])
        export(self.root, {'worker_state': 'Ready'}, assets)
        self.store.snapshot(cid, 'Demo User', 'x', str(second), [{'text': 'old'}, {'text': 'new'}])
        export(self.root, {'worker_state': 'Ready'}, assets)
        self.assertFalse(any(str(first.resolve()) == key[0] for key in assets.cache))
        self.assertTrue(any(str(second.resolve()) == key[0] for key in assets.cache))

    def test_asset_root_and_pixel_sha_are_not_file_ids(self):
        assets = Assets(self.root)
        self.assertIsNone(assets.image(Path(self.temp.name).parent / 'secret.png'))
        sha = assets.image(self.avatar, 'avatar')
        self.assertEqual(len(sha), 64)
        self.assertNotEqual(sha, 'raw-pixel-sha')

    def test_invalid_coverage_does_not_export_contacts(self):
        scan = self.scan('public', 'bad')
        self.store.db.execute('UPDATE contact_positions SET ordinal=4 WHERE scan_id=?', (scan,))
        self.store.db.commit()
        report, _ = export(self.root, {'worker_state': 'Ready'})
        self.assertEqual(report['contacts'], [])
        self.assertFalse(report['scans'][0]['coverage_valid'])

    def frame_with_avatar(self, name, color, box=(500, 180, 36, 36), direction='incoming'):
        frame = self.root / 'data' / name
        picture = Image.new('RGB', (1920, 1040), (248, 248, 248))
        x, y, w, h = box
        ImageDraw.Draw(picture).rectangle((x, y, x+w-1, y+h-1), fill=color)
        picture.save(frame)
        payload = {'text': name, 'message_kind': 'text', 'direction': direction, 'visual': {
            'avatar_box': list(box), 'avatar_verified': True,
            'avatar_sha256': hashlib.sha256(picture.crop((x, y, x+w, y+h)).tobytes()).hexdigest(),
            'avatar_source': 'same_frame_message_avatar', 'avatar_color_order': 'RGB'}}
        return frame, payload

    def test_message_avatars_are_the_speakers_and_content_addressed(self):
        left, incoming = self.frame_with_avatar('left.png', (30, 90, 180))
        right, outgoing = self.frame_with_avatar('right.png', (180, 50, 30), (1858, 200, 36, 36), 'outgoing')
        assets = Assets(self.root)
        for frame, payload, expected in ((left, incoming, (30, 90, 180)), (right, outgoing, (180, 50, 30))):
            result = payload_assets(payload, frame, assets)
            sha = result['sender_avatar_asset']
            self.assertEqual(hashlib.sha256(assets.items[sha]).hexdigest(), sha)
            with Image.open(io.BytesIO(assets.items[sha])) as crop:
                self.assertEqual(crop.size, (36, 36))
                self.assertEqual(crop.getpixel((10, 10)), expected)
        self.assertNotEqual(incoming['sender_avatar_asset'], outgoing['sender_avatar_asset'])

    def test_moved_or_unverified_avatar_and_conversation_icon_are_rejected(self):
        frame, payload = self.frame_with_avatar('speaker.png', (30, 90, 180))
        other, _ = self.frame_with_avatar('moved.png', (180, 50, 30))
        assets = Assets(self.root)
        self.assertIsNone(assets.message_avatar(other, payload))
        payload['visual']['avatar_verified'] = False
        self.assertIsNone(assets.message_avatar(frame, payload))
        payload['visual']['avatar_verified'] = True
        payload['visual']['avatar_box'] = [72, 355, 36, 36]
        self.assertIsNone(assets.message_avatar(frame, payload))
        payload['sender_avatar_asset'] = 'f' * 64
        self.assertNotIn('sender_avatar_asset', payload_assets(payload, frame, assets))

    def test_incremental_message_keeps_its_own_snapshot_avatar(self):
        cid = 'd' * 64
        first_frame, first_payload = self.frame_with_avatar('first-message.png', (30, 90, 180))
        first = self.store.snapshot(cid, '开发组(4)', 'x', str(first_frame), [first_payload])
        self.store.db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)', (
            'old-message', cid, first['snapshot_id'], 0, json.dumps(first_payload), 1))
        self.store.db.commit()
        newest_frame, newest_payload = self.frame_with_avatar('new-message.png', (180, 50, 30))
        self.store.snapshot(cid, '开发组(4)', 'x', str(newest_frame), [newest_payload])
        report, images = export(self.root, {'worker_state': 'Ready'})
        row = report['conversations'][0]
        old_sha = row['messages'][0]['payload']['sender_avatar_asset']
        new_sha = row['latest_snapshot']['payloads'][0]['sender_avatar_asset']
        self.assertNotEqual(old_sha, new_sha)
        with Image.open(io.BytesIO(images[old_sha])) as crop:
            self.assertEqual(crop.getpixel((10, 10)), (30, 90, 180))


if __name__ == '__main__':
    unittest.main()
