import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'windows'))
from apex_automation.account_provider import HttpAccountProvider, LeaseProviderError, ProviderHttpResponse
from apex_automation.registration_worker import RegistrationWorker, BitBrowser, write_json


class RegistrationWorkerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = self.root / 'runner.json'
        config.write_text('{}')
        (self.root / 'registration.private.json').write_text('{}')
        self.provider = HttpAccountProvider('https://forge.example/v1/runner/account-leases', 'provider-token', client_version='test')
        self.worker = RegistrationWorker(self.provider, SimpleNamespace(config_path=config), self.root,
                                         notify=Mock(), sleep=lambda _: None)
        self.worker.bit = Mock()
        self.worker.bit.pids.return_value = {}
        self.worker.request = Mock(return_value={})
        self.ea = Mock()

    def batch(self, count=1):
        return {'batchId': 'batch-one', 'tasks': [{'id': f'task-{i}', 'state': 'running'} for i in range(count)]}

    def child(self, code=0):
        child = Mock(pid=999999, returncode=code)
        child.poll.return_value = code
        return child

    def finishes(self):
        return [c for c in self.worker.request.call_args_list if c.args[1].endswith('/finish')]

    def test_idle_test_mode_obeys_update_boundary_before_dispatch(self):
        from apex_automation.managed_runtime import ManagedUpdateRequested
        self.worker.maintenance = Mock()
        self.worker.request.return_value = {'batch': None}
        self.worker.maintenance.boundary.side_effect = ManagedUpdateRequested()
        with self.assertRaises(ManagedUpdateRequested):
            self.worker.before_game(self.ea)
        self.assertEqual(self.worker.request.call_count, 1)
        self.assertEqual(self.worker.request.call_args.args, ('GET', '/current'))

    def test_diagnostic_cursor_advances_only_after_ack_and_replay_uses_same_event(self):
        batch = self.batch()
        directory = self.worker.root / batch['batchId']
        work = directory / 'task-0'
        work.mkdir(parents=True)
        self.worker.local_event(work, 'process_exit', exitCode=1)
        self.worker.request.side_effect = LeaseProviderError('offline', code='NETWORK', retryable=True)
        with self.assertRaises(LeaseProviderError):
            self.worker.flush_diagnostics(batch, directory)
        original = self.worker.request.call_args.args[2]['events'][0]
        self.assertFalse((work / 'worker_diagnostics.jsonl.cursor.json').exists())
        self.worker.request.side_effect = None
        self.worker.request.return_value = {'accepted': True}
        self.worker.flush_diagnostics(batch, directory)
        self.assertEqual(original, self.worker.request.call_args.args[2]['events'][0])
        calls = self.worker.request.call_count
        self.worker.flush_diagnostics(batch, directory)
        self.assertEqual(calls, self.worker.request.call_count)

    def test_game_starts_only_after_verified_browser_cleanup_and_ea_preflight(self):
        order = []
        self.worker.bit.cleanup.side_effect = lambda *a: order.append('cleanup')
        self.ea.ensure_started.side_effect = lambda: order.append('ea')
        self.worker.request.side_effect = lambda method, path, *a: order.append('finish') if path.endswith('/finish') else {}
        with patch.object(self.worker, 'launch', return_value=self.child()):
            self.worker.run_batch(self.batch(), self.ea)
        self.assertEqual(order, ['cleanup', 'ea', 'finish'])

    def test_cleanup_failure_keeps_machine_reserved(self):
        self.worker.bit.cleanup.side_effect = RuntimeError('still open')
        with patch.object(self.worker, 'launch', return_value=self.child()):
            self.worker.run_batch(self.batch(), self.ea)
        self.assertEqual(self.finishes(), [])
        self.ea.ensure_started.assert_not_called()
        self.assertEqual(self.worker.request.call_args.args[2]['state'], 'blocked')

    def test_ea_unknown_keeps_batch_and_does_not_repeat_registration_on_retry(self):
        self.ea.ensure_started.side_effect = RuntimeError('unknown page')
        with patch.object(self.worker, 'launch', return_value=self.child()):
            self.worker.run_batch(self.batch(), self.ea)
        self.assertEqual(self.finishes(), [])
        batch = self.batch()
        batch['tasks'][0]['state'] = 'succeeded'
        self.ea.ensure_started.side_effect = None
        with patch.object(self.worker, 'launch', side_effect=AssertionError('must not re-register')):
            self.worker.run_batch(batch, self.ea)
        self.assertEqual(len(self.finishes()), 1)

    def test_restart_uploads_only_and_keeps_task_directory(self):
        work = self.worker.root / 'batch-one' / 'task-0'
        write_json(work / 'started.json', {'started': True})
        write_json(work / 'windows_trial.json', {'task_id': 'task-0'})
        with patch.object(self.worker, 'launch', return_value=self.child()) as launch:
            self.worker.run_batch(self.batch(), self.ea)
        self.assertTrue(launch.call_args.kwargs['sync_only'])
        self.assertEqual(launch.call_args.args[1], work)

    def retry_checkpoint(self, work, **extra):
        write_json(work / 'windows_trial.json', {'task_id': 'task-0'})
        result = dict(ea_creation_submitted=True, ea_registered=False, ea_password='saved',
                      ea_origin_id='same-player', stage='verify_account', error_code='ea_network_error')
        result.update(extra)
        write_json(work / 'ea_full_setup_result.json', result)
        write_json(work / 'flow_browsers.json', ['owned-profile'])

    def test_network_failure_retries_same_task_only_after_cleanup(self):
        order = []
        def launch(task, work, **kwargs):
            order.append('launch')
            if order.count('launch') == 1:
                self.retry_checkpoint(work)
                return self.child(1)
            self.assertFalse(kwargs['sync_only'])
            self.assertEqual(json.loads((work / 'started.json').read_text())['attempts'], 2)
            self.assertEqual(json.loads((work / 'flow_operation.json').read_text()), {})
            return self.child(0)
        self.worker.bit.cleanup.side_effect = lambda ids, sleep: order.append(('cleanup', ids))
        with patch.object(self.worker, 'launch', side_effect=launch), patch.object(self.worker, 'mark_interrupted') as finish:
            self.worker.run_batch(self.batch(), self.ea)
        self.assertEqual(order[:3], ['launch', ('cleanup', ['owned-profile']), 'launch'])
        finish.assert_not_called()
        self.assertEqual(len(self.finishes()), 1)

    def test_repeated_failure_stops_after_three_attempts_even_across_restart(self):
        work = self.worker.root / 'batch-one' / 'task-0'
        self.retry_checkpoint(work)
        write_json(work / 'started.json', {'started': True, 'attempts': 2})
        with patch.object(self.worker, 'launch', return_value=self.child(1)) as launch, \
                patch.object(self.worker, 'mark_interrupted') as finish:
            self.worker.run_batch(self.batch(), self.ea)
        self.assertEqual(launch.call_count, 1)
        self.assertFalse(launch.call_args.kwargs['sync_only'])
        finish.assert_called_once()
        self.assertEqual(json.loads((work / 'started.json').read_text())['attempts'], 3)

    def test_step_timeout_can_recover_without_resetting_total_budget(self):
        import time
        first_started = time.time() - 10
        def launch(task, work, **kwargs):
            state = json.loads((work / 'started.json').read_text())
            if state['attempts'] == 1:
                self.retry_checkpoint(work, error_code=None)
                state['firstStartedAt'] = first_started
                write_json(work / 'started.json', state)
                write_json(work / 'flow_operation.json', {'deadline': time.monotonic() - 100, 'stage': 'verify_account'})
                child = self.child(None)
                def stop():
                    child.returncode = -15
                    child.poll.return_value = -15
                child.terminate.side_effect = stop
                return child
            self.assertEqual(state['firstStartedAt'], first_started)
            return self.child(0)
        with patch.object(self.worker, 'launch', side_effect=launch) as spawn, patch.object(self.worker, 'mark_interrupted') as finish:
            self.worker.run_batch(self.batch(), self.ea)
        self.assertEqual(spawn.call_count, 2)
        finish.assert_not_called()

    def test_exhausted_wall_clock_only_uploads_and_does_not_reset(self):
        import time
        work = self.worker.root / 'batch-one' / 'task-0'
        self.retry_checkpoint(work)
        write_json(work / 'started.json', {'attempts': 1, 'firstStartedAt': time.time() - self.worker.timeout - 1})
        with patch.object(self.worker, 'launch', return_value=self.child(0)) as launch:
            self.worker.run_batch(self.batch(), self.ea)
        self.assertTrue(launch.call_args.kwargs['sync_only'])

    def test_recovery_cleanup_failure_does_not_launch_another_browser(self):
        def launch(task, work, **kwargs):
            self.retry_checkpoint(work)
            return self.child(1)
        self.worker.bit.cleanup.side_effect = RuntimeError('still running')
        with patch.object(self.worker, 'launch', side_effect=launch) as spawn:
            self.worker.run_batch(self.batch(), self.ea)
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(self.finishes(), [])
        self.ea.ensure_started.assert_not_called()

    def test_live_orphan_child_blocks_recovery_upload_and_new_work(self):
        work = self.worker.root / 'batch-one' / 'task-0'
        write_json(work / 'started.json', {'started': True, 'pid': 123})
        with patch('apex_automation.registration_worker.process_alive', return_value=True), \
                patch.object(self.worker, 'launch', side_effect=AssertionError('must wait')):
            self.worker.run_batch(self.batch(), self.ea)
        self.assertEqual(self.finishes(), [])

    def test_unmanaged_browser_window_is_not_closed_and_prevents_game(self):
        self.worker.request.side_effect = [{'batch': None}, {'action': 'game'}]
        self.worker.bit.pids.return_value = {'manual-profile': 200}
        self.assertTrue(self.worker.before_game(self.ea))
        self.worker.bit.cleanup.assert_not_called()
        self.ea.ensure_started.assert_not_called()

    def test_an_unsent_result_does_not_stop_the_other_task(self):
        # 10-16: a brief outage failed one task's upload; the whole round
        # stopped and the other task's Steam registration was killed.
        running = self.child(0)
        running.poll.side_effect = [None] * 6 + [0] * 40
        children = {'task-0': self.child(1), 'task-1': running}
        offline = LeaseProviderError('offline', code='PROVIDER_UNREACHABLE', retryable=True)
        with patch.object(self.worker, 'launch', side_effect=lambda task, work, **kw: children[task['id']]), \
                patch.object(self.worker, 'retry_action', return_value='stop'), \
                patch.object(self.worker, 'mark_interrupted', side_effect=[offline, None]) as upload:
            self.worker.run_batch(self.batch(2), self.ea)
        self.assertEqual(upload.call_count, 2)
        running.terminate.assert_not_called()
        self.assertEqual(len(self.finishes()), 1)

    def test_a_result_still_unsent_keeps_the_batch_open_without_killing_anything(self):
        offline = LeaseProviderError('offline', code='PROVIDER_UNREACHABLE', retryable=True)
        child = self.child(1)
        with patch.object(self.worker, 'launch', return_value=child), \
                patch.object(self.worker, 'retry_action', return_value='stop'), \
                patch.object(self.worker, 'mark_interrupted', side_effect=offline) as upload:
            self.worker.run_batch(self.batch(), self.ea)
        self.assertGreater(upload.call_count, 2)
        child.terminate.assert_not_called()
        self.assertEqual(self.finishes(), [])
        self.assertEqual(self.worker.request.call_args.args[2]['state'], 'blocked')

    def test_current_game_recovery_bypasses_ea_preflight(self):
        self.worker.request.side_effect = [{'batch': None}, {'action': 'game', 'recoverGame': True}]
        self.assertFalse(self.worker.before_game(self.ea))
        self.ea.ensure_started.assert_not_called()

    def test_lost_dispatch_response_reuses_key_and_body(self):
        self.worker.request.side_effect = [{'batch': None}, LeaseProviderError('offline', retryable=True)]
        with self.assertRaises(LeaseProviderError):
            self.worker.before_game(self.ea)
        original = self.worker.request.call_args
        self.worker.request.side_effect = [{'batch': None}, {'action': 'wait', 'reason': 'waiting'}]
        self.assertTrue(self.worker.before_game(self.ea))
        self.assertEqual(self.worker.request.call_args, original)

    def test_subprocesses_have_independent_output_and_runner_credentials(self):
        observed = []
        def spawn(command, **kwargs):
            observed.append((command, kwargs['env']))
            return self.child()
        with patch('apex_automation.registration_worker.subprocess.Popen', side_effect=spawn):
            self.worker.run_batch(self.batch(2), self.ea)
        self.assertEqual(len(observed), 2)
        self.assertEqual(observed[0][0][:3], [sys.executable, '-m', 'apex_registration'])
        self.assertEqual(observed[0][1]['PYTHONPATH'], str(self.worker.runtime_root))
        self.assertNotEqual(observed[0][1]['ACCOUNT_FLOW_OUTPUT_DIR'], observed[1][1]['ACCOUNT_FLOW_OUTPUT_DIR'])
        self.assertEqual([e['FORGE_RUNNER_TASK_ID'] for _, e in observed], ['task-0', 'task-1'])
        self.assertTrue(all(e['FORGE_TOKEN'] == 'provider-token' for _, e in observed))
        self.assertNotIn('provider-token', str([c for c, _ in observed]))

    def test_interrupted_result_replays_successful_pending_payload(self):
        work = self.root / 'pending'
        payload = {'finished': True, 'exit_code': 0, 'result': {'stage': 'complete'}, 'steam_result': {}}
        write_json(work / 'forge_pending_result.json', payload)
        transport = Mock()
        transport.request.return_value = ProviderHttpResponse(200, {}, b'{"state":"succeeded"}')
        self.worker.api.transport = transport
        self.worker.mark_interrupted({'id': 'task-0'}, work)
        self.assertEqual(json.loads(transport.request.call_args.args[3]), payload)
        self.assertEqual(transport.request.call_args.args[1], 'https://forge.example/v1/runner/registration-tasks/task-0/result')

    def test_unavailable_registration_environment_still_allows_game(self):
        self.worker.bit.pids.side_effect = OSError('not running')
        self.worker.request.side_effect = [{'batch': None}, {'action': 'game'}]
        self.assertFalse(self.worker.before_game(self.ea))
        self.ea.ensure_started.assert_called_once()

    def test_parent_preserves_acknowledged_child_failure(self):
        work = self.root / 'finished'
        payload = {'finished': True, 'exit_code': 2, 'result': {'stage': 'proxy_check',
            'error_code': 'proxy_check_failed', 'error_message': 'ipinfo.io: ConnectTimeout'}, 'steam_result': {}}
        write_json(work / 'forge_final_result.json', payload)
        transport = Mock()
        transport.request.return_value = ProviderHttpResponse(200, {}, b'{"state":"needs_review"}')
        self.worker.api.transport = transport
        self.worker.mark_interrupted({'id': 'task-0'}, work)
        self.assertEqual(json.loads(transport.request.call_args.args[3]), payload)

    def test_failure_before_checkout_is_uploaded_with_stage(self):
        work = self.root / 'preflight'
        result = {'stage': 'browser_check', 'error_code': 'browser_check_failed', 'error_message': 'Local API unavailable'}
        write_json(work / 'ea_full_setup_result.json', result)
        transport = Mock()
        transport.request.return_value = ProviderHttpResponse(200, {}, b'{"state":"needs_review"}')
        self.worker.api.transport = transport
        self.worker.mark_interrupted({'id': 'task-0'}, work)
        self.assertEqual(json.loads(transport.request.call_args.args[3])['result'], result)

    def test_killed_child_uploads_saved_exit_without_an_existing_payload(self):
        work = self.root / 'killed'
        write_json(work / 'proxy_exit.json', {'provider': '711', 'attempt': 2, 'ip': '192.0.2.2'})
        transport = Mock()
        transport.request.return_value = ProviderHttpResponse(200, {}, b'{"state":"failed"}')
        self.worker.api.transport = transport
        self.worker.mark_interrupted({'id': 'task-0'}, work)
        payload = json.loads(transport.request.call_args.args[3])
        self.assertEqual((payload['exit']['provider'], payload['exit']['ip']), ('711', '192.0.2.2'))

    def test_no_local_registration_file_is_needed_for_dispatch(self):
        self.worker.config_path.unlink()
        self.worker.configure()
        self.worker.bit = Mock()
        self.worker.bit.pids.return_value = {}
        self.worker.request.side_effect = [{'batch': None}, {'action': 'game'}]
        self.assertFalse(self.worker.before_game(self.ea))
        body = self.worker.request.call_args.args[2]
        self.assertTrue(body['ready'])
        self.assertEqual(body['registrationConfigVersion'], 1)
        self.assertNotIn('canRegisterSteam', body)
        self.assertIsNone(body['error'])
        self.ea.ensure_started.assert_called_once()

    def test_old_forge_without_registration_keeps_existing_game_loop(self):
        self.worker.request.side_effect = LeaseProviderError('not found', code='HTTP_404')
        self.assertFalse(self.worker.before_game(self.ea))
        self.assertFalse(self.worker.before_game(self.ea))
        self.worker.request.assert_called_once_with('GET', '/current')
        self.ea.ensure_started.assert_called_once()

    def test_pending_registration_prevents_game_claim_and_then_yields(self):
        from apex_automation.account_orchestrator import AccountOrchestrator, AccountCycleOutcome
        from apex_automation.orchestration_state import AtomicCheckpointStore
        provider = Mock()
        provider.current.return_value = None
        provider.claim.return_value = None
        registration = Mock()
        registration.before_game.side_effect = [True, False]
        cycle = AccountOrchestrator(provider=provider, ea_driver=self.ea, play_session=Mock(),
            checkpoint_store=AtomicCheckpointStore(self.root / 'checkpoint.json'),
            device_id='test-device', capture_source=Mock(), registration=registration)
        self.assertEqual(cycle.run_once().outcome, AccountCycleOutcome.NO_ACCOUNT)
        provider.claim.assert_not_called()
        self.assertEqual(cycle.run_once().outcome, AccountCycleOutcome.NO_ACCOUNT)
        provider.claim.assert_called_once()

    def test_pending_game_operation_bypasses_registration(self):
        from apex_automation.account_orchestrator import AccountOrchestrator, AccountCycleOutcome
        from apex_automation.orchestration_state import AtomicCheckpointStore, OrchestrationCheckpoint, PendingOperation
        checkpoint = AtomicCheckpointStore(self.root / 'checkpoint.json')
        checkpoint.save(OrchestrationCheckpoint(device_id='test-device',
            pending_operation=PendingOperation.CLAIM, claim_request_id='saved-request',
            operation_id='saved-request'))
        provider = Mock()
        provider.current.return_value = None
        provider.claim.return_value = None
        registration = Mock()
        cycle = AccountOrchestrator(provider=provider, ea_driver=self.ea, play_session=Mock(),
            checkpoint_store=checkpoint, device_id='test-device', capture_source=Mock(),
            registration=registration)
        self.assertEqual(cycle.run_once().outcome, AccountCycleOutcome.NO_ACCOUNT)
        registration.before_game.assert_not_called()
        provider.claim.assert_called_once()

    def test_cleanup_checks_actual_pids_not_just_close_response(self):
        bit = BitBrowser('http://127.0.0.1:54345')
        bit.pids = Mock(side_effect=[{'ours': 1, 'manual': 2}, {'ours': 1}, {}])
        bit.call = Mock()
        bit.cleanup(['ours'], sleep=lambda _: None)
        bit.call.assert_called_once_with('/browser/close', {'id': 'ours'})
        self.assertEqual(bit.pids.call_count, 3)

    def test_local_api_key_applies_to_capacity_and_cleanup_calls(self):
        for key in ('', 'fixture-api-key'):
            with self.subTest(key=bool(key)):
                bit = BitBrowser('http://127.0.0.1:54345', key)
                bit.opener = Mock()
                bit.opener.open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'{"success":true,"data":{}}')))
                bit.opener.open.return_value.__exit__ = Mock(return_value=False)
                bit.pids()
                request = bit.opener.open.call_args.args[0]
                self.assertEqual(request.get_header('X-api-key'), key or None)


if __name__ == '__main__':
    unittest.main()
