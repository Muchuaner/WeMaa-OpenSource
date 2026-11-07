import unittest
from unittest.mock import Mock, patch
from watch_recovery import run_watch_with_recovery, ContinuousActivation


class WatchRecoveryTests(unittest.TestCase):
    def test_window_reentry_preserves_activation_and_verified_baselines(self):
        activation = ContinuousActivation()
        baselines, attempted = {'private'}, {'private-route'}
        observations = []
        def execute(runtime):
            now = 100 + 30 * len(observations)
            resumed = activation.restore('same-worker', now)
            if resumed is None:
                activation.remember('same-worker', now, baselines, attempted)
                baselines.add('verified-group')
                attempted.add('group-route')
                observations.append(now)
                raise RuntimeError('LAYOUT_CHANGED_STOP')
            enabled_at, verified, routes = resumed
            observations.append(enabled_at)
            self.assertIn('verified-group', verified)
            self.assertIn('group-route', routes)
            # A message first seen during the outage belongs to the original
            # activation, instead of becoming startup history at time 130.
            self.assertGreater(115, enabled_at)
            return 'resumed'
        with patch('watch_recovery.check_frame', return_value=480):
            self.assertEqual(run_watch_with_recovery(Mock(), execute, Mock(), sleep=Mock()), 'resumed')
        self.assertEqual(observations, [100, 100])

    def test_new_process_or_different_run_cannot_inherit_old_activation(self):
        old = ContinuousActivation()
        old.remember('old', 100, {'group'}, {'route'})
        self.assertIsNone(old.restore('new', 130))
        self.assertIsNone(old.restore('old', 90))
        self.assertIsNone(ContinuousActivation().restore('old', 130))

    def test_window_failure_waits_for_two_consecutive_valid_probes(self):
        first = Mock()
        bad = Mock()
        good1, good2, good3 = Mock(), Mock(), Mock()
        factory = Mock(side_effect=[first, bad, good1, good2, good3])
        execute = Mock(side_effect=[RuntimeError('LAYOUT_CHANGED_STOP'), 'finished'])
        notify, sleep = Mock(), Mock()
        good4 = Mock()
        factory.side_effect = [first, bad, good1, good2, good3, good4]
        with patch('watch_recovery.check_frame', side_effect=[
                ValueError('DIVIDER_NOT_VERIFIABLE'), 480,
                ValueError('DIVIDER_CHANGED:490'), 480, 480]):
            result = run_watch_with_recovery(factory, execute, notify, sleep=sleep)
        self.assertEqual(result, 'finished')
        self.assertEqual(execute.call_count, 2)
        self.assertIs(execute.call_args.args[0], good4)
        self.assertEqual(sleep.call_count, 5)
        self.assertEqual(notify.call_args.args[0]['stable_checks'], 2)
        for call in notify.call_args_list:
            self.assertFalse(call.args[0]['sending_enabled'])
        for candidate in [bad, good1, good2, good3, good4]:
            candidate.capture_image.assert_called_once()
            candidate.node.assert_not_called()

    def test_initial_minimized_or_absent_window_can_recover(self):
        one, two = Mock(), Mock()
        factory = Mock(side_effect=[RuntimeError('WECHAT_WINDOW_NOT_UNIQUE:0'), one, two])
        execute = Mock(return_value='done')
        with patch('watch_recovery.check_frame', return_value=480):
            self.assertEqual(run_watch_with_recovery(factory, execute, Mock(), sleep=Mock()), 'done')
        execute.assert_called_once_with(two)

    def test_unrelated_and_identity_errors_are_not_restarted(self):
        for reason in ['MAA_NODE_FAILED:SelectRecentRow', 'WECHAT_WINDOW_NOT_UNIQUE:2', 'CHAT_TITLE_MISMATCH']:
            with self.subTest(reason=reason):
                execute = Mock(side_effect=RuntimeError(reason))
                notify = Mock()
                with self.assertRaisesRegex(RuntimeError, reason):
                    run_watch_with_recovery(Mock(), execute, notify, sleep=Mock())
                execute.assert_called_once()
                notify.assert_not_called()

    def test_unknown_probe_error_is_terminal(self):
        runtime = Mock()
        runtime.capture_image.side_effect = RuntimeError('RESOURCE_LOAD_FAILED')
        with self.assertRaisesRegex(RuntimeError, 'RESOURCE_LOAD_FAILED'):
            run_watch_with_recovery(Mock(return_value=runtime),
                Mock(side_effect=RuntimeError('MAA_BLACK_FRAME')), Mock(), sleep=Mock())

    def test_durable_send_started_is_not_claimed_again_after_recovery(self):
        state = {'status': 'ready', 'clicks': 0}
        def execute(runtime):
            if state['status'] == 'send_started':
                state['status'] = 'uncertain'
                return state['status']
            state.update(status='send_started', clicks=1)
            raise RuntimeError('MAA_CAPTURE_DIMENSIONS:(1040, 1918, 3)')
        with patch('watch_recovery.check_frame', return_value=480):
            self.assertEqual(run_watch_with_recovery(Mock(), execute, Mock(), sleep=Mock()), 'uncertain')
        self.assertEqual(state['clicks'], 1)
