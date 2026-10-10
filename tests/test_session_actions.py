import sys
import threading
import base64
import json
import subprocess
import contextlib
import xml.etree.ElementTree as ET
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.session_actions import SleepGuard, CompletionActions, ES_CONTINUOUS, ES_SYSTEM_REQUIRED, notify_success, notify_failure, send_windows_toast, _run_power_action


class SessionActionTests(unittest.TestCase):
    def test_native_power_signatures_preserve_unsaved_apps_and_wake_events(self):
        library = mock.Mock()
        library.InitiateSystemShutdownExW.return_value = 1
        library.AbortSystemShutdownW.return_value = 1
        library.SetSuspendState.return_value = 1
        with mock.patch('core.session_actions.ctypes.WinDLL', return_value=library), \
             mock.patch('core.session_actions._shutdown_privilege', side_effect=lambda: contextlib.nullcontext()):
            _run_power_action('shutdown')
            _run_power_action('abort_shutdown')
            _run_power_action('sleep')
        library.InitiateSystemShutdownExW.assert_called_once_with(
            None, 'AI Music Processor: confirmed batch completed', 60, False, False, 0x80040000)
        library.AbortSystemShutdownW.assert_called_once_with(None)
        library.SetSuspendState.assert_called_once_with(False, False, False)

    def test_toast_uses_own_identity_and_escaped_xml_with_honest_receipt(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({
            'status': 'SUBMITTED', 'app_id': 'AI.Music.Processor', 'history_confirmed': True}), ''))
        receipt = send_windows_toast('<title>&', 'a "song" & $() text', runner=runner)
        self.assertTrue(receipt['history_confirmed'])
        command = runner.call_args.args[0]
        env = runner.call_args.kwargs['env']
        xml = ET.fromstring(base64.b64decode(env['AI_MUSIC_PROCESSOR_TOAST_XML']))
        self.assertEqual([element.text for element in xml.findall('.//text')], ['<title>&', 'a "song" & $() text'])
        self.assertNotIn('a "song"', command[-1])
        self.assertIn('AI.Music.Processor', command[-1])
        runner.return_value = subprocess.CompletedProcess([], 1, '', 'platform failure')
        self.assertEqual(send_windows_toast('title', 'message', runner=runner)['status'], 'FAILED')
        local, notifier = mock.Mock(), mock.Mock(return_value={'status': 'FAILED', 'history_confirmed': False})
        self.assertTrue(notify_failure(local, toast=True, notifier=notifier)['ui_notified'])
        notifier.assert_called_once()

    def test_sleep_guard_is_opt_in_restores_same_thread_after_error(self):
        api = mock.Mock(return_value=ES_CONTINUOUS)
        with SleepGuard(False, api):
            pass
        api.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'processing failed'):
            with SleepGuard(True, api) as guard:
                self.assertTrue(guard.active)
                raise RuntimeError('processing failed')
        self.assertFalse(guard.active)
        self.assertEqual(api.call_args_list, [mock.call(ES_CONTINUOUS | ES_SYSTEM_REQUIRED), mock.call(ES_CONTINUOUS)])
        with self.assertRaises(OSError):
            with SleepGuard(True, mock.Mock(return_value=0)):
                pass

    def test_sleep_guard_refuses_restoration_from_another_thread(self):
        api = mock.Mock(return_value=ES_CONTINUOUS)
        guard = SleepGuard(True, api)
        guard.__enter__()
        errors = []
        def wrong_thread():
            try:
                guard.__exit__(None, None, None)
            except RuntimeError as error:
                errors.append(str(error))
        thread = threading.Thread(target=wrong_thread)
        thread.start(); thread.join()
        self.assertTrue(errors)
        self.assertTrue(guard.active)
        guard.__exit__(None, None, None)
        self.assertEqual(api.call_count, 2)

    def test_power_action_needs_consent_exact_batch_nonce_and_all_done(self):
        policy, runner = CompletionActions(), mock.Mock()
        self.assertEqual(policy.finish({'old': 'DONE'}, 'old', runner=runner)['status'], 'DISARMED')
        with self.assertRaises(ValueError):
            policy.arm('shutdown', ['job1'], confirmed=False)
        armed = policy.arm('shutdown', ['job1', 'job2'], confirmed=True)
        nonce = armed['dispatch_nonce']
        self.assertEqual(policy.finish({'job1': 'DONE', 'job2': 'RUNNING'}, nonce, runner=runner)['status'], 'WAITING')
        done = {'job1': 'DONE', 'job2': 'DONE'}
        for flag in ('pending', 'active', 'maintenance'):
            self.assertEqual(policy.finish(done, nonce, runner=runner, **{flag: True})['status'], 'WAITING')
        self.assertEqual(policy.finish(done, nonce, runner=runner)['status'], 'SCHEDULED')
        runner.assert_called_once_with('shutdown')
        self.assertEqual(policy.finish(done, nonce, runner=runner)['status'], 'DISARMED')
        self.assertEqual(CompletionActions().finish(done, nonce, runner=runner)['status'], 'DISARMED')
        with self.assertRaises(RuntimeError):
            policy.arm('exit', ['new-job'], confirmed=True)
        self.assertEqual(policy.abort_scheduled_shutdown(runner=runner)['status'], 'ABORTED')
        self.assertEqual(runner.call_args_list, [mock.call('shutdown'), mock.call('abort_shutdown')])
        self.assertEqual(policy.abort_scheduled_shutdown(runner=runner)['status'], 'NOT_SCHEDULED')

    def test_failure_queue_changes_and_action_errors_never_auto_retry(self):
        policy, runner = CompletionActions(), mock.Mock(side_effect=RuntimeError('mock action failed'))
        for statuses, nonce_change in (({'job': 'FAILED'}, False), ({'job': 'CANCELLED'}, False),
                                       ({'job': 'DONE', 'new': 'DONE'}, False), ({'job': 'DONE'}, True)):
            nonce = policy.arm('sleep', ['job'], confirmed=True)['dispatch_nonce']
            policy.finish(statuses, 'another' if nonce_change else nonce, runner=runner)
            self.assertEqual(policy.finish({'job': 'DONE'}, nonce, runner=runner)['status'], 'DISARMED')
        runner.assert_not_called()
        nonce = policy.arm('sleep', ['job'], confirmed=True)['dispatch_nonce']
        with self.assertRaisesRegex(RuntimeError, 'mock action failed'):
            policy.finish({'job': 'DONE'}, nonce, runner=runner)
        self.assertEqual(policy.finish({'job': 'DONE'}, nonce, runner=runner)['status'], 'DISARMED')

    def test_exit_and_notification_callbacks_are_local(self):
        policy, quit_callback, notification = CompletionActions(), mock.Mock(), mock.Mock()
        nonce = policy.arm('exit', ['job'], confirmed=True)['dispatch_nonce']
        self.assertEqual(policy.finish({'job': 'DONE'}, nonce, quit_callback=quit_callback)['status'], 'EXECUTED')
        quit_callback.assert_called_once_with()
        result = notify_success(notification, beep=False)
        self.assertTrue(result['ui_notified'])
        self.assertFalse(result['sound'])
        notification.assert_called_once()


if __name__ == '__main__':
    unittest.main()
