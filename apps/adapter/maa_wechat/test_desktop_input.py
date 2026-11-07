import ctypes
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from desktop_input import InputDesktopGate, desktop_probe


def readable(session=1):
    return {'available': True, 'session_id': session, 'process_id': 100,
            'cursor_readable': True, 'input_desktop_accessible': True,
            'input_desktop_name': 'Default', 'thread_desktop_name': 'Default',
            'desktop_matches': True, 'reason': 'INPUT_DESKTOP_READABLE', 'last_error': None}


class DesktopProbeTests(unittest.TestCase):
    def probe_with(self, *, session=1, cursor_ok=True, input_ok=True,
                   input_name='Default', thread_name='Default', name_ok=True):
        errors = {'value': 0}
        def fail():
            errors['value'] = 5
            return 0
        def assign_session(pid, pointer):
            pointer._obj.value = session
            return 1
        def assign_name(handle, what, buffer, size, required):
            if not name_ok:return fail()
            buffer.value = input_name if handle == 99 else thread_name
            return 1
        user = SimpleNamespace(GetCursorPos=Mock(side_effect=lambda p: 1 if cursor_ok else fail()),
            OpenInputDesktop=Mock(side_effect=lambda *args: 99 if input_ok else fail()),
            GetThreadDesktop=Mock(return_value=55), GetUserObjectInformationW=Mock(side_effect=assign_name),
            CloseDesktop=Mock(return_value=1))
        kernel = SimpleNamespace(GetCurrentThreadId=Mock(return_value=42),
                                  ProcessIdToSessionId=Mock(side_effect=assign_session))
        with patch('desktop_input.sys.platform', 'win32'), \
             patch.object(ctypes, 'WinDLL', side_effect=[user, kernel], create=True), \
             patch.object(ctypes, 'set_last_error', side_effect=lambda v: errors.update(value=v), create=True), \
             patch.object(ctypes, 'get_last_error', side_effect=lambda: errors['value'], create=True):
            return desktop_probe(), user, kernel

    def test_healthy_probe_is_read_only_and_contains_no_cursor_coordinates(self):
        state, user, _ = self.probe_with()
        self.assertTrue(state['available'])
        self.assertEqual(state['session_id'], 1)
        self.assertNotIn('x', state);self.assertNotIn('y', state)
        user.OpenInputDesktop.assert_called_once_with(0, False, 1)
        user.CloseDesktop.assert_called_once_with(99)

    def test_cursor_error_five_blocks_even_when_printwindow_desktop_is_readable(self):
        state, user, _ = self.probe_with(cursor_ok=False)
        self.assertFalse(state['available'])
        self.assertEqual((state['reason'], state['last_error']), ('CURSOR_ACCESS_DENIED', 5))
        user.CloseDesktop.assert_called_once_with(99)

    def test_input_desktop_access_denied_blocks_and_never_switches_desktop(self):
        state, user, _ = self.probe_with(input_ok=False)
        self.assertEqual((state['reason'], state['last_error']), ('INPUT_DESKTOP_ACCESS_DENIED', 5))
        self.assertFalse(state['available'])
        user.GetThreadDesktop.assert_not_called();user.CloseDesktop.assert_not_called()

    def test_secure_or_mismatched_desktop_never_counts_as_available(self):
        for names in [('Winlogon', 'Default'), ('Winlogon', 'Winlogon')]:
            with self.subTest(names=names):
                state, _, _ = self.probe_with(input_name=names[0], thread_name=names[1])
                self.assertFalse(state['available'])

    def test_failed_desktop_name_read_closes_its_read_only_handle(self):
        state, user, _ = self.probe_with(name_ok=False)
        self.assertFalse(state['available'])
        self.assertEqual(state['reason'], 'INPUT_DESKTOP_NAME_UNAVAILABLE')
        user.CloseDesktop.assert_called_once_with(99)

    def test_background_session_zero_is_rejected_before_cursor_probe(self):
        state, user, _ = self.probe_with(session=0)
        self.assertEqual(state['reason'], 'INPUT_WORKER_IN_SESSION_ZERO')
        user.GetCursorPos.assert_not_called();user.OpenInputDesktop.assert_not_called()


