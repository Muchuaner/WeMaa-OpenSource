import unittest
import json
from unittest.mock import patch
import urllib.error
import time
import tempfile
import threading
from pathlib import Path
from store import Store

from admin_agent import Agent, execute, result_receipt, MAX_RESULT_BYTES


class AgentTests(unittest.TestCase):
    def test_oversized_uncertain_receipt_clears_delivery_block_without_replaying_command(self):
        value=self.agent();cid='a'*32
        detail={'status':'uncertain','reason':'send_postcondition_not_verified','conversation_id':'b'*64,
                'evidence':{'title_verified':True,'outgoing_verified':False,'profile_verified':True},
                'diagnostic':{'send_click_attempted':True,'draft_retained':False,
                              'history_before':[{'text':'历史消息'*15000}],
                              'after_payloads':[{'payloads':[{'text':'完整本地证据'}]}]}}
        original=json.dumps(detail,ensure_ascii=False)
        value.state['commands'][cid]={'status':'failed','detail':detail,'reported':False,
                                     'delivery_error':'RESULT_HTTP_413'}
        bodies=[]
        def api(path,body,method):
            self.assertEqual(path,'/commands/'+cid+'/result')
            self.assertLessEqual(len(json.dumps(body,ensure_ascii=False,separators=(',',':')).encode()),MAX_RESULT_BYTES)
            bodies.append(body)
        value.api=api
        with patch('admin_agent.execute') as execute_command:
            value.deliver_results();value.deliver_results()
        execute_command.assert_not_called()
        self.assertEqual(len(bodies),1)
        self.assertTrue(value.state['commands'][cid]['reported'])
        self.assertNotIn('delivery_error',value.state['commands'][cid])
        self.assertEqual(bodies[0]['status'],'failed')
        self.assertEqual(bodies[0]['detail']['status'],'uncertain')
        self.assertEqual(bodies[0]['detail']['evidence'],detail['evidence'])
        self.assertTrue(bodies[0]['detail']['diagnostic']['send_click_attempted'])
        self.assertEqual(json.dumps(value.state['commands'][cid]['detail'],ensure_ascii=False),original)
        self.assertTrue(bodies[0]['detail']['diagnostic_archive']['full_detail_retained'])

    def test_bounded_existing_receipt_is_unchanged_and_large_verified_send_preserves_api_gates(self):
        detail={'status':'sent','reason':'empty_draft_and_new_exact_outgoing_verified',
                'evidence':{'title_verified':True,'outgoing_verified':True,'profile_verified':True},
                'diagnostic':{'send_click_attempted':True,'draft_retained':False}}
        self.assertEqual(result_receipt('succeeded',detail),{'status':'succeeded','detail':detail})
        detail['diagnostic']['history_before']=['x'*100000]
        body=result_receipt('succeeded',detail)
        self.assertEqual(body['status'],'succeeded')
        self.assertEqual(body['detail']['evidence'],detail['evidence'])
        self.assertEqual(body['detail']['diagnostic'],{'send_click_attempted':True,'draft_retained':False})

    def test_contact_send_is_deferred_to_existing_gui_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'data').mkdir()
            command={'id':'a'*32,'run_id':'maa-web-aaaaaaaa','action':'send_contact_message',
                     'params':{'contact_id':'wx:test_user','wechat_id':'test_user','text':'你好'},'expires_at':time.time()+60}
            state={'old_gateway_state':'Disabled','worker_state':'Running','trial_state':'Ready'}
            with patch('admin_agent.ROOT',root),patch('admin_agent.runtime_state',return_value=state),patch('admin_agent.powershell') as launcher:
                self.assertTrue(execute(command)['deferred_gui']);launcher.assert_not_called()
                execute(command)
            store=Store(root/'data/wechat.sqlite3')
            self.assertEqual(store.db.execute('SELECT count(*) FROM gui_commands').fetchone()[0],1)
            store.db.close()
    def test_picture_sync_failure_does_not_starve_command_intake(self):
        value=self.agent()
        with patch.object(value,'deliver_results'),patch.object(value,'command') as intake,patch.object(value,'sync',side_effect=OSError('picture store busy')),patch('admin_agent.atomic_json'):
            with self.assertRaises(RuntimeError): value.run(once=True)
        intake.assert_called_once()
    def test_gui_result_is_not_reported_as_sent_until_owner_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'data').mkdir()
            command={'id':'a'*32,'run_id':'maa-web-aaaaaaaa','action':'send_message','params':{'conversation_id':'b'*64,'text':'你好'},'expires_at':time.time()+60}
            value=self.agent();calls=[]
            value.api=lambda path,*args: command if path=='/commands/next' else calls.append((path,args))
            state={'old_gateway_state':'Disabled','worker_state':'Running','trial_state':'Ready'}
            with patch('admin_agent.ROOT',root),patch('admin_agent.runtime_state',return_value=state):
                value.command();value.deliver_results()
                self.assertEqual(value.state['commands'][command['id']]['status'],'pending_gui')
                self.assertEqual(calls,[])
                store=Store(root/'data'/'wechat.sqlite3')
                store.gui_result(command['id'],'succeeded',{'status':'sent','conversation_id':'b'*64});store.db.close()
                value.deliver_results()
                self.assertEqual(calls[0][1][0]['detail']['status'],'sent')
                self.assertTrue(value.state['commands'][command['id']]['reported'])
    def test_asset_failure_does_not_block_status_and_events(self):
        value=self.agent(); value.assets=object(); calls=[]
        report={'runtime':{},'events':[{'kind':'chat_observed'}], 'conversations':[{'avatar_asset':'a'*64,'latest_snapshot':{'frame_asset':'b'*64}}]}
        value.state['uploaded']=['a'*64]
        def api(path,body,method):
            calls.append((path,body))
            if path=='/assets': raise OSError('storage busy')
        value.api=api
        with patch('admin_agent.runtime_state',return_value={}), patch('admin_agent.export',return_value=(report,{'b'*64:b'png'})):
            with self.assertRaises(OSError): value.sync()
        self.assertEqual(calls[0][0],'/telemetry')
        self.assertTrue(calls[0][1]['runtime']['asset_upload_pending'])
        self.assertEqual(calls[0][1]['conversations'][0]['avatar_asset'],'a'*64)
        self.assertIsNone(calls[0][1]['conversations'][0]['latest_snapshot']['frame_asset'])
        self.assertEqual(calls[0][1]['events'],report['events'])
    def agent(self):
        value = Agent.__new__(Agent)
        value.state = {'commands': {}, 'uploaded': []}
        value.state_lock = threading.RLock()
        value.sync_status = {}
        value.save = lambda: None
        return value

    def test_slow_telemetry_upload_does_not_delay_control_intake(self):
        value=self.agent();started=threading.Event();release=threading.Event();stop=threading.Event()
        observed=[]
        def slow_sync():
            started.set()
            self.assertTrue(release.wait(3))
        def command():
            self.assertTrue(started.wait(1))
            observed.append('command')
            if len(observed)==3:
                release.set();stop.set()
        with patch.object(value,'sync',side_effect=slow_sync), \
             patch.object(value,'deliver_results'),patch.object(value,'command',side_effect=command), \
             patch('admin_agent.atomic_json'),patch('admin_agent.CONTROL_INTERVAL_SECONDS',.01):
            value.run(stop_event=stop)
        self.assertEqual(len(observed),3)

    def test_background_asset_commit_preserves_concurrent_command_journal(self):
        value=self.agent();value.assets=object()
        def api(path,body,method):
            if path=='/assets':
                with value.state_lock:
                    value.state['commands']['concurrent']={'status':'pending_gui','reported':False}
        value.api=api
        with patch('admin_agent.runtime_state',return_value={}), \
             patch('admin_agent.export',return_value=({'runtime':{}},{'a'*64:b'png'})):
            value.sync()
        self.assertEqual(value.state['commands']['concurrent']['status'],'pending_gui')
        self.assertIn('a'*64,value.state['uploaded'])

    def test_asset_backlog_is_bounded_and_unuploaded_references_stay_hidden(self):
        value=self.agent();value.assets=object();calls=[]
        images={format(i,'064x'):b'png' for i in range(45)}
        report={'runtime':{},'conversations':[{'avatar_asset':sha} for sha in images]}
        value.api=lambda path,body,method:calls.append((path,body))
        with patch('admin_agent.runtime_state',return_value={}), \
             patch('admin_agent.export',return_value=(report,images)):
            value.sync()
        batches=[body for path,body in calls if path=='/assets']
        self.assertEqual(len(batches),1)
        self.assertEqual(len(batches[0]['items']),40)
        final=calls[-1][1]
        self.assertTrue(final['runtime']['asset_upload_pending'])
        self.assertEqual(sum(x['avatar_asset'] is None for x in final['conversations']),5)
        self.assertEqual(len(value.state['uploaded']),40)

    def test_expired_server_result_does_not_wedge_delivery(self):
        value = self.agent()
        cid = 'a' * 32
        value.state['commands'][cid] = {'status': 'succeeded', 'detail': {}, 'reported': False}
        value.api = lambda *args: (_ for _ in ()).throw(urllib.error.HTTPError('https://example.invalid', 404, '', {}, None))
        value.deliver_results()
        self.assertTrue(value.state['commands'][cid]['reported'])
        self.assertIn('404', value.state['commands'][cid]['delivery_error'])

    def test_transient_result_error_retains_retryable_result(self):
        value = self.agent()
        cid = 'a' * 32
        value.state['commands'][cid] = {'status': 'succeeded', 'detail': {}, 'reported': False}
        value.api = lambda *args: (_ for _ in ()).throw(urllib.error.HTTPError('https://example.invalid', 503, '', {}, None))
        with self.assertRaises(urllib.error.HTTPError):
            value.deliver_results()
        self.assertFalse(value.state['commands'][cid]['reported'])

    def test_command_is_journaled_before_execution_and_not_repeated(self):
        value = self.agent()
        command = {'id': 'a' * 32, 'action': 'capture', 'run_id': 'maa-web-aaaaaaaaaaaaaaaa'}
        value.api = lambda path, *args: command if path == '/commands/next' else {}
        def execute(_):
            self.assertEqual(value.state['commands'][command['id']]['status'], 'executing')
            return {'verified': True}
        with patch('admin_agent.execute', side_effect=execute) as mocked:
            value.command()
            value.command()
            self.assertEqual(mocked.call_count, 1)
        self.assertEqual(value.state['commands'][command['id']]['status'], 'succeeded')

    def test_reuse_and_resume_pass_only_validated_launcher_arguments(self):
        state = {'old_gateway_state': 'Disabled', 'worker_state': 'Ready', 'trial_state': 'Missing'}
        for key, flag in [('reuse_scan', '-ReuseScan'), ('scan_id', '-ResumeScan')]:
            command = {'id': 'a' * 32, 'run_id': 'maa-web-aaaaaaaaaaaaaaaa', 'expires_at': time.time() + 90,
                       'action': 'scan_contacts', 'params': {'scope': 'enterprise', key: 'b' * 32}}
            with patch('admin_agent.runtime_state', return_value=state), patch('admin_agent.powershell') as launcher:
                execute(command)
                argv = launcher.call_args.args[1]
                self.assertEqual(argv[-2:], [flag, 'b' * 32])
                self.assertEqual(argv[argv.index('-Scope') + 1], 'enterprise')

    def test_reuse_resume_exclusive_and_other_actions_reject_scan_parameters(self):
        base = {'id': 'a' * 32, 'run_id': 'maa-web-aaaaaaaaaaaaaaaa', 'expires_at': time.time() + 90}
        for action, params in [('scan_contacts', {'reuse_scan': 'b' * 32, 'scan_id': 'c' * 32}),
                               ('start_watch', {'reuse_scan': 'b' * 32}), ('capture', {'scope': 'public'}),
                               ('scan_contacts', {'reuse_scan': 'invalid'})]:
            with patch('admin_agent.powershell') as launcher:
                with self.assertRaises(ValueError):
                    execute({**base, 'action': action, 'params': params})
                launcher.assert_not_called()


if __name__ == '__main__':
    unittest.main()
