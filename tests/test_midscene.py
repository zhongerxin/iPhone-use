import fcntl
import json
from pathlib import Path
import subprocess
import sys
import time
import threading
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from iphone_use import Runtime
from wda_client import WDAError
import wda_midscene
import wda_mode

ORIGINAL_EXECUTE_WORKER=wda_midscene.execute_worker


class MidsceneTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.runtime = Runtime(self.root, base_url='http://127.0.0.1:18123')
        self.addCleanup(self.runtime.close)
        wda_mode.settings(self.root, "steps")
        self.runtime.screen = Mock()
        self.runtime.screen.paused.return_value = False
        self.runtime.client = Mock(host='127.0.0.1', port=18123, session_id='borrowed-session')
        self.runtime.client.request.return_value = {'value': False}
        self.runtime.client.ensure_session.return_value = 'borrowed-session'
        worker = self.root / 'worker/run.mjs'
        dependency = worker.parent / 'node_modules/@midscene/ios/package.json'
        dependency.parent.mkdir(parents=True)
        dependency.write_text('{}')
        self.addCleanup(patch.stopall)
        patch.object(wda_midscene, 'WORKER', worker).start()
        patch.object(wda_midscene.shutil, 'which', return_value='/usr/bin/node').start()
        self.process = patch.object(wda_midscene, 'execute_worker', return_value=Mock(
            returncode=0, stdout='{"ok":true}')).start()

    def call(self, action='screenshot', **args):
        return self.runtime.call('pua_midscene', {'action': action, **args})

    def test_midscene_invalidates_input_on_success_and_uncertain_failure(self):
        for fails in (False, True):
            self.runtime.phone.pending_input={'token': 'old', 'mark': self.runtime.phone.accepted_actions, 'created': time.monotonic()}
            mark=self.runtime.phone.accepted_actions
            self.process.return_value=Mock(returncode=int(fails),stdout=json.dumps({'ok': not fails}))
            if fails:
                with self.assertRaises(WDAError):self.call('tap',x=1,y=1)
            else:self.call('tap',x=1,y=1)
            self.assertIsNone(self.runtime.phone.pending_input)
            with self.assertRaises(WDAError) as expired:self.runtime.phone.continue_input('old')
            self.assertEqual(expired.exception.code,'input_continuation_expired')
            self.assertEqual(self.runtime.phone.accepted_actions,mark+1)

    def test_warm_runtime_adopts_settings_and_action_invalidation(self):
        other=Runtime(self.root,base_url=self.runtime.base_url)
        self.addCleanup(other.close)
        other.screen=Mock();other.screen.paused.return_value=False
        other.client=Mock(host='127.0.0.1',port=18123,session_id='borrowed-session')
        other.client.request.return_value={'value':False}
        other.client.ensure_session.return_value='borrowed-session'
        other.phone.pending_input={'token':'old'}
        self.call('tap',x=1,y=1)
        with patch.object(other,'_call',return_value={'ok':True}):other.call('pua_observe',{'mode':'tree'})
        other.client.reapply_settings.assert_called_once()
        self.assertIsNone(other.phone.pending_input)
        self.assertEqual(other._midscene_revision,1)
        with patch.object(other,'_call',return_value={'ok':True}):other.call('pua_observe',{'mode':'tree'})
        other.client.reapply_settings.assert_called_once()  # ordinary reads do not resend settings

    def test_worker_cancels_for_cross_runtime_authentication_pause(self):
        # Exercise the real child-process monitor, not the mocked worker entry.
        worker=self.root/'slow.py';worker.write_text('import time; time.sleep(20)')
        event=threading.Event()
        self.runtime.screen.paused.side_effect=event.is_set
        timer=threading.Timer(.2,event.set);timer.start();self.addCleanup(timer.cancel)
        start=time.monotonic()
        with self.assertRaises(WDAError) as error:
            ORIGINAL_EXECUTE_WORKER([sys.executable,str(worker)],runtime=self.runtime,action='act',input='{}',timeout=20,text=True,cwd=self.root)
        self.assertEqual(error.exception.code,'preview_paused')
        self.assertTrue(error.exception.uncertain)
        self.assertLess(time.monotonic()-start,2)

    def test_default_without_model_configuration_and_legacy_env_not_forwarded(self):
        (self.root / 'midscene.json').write_text('{"enabled":false,"env":{"MIDSCENE_MODEL_API_KEY":"legacy-secret"}}')
        with patch.dict(wda_midscene.os.environ, {'MIDSCENE_MODEL_API_KEY': 'legacy-secret'}):
            self.assertTrue(self.call()['ok'])
        kwargs = self.process.call_args.kwargs
        self.assertNotIn('MIDSCENE_MODEL_API_KEY', kwargs['env'])
        self.assertNotIn('legacy-secret', kwargs['input'])

    def test_invalid_actions_and_arguments_never_contact_phone(self):
        invalid = [('act', {'prompt': 'task'}), ('tap', {'x': 5}),
                   ('screenshot', {'text': 'unexpected'}), ('input', {'text': 'send\n'}),
                   ('launch', {'text': 'https://example.com'}), ('record', {'text': 'done'}),
                   ('tap', {'x': -1, 'y': 1}), ('screenshot', {'planning': 'compact'}),
                   ('act', {'text': 'task', 'planning': 'unknown'})]
        for action, args in invalid:
            with self.subTest(action=action, args=args), self.assertRaises(WDAError):
                self.call(action, **args)
        self.process.assert_not_called()
        self.runtime.client.request.assert_not_called()

    def test_planning_profile_is_explicit_and_default_is_preserved(self):
        wda_mode.settings(self.root, 'ai')
        with patch.object(wda_midscene.wda_chatgpt, 'run', return_value={'authorized': True}):
            self.call('act', text='Read a value', planning='compact')
            request = json.loads(self.process.call_args.kwargs['input'])
            self.assertEqual(request['planning'], 'compact')
            self.assertEqual(request['args'], {'text': 'Read a value'})
            self.call('act', text='Edit an event')
            self.assertNotIn('planning', json.loads(self.process.call_args.kwargs['input']))

    def test_wait_uses_ai_worker_and_bounded_timeout(self):
        with self.assertRaises(WDAError) as error:
            self.call('wait', text='Ready')
        self.assertEqual(error.exception.code, 'midscene_ai_disabled')
        self.runtime.client.request.assert_not_called()
        wda_mode.settings(self.root, 'ai')
        with patch.object(wda_midscene.wda_chatgpt, 'run', return_value={'authorized': True}):
            for timeout in (15000, 1000, 60000):
                self.call('wait', text='Ready', **({} if timeout == 15000 else {'timeout_ms': timeout}))
                self.assertTrue(self.process.call_args.args[0][1].endswith('run-ai.mjs'))
                self.assertEqual(json.loads(self.process.call_args.kwargs['input'])['args']['timeout_ms'], timeout)
            self.process.return_value = Mock(returncode=1, stdout='{"ok":false,"error":"wait_timeout"}')
            with self.assertRaises(WDAError) as error:
                self.call('wait', text='Ready')
            self.assertFalse(error.exception.uncertain)
            self.assertEqual(error.exception.details['reason'], 'wait_timeout')

    def test_wait_invalid_arguments_never_contact_phone(self):
        for args in ({'text': ''}, {'text': 'Ready', 'timeout_ms': 999},
                     {'text': 'Ready', 'timeout_ms': 60001}, {'text': 'Ready', 'timeout_ms': True},
                     {'text': 'Ready', 'planning': 'compact'}, {'text': 'Ready', 'x': 1}):
            with self.subTest(args=args), self.assertRaises(WDAError):
                self.call('wait', **args)
        self.runtime.client.request.assert_not_called()
        self.process.assert_not_called()

    def test_missing_dependency_without_phone_access(self):
        (wda_midscene.WORKER.parent / 'node_modules/@midscene/ios/package.json').unlink()
        with self.assertRaises(WDAError) as error:
            self.call()
        self.assertEqual(error.exception.code, 'midscene_not_installed')
        self.runtime.client.request.assert_not_called()

    def test_shared_session_and_cross_process_lock(self):
        def execute(command, **kwargs):
            with (self.root / 'operation.lock').open('a') as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            request = json.loads(kwargs['input'])
            self.assertEqual((request['host'], request['port'], request['sessionId']),
                             ('127.0.0.1', 18123, 'borrowed-session'))
            self.assertEqual(kwargs['cwd'], self.root)
            return Mock(returncode=0, stdout='{"ok":true}')
        self.process.side_effect = execute
        self.call()
        self.runtime.client.reapply_settings.assert_called_once_with()

    def test_report_id_generated_forwarded_and_validated(self):
        result = self.call()
        self.assertRegex(result['report_id'], r'^[a-f0-9]{32}$')
        self.assertEqual(json.loads(self.process.call_args.kwargs['input'])['reportId'], result['report_id'])
        self.assertEqual(self.call(report_id='task-1')['report_id'], 'task-1')
        self.process.reset_mock()
        for invalid in ('../outside', 'a/b', 'a.b', '汉字', 'a' * 65, ''):
            with self.assertRaises(WDAError):
                self.call(report_id=invalid)
        self.process.assert_not_called()

    def test_busy_paused_and_locked_refuse_worker(self):
        with (self.root / 'operation.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaises(WDAError) as error:
                self.call()
            self.assertEqual(error.exception.code, 'device_busy')
        self.runtime.screen.paused.return_value = True
        with self.assertRaises(WDAError) as error:
            self.call()
        self.assertEqual(error.exception.code, 'preview_paused')
        self.runtime.screen.paused.return_value = False
        self.runtime.client.request.return_value = {'value': True}
        with self.assertRaises(WDAError) as error:
            self.call()
        self.assertEqual(error.exception.code, 'phone_locked')
        self.process.assert_not_called()

    def test_timeout_uncertain_for_mutation_no_replay(self):
        self.process.side_effect = subprocess.TimeoutExpired('node', 60, stderr='secret')
        with self.assertRaises(WDAError) as error:
            self.call('tap', x=1, y=2)
        self.assertTrue(error.exception.uncertain)
        self.assertNotIn('secret', str(error.exception))
        self.process.assert_called_once()
        self.runtime.client.reapply_settings.assert_called_once_with()

    def test_failed_host_verification_preserves_report(self):
        self.process.return_value = Mock(returncode=1, stdout='{"ok":false,"report":"/local/report.html","model_requests":[{"ok":false,"total_ms":42}]}')
        with self.assertRaises(WDAError) as error:
            self.call('record', text='Expected page missing', passed=False, report_id='task-1')
        self.assertFalse(error.exception.uncertain)
        self.assertEqual(error.exception.details['report_id'], 'task-1')
        self.assertEqual(error.exception.details['report'], '/local/report.html')
        self.assertEqual(error.exception.details['model_requests'], [{'ok': False, 'total_ms': 42}])

    def test_worker_is_shipped_without_node_modules(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import package
        files = {str(rel) for _, rel in package.package_files()}
        self.assertTrue({'server/midscene/run.mjs', 'server/midscene/package.json',
                         'server/midscene/package-lock.json', 'server/wda_midscene.py'} <= files)
        self.assertFalse(any('node_modules' in name for name in files))

    def test_ai_requires_consent_before_phone_access_and_never_uses_api_key(self):
        self.call("settings", mode="ai")
        with patch.object(wda_midscene.wda_chatgpt, 'run', return_value={'authorized': False}):
            with self.assertRaises(WDAError) as error:
                self.call('assert', text='Expected page')
        self.assertEqual(error.exception.code, 'chatgpt_sign_in_required')
        self.runtime.client.request.assert_not_called()
        with patch.object(wda_midscene.wda_chatgpt, 'run', return_value={'authorized': True}), \
                patch.dict(wda_midscene.os.environ, {'OPENAI_API_KEY': 'must-not-forward'}):
            self.call('act', text='Open About')
        self.assertTrue(self.process.call_args.args[0][1].endswith('run-ai.mjs'))
        self.assertEqual(self.process.call_args.kwargs['timeout'], 330)
        self.assertNotIn('OPENAI_API_KEY', self.process.call_args.kwargs['env'])
        self.assertEqual(len(self.process.call_args.kwargs['pass_fds']), 1)

    def test_authorization_tools_work_without_phone_or_device_lock(self):
        with patch.object(wda_midscene.wda_chatgpt, 'run', return_value={'ok': True, 'authorized': False}) as auth:
            with (self.root / 'operation.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self.assertFalse(self.call('auth_status')['authorized'])
            auth.assert_called_once_with(self.root, 'auth_status')
            with self.assertRaises(WDAError):
                self.call('auth_login', report_id='unexpected')
        self.runtime.client.request.assert_not_called()

    def test_preference_defaults_steps_and_explicit_off_blocks_even_if_authorized(self):
        (self.root / 'execution-mode.json').unlink()
        self.assertEqual(self.call('settings')['mode'], 'steps')
        with patch.object(wda_midscene.wda_chatgpt, 'run', return_value={'ok': True, 'authorized': True}):
            self.call('auth_status')
        self.assertEqual(self.call('settings')['mode'], 'steps')
        self.call('settings', mode='off')
        self.assertEqual(self.call('settings')['mode'], 'off')
        with self.assertRaises(WDAError) as error:
            self.call()
        self.assertEqual(error.exception.code, 'midscene_disabled')
        self.runtime.client.request.assert_not_called()
        self.process.assert_not_called()
        with patch.object(wda_midscene.wda_chatgpt, 'run', return_value={'ok': True, 'authorized': True}):
            self.call('auth_status')
        self.assertEqual(self.call('settings')['mode'], 'off')

    def test_steps_blocks_ai_before_auth_or_device_and_settings_persist(self):
        self.assertEqual(self.call('settings', mode='steps')['mode'], 'steps')
        with patch.object(wda_midscene.wda_chatgpt, 'run') as auth:
            with self.assertRaises(WDAError) as error:
                self.call('act', text='Open About')
            self.assertEqual(error.exception.code, 'midscene_ai_disabled')
            auth.assert_not_called()
        self.runtime.client.request.assert_not_called()
        self.call('settings', mode='ai')
        other = Runtime(self.root, base_url='http://127.0.0.1:18123')
        try:
            self.assertEqual(other.call('pua_midscene', {'action': 'settings'})['mode'], 'ai')
        finally:
            other.close()
        self.assertEqual((self.root / 'execution-mode.json').stat().st_mode & 0o777, 0o600)
        account = self.root / 'chatgpt/account.json'
        account.parent.mkdir(); account.write_text('fixture-preserve')
        self.call('settings', mode='off')
        self.assertEqual(account.read_text(), 'fixture-preserve')

    def test_mode_change_is_locked_and_rejects_extra_fields(self):
        with (self.root / 'operation.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.call('settings')['mode'], 'steps')
            with self.assertRaises(WDAError) as error:
                self.call('settings', mode='off')
            self.assertEqual(error.exception.code, 'device_busy')
        for args in ({'mode': 'unknown'}, {'text': 'unexpected'}, {'report_id': 'unused'}):
            with self.assertRaises(WDAError):
                self.call('settings', **args)
        (self.root / 'execution-mode.json').write_text('{broken')
        with self.assertRaises(WDAError) as error:
            self.call()
        self.assertEqual(error.exception.code, 'invalid_execution_mode')
        self.assertEqual(self.call('settings', mode='off')['mode'], 'off')
