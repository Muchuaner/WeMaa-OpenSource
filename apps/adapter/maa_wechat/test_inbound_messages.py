from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from inbound_messages import InboundMessages
from inbound_work import InboundWork
from store import Store


class MessageWorkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'state.sqlite3'
        self.store = Store(self.path)
        self.messages = InboundMessages(self.store)
        self.cid = 'c' * 64
        self.target = {'name':'Test', 'conversation_title':'Test', 'avatar_sha':'a' * 64}
        self.row = dict(self.target, key='row', unread=False)
        self.old = [self.payload('anchor-one', 120), self.payload('anchor-two', 180)]
        self.queue = Mock(side_effect=lambda body,snapshot,ordinal,target:
                          self.store.queue_reply(body,snapshot,ordinal))
        self.client = SimpleNamespace(root=self.root, queue=self.queue)
        self.creation = SimpleNamespace(queue_event=Mock())
        self.experiment = SimpleNamespace(applies_to=lambda body:False)
        self.proof = {'wechat_id':'wx_test', 'display_name':'Test', 'stable':True,
                      'profile_sha256':'f'*64, 'message_frame_sha256':'d'*64,
                      'message_identity':'a'*64, 'message_avatar_sha256':'b'*64}
        self.observe(self.old)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def payload(self, text, y, safe=True):
        return {'text':text if safe else '', 'direction':'incoming', 'message_kind':'text',
            'reply_safe':safe, 'ocr':{'stable':safe},
            'text_copy':{'verified':safe, 'method':'native_context_menu',
                         'reason':None if safe else 'COPY_CLIPBOARD_BUSY'},
            'visual':{'verified':True, 'box':[540,y,180,40],
                      'sha256':hashlib.sha256(text.encode()).hexdigest(),
                      'avatar_verified':True, 'avatar_sha256':'b'*64, 'avatar_box':[495,y,36,36]}}

    def observe(self, payloads, enabled=True, suppress=False, now=None):
        now=time.time() if now is None else now
        return self.store.snapshot(self.cid, 'Test', self.target['avatar_sha'], 'frame.png', deepcopy(payloads),
            observation_head=self.messages.head(self.cid),
            on_observation=lambda result,values:self.messages.record(self.cid,self.target,result,values,
                                  enabled=enabled,suppress_new=suppress,now=now))

    def process(self, result, payloads):
        with patch('sender_identity.verify_member',return_value=self.proof):
            return self.messages.process(result,deepcopy(payloads),self.row,'Test',self.target,
                self.client,self.creation,self.experiment,None,None,'test',90)

    def test_restart_after_snapshot_commit_recovers_without_badge(self):
        payloads=self.old+[self.payload('fresh',240)]
        first=self.observe(payloads,now=100)
        event=first['message_event_ids']['2']
        self.store.db.close()
        self.store=Store(self.path)
        self.messages=InboundMessages(self.store)
        second=self.observe(payloads)
        self.assertEqual(second['status'],'unchanged')
        self.assertEqual(second['message_event_ids']['2'],event)
        with patch('inbound_messages.time.time',return_value=110):
            self.assertEqual(self.process(second,payloads)['accepted'],[2])
        self.assertEqual(self.messages.item(event)['status'],'queued')
        self.process(self.observe(payloads),payloads)
        self.assertEqual(self.queue.call_count,1)

    def private_contact(self):
        from contact_identity import link_verified_chat
        self.store.db.execute('INSERT INTO contacts(id,wechat_id,identity_status,facts_json) VALUES(?,?,?,?)',
            ('wx:wx_test','wx_test','ocr_wechat_id',json.dumps({'scope':'personal'})))
        route_proof={'wechat_id':'wx_test','stable':True,'profile_sha256':'f'*64,
                     'observation_sha256':'e'*64}
        link_verified_chat(self.store,'wx:wx_test',self.cid,self.target,route_proof)

    def test_bound_private_contact_route_proof_requires_fresh_message_profile_and_dispatches(self):
        # Production contact_chat_history has no display_name or message/avatar
        # hashes. It must constrain wxid, never masquerade as a message proof.
        self.private_contact()
        payloads=self.old+[self.payload('哈咯',240)]
        result=self.observe(payloads)
        with patch('sender_identity.verify_member',return_value=self.proof) as verify:
            outcome=self.messages.process(result,payloads,self.row,'Test',self.target,
                self.client,self.creation,self.experiment,None,None,'test',90)
        self.assertEqual(outcome,{'accepted':[2],'rejected':[]})
        self.assertEqual(verify.call_args.kwargs,{'expected_wx':'wx_test'})
        body=self.queue.call_args.args[0]
        self.assertEqual(body['text'],'哈咯')
        self.assertEqual(set(body['identity']),{'wechat_id','display_name','profile_sha256',
            'message_frame_sha256','message_avatar_sha256','stable'})
        saved=json.loads(self.store.db.execute('SELECT proof_json FROM contact_chat_history').fetchone()[0])
        self.assertEqual(saved['observation_sha256'],self.proof['message_frame_sha256'])
        self.assertEqual(self.messages.item(result['message_event_ids']['2'])['status'],'queued')

    def test_private_contact_proof_does_not_authorize_different_sender(self):
        self.private_contact()
        payloads=self.old+[self.payload('fresh',240)]
        result=self.observe(payloads)
        with patch('sender_identity.verify_member',return_value=dict(self.proof,wechat_id='wx_other')):
            outcome=self.messages.process(result,payloads,self.row,'Test',self.target,
                self.client,self.creation,self.experiment,None,None,'test',90)
        self.assertEqual(outcome['accepted'],[])
        self.assertEqual(self.messages.item(result['message_event_ids']['2'])['reason'],
                         'MESSAGE_SENDER_PROFILE_CONFLICT')
        self.queue.assert_not_called()

    def test_same_avatar_directory_entry_cannot_replace_new_occurrence_profile(self):
        from sender_directory import SenderDirectory
        SenderDirectory(self.store).remember(self.cid,self.old[-1],self.proof,'Test')
        payloads=self.old+[self.payload('fresh',240)]
        result=self.observe(payloads)
        with patch('sender_identity.verify_member',return_value=self.proof) as verify:
            outcome=self.messages.process(result,payloads,self.row,'Test',self.target,
                self.client,self.creation,self.experiment,None,None,'test',90)
        self.assertEqual(outcome['accepted'],[2])
        verify.assert_called_once()

    def test_one_copy_failure_does_not_block_good_peer_and_recovers_later(self):
        payloads=self.old+[self.payload('good',240),self.payload('bad',300,safe=False)]
        first=self.observe(payloads)
        self.assertEqual(first['status'],'review')
        events=first['message_event_ids']
        outcome=self.process(first,payloads)
        self.assertEqual(outcome,{'accepted':[2],'rejected':[3]})
        self.assertEqual(self.messages.item(events['2'])['status'],'queued')
        self.assertEqual(self.messages.item(events['3'])['status'],'pending')
        self.store.db.execute('UPDATE inbound_message_work SET next_attempt=0')
        self.store.db.commit()
        payloads[-1]=self.payload('bad',300)
        second=self.observe(payloads)
        self.assertEqual(second['message_event_ids'],events)
        self.assertEqual(self.process(second,payloads)['accepted'],[3])
        self.assertEqual(self.queue.call_count,2)
        self.assertEqual(self.messages.item(events['3'])['first_seen'],self.messages.item(events['2'])['first_seen'])

    def test_old_review_retained_while_fresh_append_gets_independent_work(self):
        work=InboundWork(self.store)
        work.observe([dict(self.row,unread=True)])
        work.update('row','review','context_gap_not_verified',1)
        seed=work.get('row')['seed']
        history=[self.payload('gap-one',120),self.payload('gap-two',180)]
        baseline=self.observe(history,suppress=True)
        self.assertEqual(baseline['message_event_ids'],{})
        fresh=history+[self.payload('fresh-after-review',240)]
        result=self.observe(fresh)
        self.assertEqual(self.process(result,fresh)['accepted'],[2])
        self.assertEqual(work.get('row')['status'],'review')
        self.assertEqual(work.get('row')['seed'],seed)

    def test_callback_failure_rolls_back_snapshot_messages_and_cursor(self):
        head=self.messages.head(self.cid)
        def fail(result,values):
            self.messages.record(self.cid,self.target,result,values,enabled=True)
            raise RuntimeError('INTERRUPTED_BEFORE_COMMIT')
        with self.assertRaisesRegex(RuntimeError,'INTERRUPTED'):
            self.store.snapshot(self.cid,'Test',self.target['avatar_sha'],'new.png',
                self.old+[self.payload('fresh',240)],observation_head=head,on_observation=fail)
        self.assertEqual(self.messages.head(self.cid),head)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM snapshots').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM messages').fetchone()[0],0)
        self.assertEqual(self.messages.health()['status_counts'],{})

    def test_disabled_capture_cannot_acquire_permission_later(self):
        payloads=self.old+[self.payload('held',240)]
        first=self.observe(payloads,enabled=False)
        event=first['message_event_ids']['2']
        second=self.observe(payloads,enabled=True)
        self.process(second,payloads)
        self.assertEqual(self.messages.item(event)['status'],'held')
        self.queue.assert_not_called()

    def test_held_failed_copy_can_be_received_later_without_reply_permission(self):
        payloads=self.old+[self.payload('held-copy',240,safe=False)]
        first=self.observe(payloads,enabled=False)
        event=first['message_event_ids']['2']
        payloads[-1]=self.payload('held-copy',240)
        second=self.observe(payloads,enabled=True)
        self.process(second,payloads)
        self.assertEqual(self.messages.item(event)['status'],'held')
        saved=json.loads(self.store.db.execute('SELECT payload_json FROM messages WHERE id=?',(event[:32],)).fetchone()[0])
        self.assertEqual(saved['text'],'held-copy')
        self.queue.assert_not_called()

    def test_transient_profile_ocr_reobserves_same_event_without_replaying_terminal_work(self):
        payloads=self.old+[self.payload('fresh',240)]
        result=self.observe(payloads)
        event=result['message_event_ids']['2']
        with patch('sender_identity.verify_member',side_effect=ValueError('SENDER_PROFILE_ID_NOT_STABLE')):
            outcome=self.messages.process(result,payloads,self.row,'Test',self.target,
                self.client,self.creation,self.experiment,None,None,'test',90)
        self.assertEqual(outcome['rejected'],[2])
        self.assertEqual(self.messages.item(event)['status'],'pending')
        self.store.db.execute('UPDATE inbound_message_work SET next_attempt=0');self.store.db.commit()
        self.assertEqual(self.process(self.observe(payloads),payloads)['accepted'],[2])
        self.store.reply_update(event,'uncertain',result={'status':'uncertain','reason':'SEND_OUTCOME_UNKNOWN'})
        self.messages.update(event,'pending','simulated_restart_before_receipt')
        with patch('sender_identity.verify_member') as verify:
            self.messages.process(self.observe(payloads),payloads,self.row,'Test',self.target,
                self.client,self.creation,self.experiment,None,None,'test',90)
        verify.assert_not_called()
        self.assertEqual(self.queue.call_count,1)
        self.assertEqual(self.store.reply_rows(('uncertain',))[0]['event_id'],event)

    def test_visual_row_key_change_keeps_original_occurrence_id(self):
        payloads=self.old+[self.payload('fresh',240)]
        first=self.observe(payloads)
        self.row['key']='new-observed-row-key'
        second=self.observe(payloads)
        self.assertEqual(first['message_event_ids'],second['message_event_ids'])
        self.assertEqual(self.process(second,payloads)['accepted'],[2])

    def test_shifted_occurrence_keeps_original_snapshot_and_source_ordinal(self):
        payloads=self.old+[self.payload('fresh',240)]
        first=self.observe(payloads)
        event=first['message_event_ids']['2']
        shifted=deepcopy(payloads[1:])
        for payload in shifted:
            payload['visual']['box'][1]-=60
            payload['visual']['avatar_box'][1]-=60
        second=self.observe(shifted)
        self.assertEqual(second['message_event_ids']['1'],event)
        self.assertEqual(self.process(second,shifted)['accepted'],[1])
        reply=self.store.reply_rows(('queued',))[0]
        self.assertEqual((reply['snapshot_id'],reply['ordinal']),(first['snapshot_id'],2))
        journal=self.store.db.execute('SELECT snapshot_id,ordinal,source_ordinal FROM inbound_dispatch WHERE event_id=?',
                                      (event,)).fetchone()
        self.assertEqual(journal,(first['snapshot_id'],2,2))

    def test_activation_cutoff_uses_original_seen_after_restart(self):
        payloads=self.old+[self.payload('older-than-activation',240)]
        first=self.observe(payloads,now=100)
        self.store.db.execute('INSERT INTO events(kind,detail_json,seen) VALUES(?,?,?)',
            ('auto_reply_scope_activated',json.dumps({'scope':'all_private_and_whitelisted_groups','historical_replay':False}),101))
        self.store.db.commit()
        self.process(self.observe(payloads,now=102),payloads)
        self.assertEqual(self.messages.item(first['message_event_ids']['2'])['status'],'held')
        self.queue.assert_not_called()

    def test_verification_retries_survive_four_minutes_with_bounded_backoff(self):
        payloads=self.old+[self.payload('bad',240,safe=False)]
        result=self.observe(payloads,now=100)
        event=result['message_event_ids']['2']
        for attempt in range(10):
            self.messages.update(event,'pending','COPY_CLIPBOARD_BUSY',retry=True,now=100+attempt*300)
        item=self.messages.item(event)
        self.assertEqual(item['status'],'pending')
        self.assertEqual(item['first_seen'],100)
        self.assertLessEqual(item['next_attempt']-2800,300)
        self.assertEqual(self.messages.pending([self.row],now=4000)[0]['key'],'row')

    def test_unlocated_work_backs_off_and_exposes_review_without_losing_body(self):
        payloads=self.old+[self.payload('retained',240)]
        first=self.observe(payloads,now=100)
        event=first['message_event_ids']['2']
        other=[self.payload('different-one',120),self.payload('different-two',180)]
        gap=self.observe(other,now=101)
        self.assertEqual(gap['message_event_ids'],{})
        self.assertEqual(self.messages.pending([self.row],now=102),[])
        self.observe(other,now=401)
        item=self.messages.item(event)
        self.assertEqual(item['status'],'review')
        self.assertEqual(item['first_seen'],100)
        self.assertEqual(json.loads(item['payload_json'])['text'],'retained')
        self.assertEqual(self.messages.health()['reviews'][0]['event_id'],event)
        self.queue.assert_not_called()

    def test_long_native_text_kept_as_explicit_review_instead_of_dropped(self):
        payloads=self.old+[self.payload('x'*2001,240)]
        result=self.observe(payloads)
        self.process(result,payloads)
        item=self.messages.item(result['message_event_ids']['2'])
        self.assertEqual(item['status'],'review')
        self.assertEqual(item['reason'],'INBOUND_TEXT_EXCEEDS_REPLY_LIMIT')
        self.assertEqual(len(json.loads(item['payload_json'])['text']),2001)
        self.queue.assert_not_called()

    def test_identical_bodies_without_unique_history_do_not_invent_occurrences(self):
        duplicate=[self.payload('same',120),self.payload('same',180)]
        self.observe(duplicate,suppress=True)
        result=self.observe(duplicate+[self.payload('same',240)])
        self.assertEqual(result['message_event_ids'],{})
        self.queue.assert_not_called()


if __name__=='__main__':
    unittest.main()