class InputGateTests(unittest.TestCase):
    def setUp(self):
        self.runtime = SimpleNamespace(capture_image=Mock(return_value='image'), node=Mock())

    def test_two_consecutive_same_session_layout_checks_required(self):
        gate = InputDesktopGate(probe=Mock(return_value=readable()), clock=lambda: 100)
        with patch('layout.check_frame', return_value=480):
            first = gate.check(self.runtime, 'run')
            second = gate.check(self.runtime, 'run')
            third = gate.check(self.runtime, 'run')
        self.assertFalse(first['ready']);self.assertTrue(second['ready']);self.assertTrue(third['ready'])
        self.assertEqual((second['run_id'], second['checked_at'], second['stable_checks']), ('run', 100, 2))
        self.assertEqual(self.runtime.capture_image.call_count, 2)
        self.runtime.node.assert_not_called()

    def test_lock_invalidates_ready_and_unlock_requires_two_fresh_checks(self):
        locked = dict(readable(), available=False, cursor_readable=False, last_error=5, reason='CURSOR_ACCESS_DENIED')
        gate = InputDesktopGate(probe=Mock(side_effect=[readable(), readable(), locked, readable(), readable()]))
        with patch('layout.check_frame', return_value=480):
            states = [gate.check(self.runtime, 'run') for _ in range(5)]
        self.assertEqual([s['ready'] for s in states], [False, True, False, False, True])
        self.assertEqual(states[2]['stable_checks'], 0)
        self.assertEqual(self.runtime.capture_image.call_count, 4)

    def test_session_change_and_layout_failure_break_consecutive_confirmation(self):
        gate = InputDesktopGate(probe=Mock(side_effect=[readable(1), readable(2), readable(2), readable(2)]))
        with patch('layout.check_frame', side_effect=[480, ValueError('DIVIDER_CHANGED'), 480, 480]):
            states = [gate.check(self.runtime, 'run') for _ in range(4)]
        self.assertEqual([s['ready'] for s in states], [False, False, False, True])
        self.assertEqual(states[1]['reason'], 'INPUT_LAYOUT_NOT_VERIFIED')
        self.runtime.node.assert_not_called()

    def test_unknown_probe_failure_fails_closed_without_gui_or_capture(self):
        gate = InputDesktopGate(probe=Mock(side_effect=OSError('unavailable')))
        state = gate.check(self.runtime, 'run')
        self.assertFalse(state['ready'])
        self.assertEqual(state['reason'], 'INPUT_DESKTOP_PROBE_FAILED')
        self.runtime.capture_image.assert_not_called();self.runtime.node.assert_not_called()

    def test_geometry_repair_invalidates_ready_until_two_new_frames_pass(self):
        self.runtime.prepare_input = Mock(side_effect=[None, None, {'restored': True}, None, None])
        gate = InputDesktopGate(probe=Mock(return_value=readable()))
        with patch('layout.check_frame', return_value=480):
            states = [gate.check(self.runtime, 'run') for _ in range(5)]
        self.assertEqual([s['ready'] for s in states], [False, True, False, True, True])
        self.assertEqual(self.runtime.capture_image.call_count, 4)
        self.runtime.node.assert_not_called()

    def test_failed_repair_blocks_a_previously_ready_worker(self):
        self.runtime.prepare_input = Mock(side_effect=[None, None, RuntimeError('WECHAT_WINDOW_IDENTITY_CHANGED')])
        gate = InputDesktopGate(probe=Mock(return_value=readable()))
        with patch('layout.check_frame', return_value=480):
            states = [gate.check(self.runtime, 'run') for _ in range(3)]
        self.assertFalse(states[2]['ready'])
        self.assertEqual(states[2]['layout_error'], 'WECHAT_WINDOW_IDENTITY_CHANGED')
        self.runtime.node.assert_not_called()

    def test_secure_desktop_never_calls_geometry_repair(self):
        self.runtime.prepare_input = Mock()
        gate = InputDesktopGate(probe=Mock(return_value=dict(readable(), available=False)))
        self.assertFalse(gate.check(self.runtime, 'run')['ready'])
        self.runtime.prepare_input.assert_not_called()

    def test_dismissed_storage_modal_requires_two_new_layout_checks(self):
        self.runtime.prepare_input = Mock(side_effect=[None, None,
            {'restored': True, 'storage_dialog_dismissed': True}, None])
        gate = InputDesktopGate(probe=Mock(return_value=readable()))
        with patch('layout.check_frame', return_value=480):
            states = [gate.check(self.runtime, 'run') for _ in range(4)]
        self.assertEqual([s['ready'] for s in states], [False, True, False, True])
        self.runtime.node.assert_not_called()

    def test_blocked_watch_pumps_network_before_any_gui_and_resumes_once_confirmed(self):
        from watch import watch
        creation = SimpleNamespace(pump=Mock(), process_one=Mock(return_value=False),
            delivery_ready=Mock(return_value=False), delivery_in_flight=Mock(return_value=False),
            pending_deliveries=Mock(return_value=0), delivery_health=Mock(return_value={}), close=Mock())
        experiment = SimpleNamespace(pump=Mock(), process_incoming_one=Mock(return_value=False),
            process_one=Mock(), close=Mock())
        cleanup = SimpleNamespace(run_one=Mock(), enabled=Mock(return_value=False), result={})
        gate = SimpleNamespace(check=Mock(side_effect=[
            dict(readable(), ready=False, stable_checks=0, reason='CURSOR_ACCESS_DENIED'),
            dict(readable(), ready=False, stable_checks=1, reason='INPUT_DESKTOP_CONFIRMING'),
            dict(readable(), ready=True, stable_checks=2)]))
        reports = []
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);(root/'data').mkdir()
            def record(root, state):
                reports.append(state)
                if state['status'] == 'waiting_for_input':
                    self.runtime.node.assert_not_called()
                    creation.process_one.assert_not_called();experiment.process_incoming_one.assert_not_called()
            with patch('desktop_input.InputDesktopGate', return_value=gate), \
                 patch('creation_client.CreationClient', return_value=creation), \
                 patch('greeting_client.GreetingClient', return_value=experiment), \
                 patch('inactive_chats.InactiveChats', return_value=cleanup), \
                 patch('manual_gui.process_one', return_value=False), \
                 patch('watch.scroll_top'), patch('watch.read_rows', return_value=([], None, 'frame')), \
                 patch('watch.quiet_scan_allowed', return_value=False), \
                 patch('watch.progress', side_effect=record), patch('watch.time.sleep') as sleep:
                result = watch(self.runtime, root, 'run', cycles=1)
        self.assertEqual(result['cycles'], 1)
        self.assertEqual(creation.pump.call_count, 5)
        self.assertEqual(experiment.pump.call_count, 3)
        self.runtime.node.assert_called_once_with('OpenRecent')
        self.assertEqual(sleep.call_args_list[0].args, (1,))
        self.assertEqual([r['status'] for r in reports[:3]], ['waiting_for_input', 'waiting_for_input', 'running'])
        self.assertFalse(reports[0]['sending_enabled']);self.assertFalse(reports[0]['manual_commands_enabled'])


if __name__ == '__main__':
    unittest.main()
