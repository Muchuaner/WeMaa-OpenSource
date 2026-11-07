import json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from contextlib import ExitStack
import numpy as np
from store import Store
from watch_policy import polling_configuration,set_polling,eligible_visible_rows
from watch import watch,send_ready_on_rows

class VisiblePolicyTests(unittest.TestCase):
    def test_recovered_watch_routes_new_creation_command_in_same_activation(self):
        from test_inbound_messages import MessageWorkTests
        from watch_recovery import ContinuousActivation
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            root=Path(folder);(root/'data').mkdir();runtime=Mock()
            picture=np.zeros((1040,1920,3),dtype=np.uint8)
            row={'name':'Test','key':'row','avatar_sha':'a'*64,'unread':False,'y':113}
            helper=MessageWorkTests();old=[helper.payload('history-one',120),helper.payload('history-two',180)]
            new=old+[helper.payload('/生图 恢复期间的新命令',240)]
            runtime.capture.return_value=(picture,'frame')
            runtime.capture_image.return_value=picture
            creation=Mock();creation.process_one.return_value=False
            creation.delivery_ready.return_value=False;creation.delivery_in_flight.return_value=False
            creation.pending_deliveries.return_value=0;creation.delivery_health.return_value={}
            greeting=Mock();greeting.applies_to.return_value=False
            cleanup=Mock();cleanup.result={}
            client=Mock();client.root=root;client.ready.return_value=[]
            stack.enter_context(patch('reply_client.ReplyClient',return_value=client))
            stack.enter_context(patch('creation_client.CreationClient',return_value=creation))
            stack.enter_context(patch('greeting_client.GreetingClient',return_value=greeting))
            stack.enter_context(patch('inactive_chats.InactiveChats',return_value=cleanup))
            gate=stack.enter_context(patch('desktop_input.InputDesktopGate'));gate.return_value.check.return_value={'ready':True}
            stack.enter_context(patch('manual_gui.process_one',return_value=False))
            stack.enter_context(patch('watch.scroll_top'))
            stack.enter_context(patch('watch.read_rows',return_value=([row],picture,'frame')))
            stack.enter_context(patch('watch.read_title',return_value='Test'))
            stack.enter_context(patch('watch.select',return_value=True))
            stack.enter_context(patch('watch.send_ready_on_rows',return_value=False))
            stack.enter_context(patch('text_copy.read_messages',side_effect=[(old,[]),(new,[])]))
            scheduler=Mock();scheduler.badge_scan_due.return_value=True
            scheduler.dump_state.return_value={'conversations':{}}
            scheduler.status.return_value={'weight':1}
            scheduler.plan.side_effect=lambda rows,**kw:[dict(rows[0],schedule={'reason':'due','weight':1})] if rows else []
            stack.enter_context(patch('watch.ActivityScheduler',return_value=scheduler))
            proof={'wechat_id':'wx_test','display_name':'Test','stable':True,
                   'profile_sha256':'f'*64,'message_frame_sha256':'d'*64,
                   'message_identity':'e'*64,'message_avatar_sha256':'b'*64}
            stack.enter_context(patch('sender_identity.verify_member',return_value=proof))
            activation=ContinuousActivation()
            watch(runtime,root,'same-worker',cycles=1,auto_reply=True,activation=activation)
            enabled_at=activation.state[1]
            creation.queue_event.assert_not_called()
            # Re-enter the real watch loop with the same process's baselines.
            watch(runtime,root,'same-worker',cycles=1,auto_reply=True,activation=activation)
            self.assertEqual(activation.state[1],enabled_at)
            creation.queue_event.assert_called_once()
            self.assertEqual(creation.queue_event.call_args.args[0]['text'],'/生图 恢复期间的新命令')
            client.queue.assert_not_called()

    def test_full_watch_partial_batch_dispatches_peer_and_prioritises_new_unread(self):
        from test_inbound_messages import MessageWorkTests
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            root=Path(folder);(root/'data').mkdir();runtime=Mock()
            picture=np.zeros((1040,1920,3),dtype=np.uint8)
            row={'name':'Test','key':'row','avatar_sha':'a'*64,'unread':False,'y':113}
            urgent=dict(row,name='Other',key='other',unread=True,y=180)
            helper=MessageWorkTests()
            old=[helper.payload('one',120),helper.payload('two',180)]
            new=old+[helper.payload('good',240),helper.payload('bad',300,safe=False)]
            runtime.capture.return_value=(picture,'frame')
            runtime.capture_image.return_value=picture
            creation=Mock();creation.process_one.return_value=False
            creation.delivery_ready.return_value=False;creation.delivery_in_flight.return_value=False
            creation.pending_deliveries.return_value=0;creation.delivery_health.return_value={}
            greeting=Mock();greeting.applies_to.return_value=False
            cleanup=Mock();cleanup.result={}
            def reply_client(owner,store):
                client=Mock();client.root=owner;client.ready.return_value=[]
                client.queue.side_effect=lambda body,snapshot,ordinal,target:store.queue_reply(body,snapshot,ordinal)
                return client
            stack.enter_context(patch('reply_client.ReplyClient',side_effect=reply_client))
            stack.enter_context(patch('creation_client.CreationClient',return_value=creation))
            stack.enter_context(patch('greeting_client.GreetingClient',return_value=greeting))
            stack.enter_context(patch('inactive_chats.InactiveChats',return_value=cleanup))
            gate=stack.enter_context(patch('desktop_input.InputDesktopGate'));gate.return_value.check.return_value={'ready':True}
            stack.enter_context(patch('manual_gui.process_one',return_value=False))
            stack.enter_context(patch('watch.scroll_top'))
            stack.enter_context(patch('watch.read_rows',side_effect=[([row],picture,'frame'),
                ([row],picture,'frame'),([row,urgent],picture,'frame')]))
            stack.enter_context(patch('watch.read_title',side_effect=lambda runtime,image,row:row['name']))
            selected=stack.enter_context(patch('watch.select',return_value=True))
            progress=stack.enter_context(patch('watch.progress'))
            stack.enter_context(patch('watch.send_ready_on_rows',return_value=False))
            stack.enter_context(patch('text_copy.read_messages',side_effect=[(old,[]),(new,[]),([],[])]))
            scheduler=Mock();scheduler.badge_scan_due.return_value=True
            scheduler.dump_state.return_value={'conversations':{}}
            scheduler.status.return_value={'weight':1}
            scheduler.plan.side_effect=lambda rows,**kwargs:[dict(rows[0],schedule={'reason':'due','weight':1})] if rows else []
            stack.enter_context(patch('watch.ActivityScheduler',return_value=scheduler))
            proof={'wechat_id':'wx_test','display_name':'Test','stable':True,
                   'profile_sha256':'f'*64,'message_frame_sha256':'d'*64,'message_avatar_sha256':'b'*64}
            stack.enter_context(patch('sender_identity.verify_member',return_value=proof))
            result=watch(runtime,root,'partial-watch',cycles=3,auto_reply=True)
            self.assertEqual(result['chats_read'],3)
            self.assertEqual(selected.call_args.args[1]['key'],'other')
            self.assertEqual(progress.call_args.args[1]['inbound_health']['messages']['status_counts'],
                             {'pending':1,'queued':1})
            db=Store(root/'data/wechat.sqlite3')
            self.assertEqual(dict(db.db.execute('SELECT status,count(*) FROM inbound_message_work GROUP BY status')),
                             {'pending':1,'queued':1})
            replies=db.reply_rows(('queued',))
            self.assertEqual(len(replies),1)
            self.assertEqual(json.loads(replies[0]['request_json'])['text'],'good')
            self.assertEqual(db.db.execute('SELECT count(*) FROM messages').fetchone()[0],2)
            db.db.close()

    def test_old_policy_migrates_scope_but_does_not_enable_paused_polling(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir()
            path=root/'data/polling-policy.json'
            path.write_text('{"enabled":false}',encoding='utf-8')
            self.assertEqual(polling_configuration(root),{'enabled':False,'scope':'main_visible'})
            set_polling(root,True)
            self.assertEqual(polling_configuration(root)['scope'],'main_visible')
            self.assertTrue(polling_configuration(root)['enabled'])
            path.write_text('{"enabled":true,"scope":"all_pages"}',encoding='utf-8')
            self.assertFalse(polling_configuration(root)['enabled'])
            path.write_text('[]',encoding='utf-8')
            self.assertFalse(polling_configuration(root)['enabled'])

    def test_push_and_unapproved_group_badges_cannot_starve_visible_private_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir();store=Store(root/'data/test.sqlite')
            target={'name':'other','avatar_sha':'a'*64,'conversation_title':'other(4)'}
            store.route('b'*64,target);store.observe_group('b'*64,'other','other(4)')
            (root/'data/group-whitelist.json').write_text('{"enabled":true,"groups":[]}',encoding='utf-8')
            rows=[{'name':'腾讯新闻','key':'push','unread':True},{'name':'other','key':'group','avatar_sha':'a'*64,'unread':True},{'name':'person','key':'person','unread':False}]
            eligible,excluded=eligible_visible_rows(root,store,rows)
            self.assertEqual([r['key'] for r in eligible],['person'])
            self.assertEqual([r['reason'] for r in excluded],['system_push','group_not_whitelisted'])
            store.db.close()

    def test_opening_outbox_is_retained_without_automatic_send(self):
        client=Mock();client.ready.return_value=[{'request_json':json.dumps({'_target':{'name':'person','avatar_sha':'a'},'_greeting':True})}]
        with patch('watch.select') as select:
            self.assertFalse(send_ready_on_rows(client,Mock(),'run',[{'name':'person','avatar_sha':'a'}],allow_opening=False))
            select.assert_not_called();client.send.assert_not_called()

    def test_full_watch_never_seeks_old_bookmark_or_greets_or_hides_chats(self):
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            root=Path(folder);(root/'data').mkdir();runtime=Mock()
            creation=Mock();creation.process_one.return_value=False
            creation.delivery_ready.return_value=False;creation.delivery_in_flight.return_value=False
            creation.pending_deliveries.return_value=0;creation.delivery_health.return_value={}
            greeting=Mock();cleanup=Mock();cleanup.result={}
            stack.enter_context(patch('creation_client.CreationClient',return_value=creation))
            stack.enter_context(patch('greeting_client.GreetingClient',return_value=greeting))
            stack.enter_context(patch('inactive_chats.InactiveChats',return_value=cleanup))
            gate=stack.enter_context(patch('desktop_input.InputDesktopGate'));gate.return_value.check.return_value={'ready':True}
            stack.enter_context(patch('manual_gui.process_one',return_value=False))
            stack.enter_context(patch('watch.scroll_top'))
            stack.enter_context(patch('watch.read_rows',return_value=([{'name':'person','key':'p','avatar_sha':'a','unread':False,'y':113}],np.zeros((1040,1920,3),dtype=np.uint8),'frame')))
            stack.enter_context(patch('watch.select',return_value=False))
            seek=stack.enter_context(patch('watch.seek_cold_page',side_effect=AssertionError('deep navigation')))
            route=stack.enter_context(patch('watch.ready_page_hint',return_value=99))
            stack.enter_context(patch('watch.quiet_scan_allowed',return_value=False))
            report=stack.enter_context(patch('watch.progress'))
            result=watch(runtime,root,'visible-test',cycles=1,max_pages=100)
            self.assertEqual(result['cycles'],1)
            seek.assert_not_called();route.assert_not_called();cleanup.run_one.assert_not_called();greeting.process_one.assert_not_called()
            data=report.call_args.args[1]
            self.assertEqual(data['polling_scope'],'main_visible')
            self.assertEqual(data['cold_page'],0)
            # Daytime still gives this visible chat a baseline attempt.
            self.assertEqual(result['identity_rejections'],1)
            self.assertTrue(data['visible_proactive_reads_enabled'])

            runtime.capture.return_value=(np.zeros((1040,1920,3),dtype=np.uint8),'frame')
            runtime.capture_image.return_value=runtime.capture.return_value[0]
            stack.enter_context(patch('watch.read_rows',return_value=([{'name':'group','key':'g','avatar_sha':'a','unread':False,'y':113}],np.zeros((1040,1920,3),dtype=np.uint8),'frame')))
            stack.enter_context(patch('watch.select',return_value=True))
            stack.enter_context(patch('watch.read_title',return_value='group(4)'))
            stack.enter_context(patch('text_copy.read_messages',return_value=([],[])))
            stack.enter_context(patch('sender_ocr.refine_group_senders',return_value=[]))
            stack.enter_context(patch('sender_directory.SenderDirectory.resolve_visible',side_effect=ValueError('MESSAGE_MOVED_DURING_PROFILE_CHECK')))
            result=watch(runtime,root,'visible-identity-test',cycles=1,max_pages=100)
            self.assertEqual(result['identity_rejections'],1)
            self.assertEqual(report.call_args.args[1]['status'],'running')
            db=Store(root/'data/wechat.sqlite3')
            self.assertEqual(db.db.execute("SELECT count(*) FROM events WHERE kind='polling_sender_identity_review'").fetchone()[0],1)
            self.assertEqual(db.db.execute('SELECT count(*) FROM reply_outbox').fetchone()[0],0)
            db.db.close()
