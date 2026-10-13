"""Run finite Forge batches using the bundled Controller registration runtime."""
from __future__ import annotations

import json
import hashlib
import datetime as dt
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from urllib.request import Request, build_opener, ProxyHandler

from .account_provider import HttpAccountProvider, LeaseProviderError
from .atomic_files import replace_with_retry
from apex_registration.config import DEFAULTS, config_path, load_config, environment
from apex_registration.recovery import MAX_ATTEMPTS, completed, recovery_action


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {} if default is None else default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data), encoding='utf-8')
    replace_with_retry(temporary, path)


def process_alive(pid):
    if not pid:
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False


class BitBrowser:
    def __init__(self, url, api_key=''):
        self.url = url.rstrip('/')
        self.api_key = api_key.strip()
        self.opener = build_opener(ProxyHandler({}))

    def call(self, path, data=None):
        req = Request(self.url + path, data=json.dumps(data or {}).encode(),
                      headers={'Content-Type': 'application/json', **({'x-api-key': self.api_key} if self.api_key else {})})
        with self.opener.open(req, timeout=10) as response:
            body = json.loads(response.read())
        if body.get('success') is not True:
            raise RuntimeError('BitBrowser 接口调用失败：' + path)
        return body.get('data')

    def pids(self):
        # Official /browser/pids/all checks actual process liveness.
        data = self.call('/browser/pids/all')
        if not isinstance(data, dict):
            raise RuntimeError('BitBrowser 进程列表无法确认')
        return {key: value for key, value in data.items() if value}

    def cleanup(self, ids, sleep=time.sleep):
        live = self.pids()
        for profile_id in set(ids) & live.keys():
            self.call('/browser/close', {'id': profile_id})
        for _ in range(15):
            if not (set(ids) & self.pids().keys()):
                return
            sleep(1)
        raise RuntimeError('本轮 BitBrowser 窗口仍未关闭')


def minimize_bitbrowser(ea_driver):
    if os.name != 'nt':
        return
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(hwnd, _):
        try:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            name = ea_driver._process_name(pid.value).lower()
            if name in {'bitbrowser.exe', '比特浏览器.exe'} and user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE, leave Local API running.
        except (AttributeError, OSError):
            pass
        return True
    user32.EnumWindows(visit, 0)


class RegistrationWorker:
    def __init__(self, provider, settings, root, *, maintenance=None, notify=print, sleep=time.sleep):
        self.runtime_root = Path(__file__).resolve().parents[1]
        self.script = self.runtime_root / 'apex_registration' / '__main__.py'
        self.config_path = config_path(settings.config_path)
        self.configure()
        self.base = provider.lease_url.rsplit('/v1/runner/account-leases', 1)[0]
        self.token = provider._provider_token
        self.api = HttpAccountProvider(self.base + '/v1/runner/registration-batches', self.token,
                                      client_version=provider.client_version, transport=provider.transport)
        self.root = Path(root) / 'registrations'
        self.maintenance, self.notify, self.sleep = maintenance, notify, sleep
        self.available = True
        self.release_version = os.environ.get('APEX_CONTROLLER_COMMIT') or provider.client_version

    def configure(self):
        self.config_error = None
        try:
            config = load_config(self.config_path)
        except ValueError as exc:
            config = dict(DEFAULTS)
            self.config_error = str(exc)
        self.environment = environment(config)
        self.timeout = config['timeoutSeconds']
        self.bit = BitBrowser(config['bitbrowserUrl'], config['bitbrowserApiKey'])

    def request(self, method, suffix, payload=None, key=None):
        return self.api._request(method, suffix, payload=payload, operation_id=key)[0]

    def pulse(self, phase):
        if self.maintenance:
            self.maintenance.pulse(phase, blocked=False)

    def before_game(self, ea_driver):
        """True consumes this turn. False permits the existing game workflow."""
        if not self.available:
            return False
        try:
            current = self.request('GET', '/current').get('batch')
        except LeaseProviderError as exc:
            if exc.code == 'HTTP_404':
                self.available = False
                self.notify('Forge 尚未提供注册调度，继续原游戏任务循环')
                ea_driver.ensure_started()
                return False
            raise
        if current:
            self.run_batch(current, ea_driver)
            return True
        if self.maintenance:
            # A paused/test-idle desktop is also a safe update boundary.
            self.maintenance.boundary()
        ready, capacity, error = False, 0, self.config_error
        if self.script.is_file() and not error:
            try:
                capacity = max(0, 5 - len(self.bit.pids()))
                ready = capacity > 0
            except (OSError, ValueError, RuntimeError):
                error = 'BitBrowser Local API 未就绪'
        elif not self.script.is_file():
            error = '注册运行模块缺失，请更新 Controller'
        pending_path = self.root / 'dispatch.json'
        pending = read_json(pending_path)
        # Persist both the key and body so a lost response recovers the same round.
        if not pending:
            pending = {'key': uuid.uuid4().hex, 'body': {'ready': ready, 'capacity': capacity, 'error': error, 'registrationConfigVersion': 1}}
            write_json(pending_path, pending)
        plan = self.request('POST', '/dispatch', pending['body'], pending['key'])
        pending_path.unlink(missing_ok=True)
        if plan['action'] == 'register':
            self.run_batch(plan['batch'], ea_driver)
            return True
        if plan['action'] == 'wait':
            self.notify(plan['reason'])
            return True
        if plan.get('recoverGame'):
            return False
        if self.script.is_file() and not error:
            # An unmanaged browser window is left alone, but blocks game mode.
            # An unavailable registration environment does not disable gaming;
            # unfinished managed batches always take the recovery path above.
            if self.bit.pids():
                self.notify('BitBrowser 仍有打开的窗口，等待关闭后再进入游戏')
                return True
            minimize_bitbrowser(ea_driver)
        ea_driver.ensure_started()
        return False

    def heartbeat(self, batch_id, state, tasks=(), error=None):
        return self.request('POST', f'/{batch_id}/heartbeat', {'state': state, 'tasks': list(tasks), 'error': error})

    def launch(self, task, directory, *, sync_only=False):
        env = dict(os.environ, **self.environment)
        env.update(FORGE_API_URL=self.base, FORGE_TOKEN=self.token, FORGE_RUNNER_TASK_ID=task['id'],
                   APEX_REGISTRATION_MANAGED='1',
                   APEX_REGISTRATION_DEFER_FAILURE='0' if sync_only else '1',
                   APEX_REGISTRATION_ATTEMPT=str(read_json(directory / 'started.json').get('attempts', 1)),
                   FORGE_PROXY=env.get('FORGE_PROXY', 'direct'), GAME_ACCOUNTS_DESKTOP_BROWSER='bitbrowser',
                   ACCOUNT_FLOW_OUTPUT_DIR=str(directory))
        log = (directory / 'runner.log').open('ab')
        env['PYTHONPATH'] = str(self.runtime_root)
        command = [sys.executable, '-m', 'apex_registration', '--output-dir', str(directory)]
        if sync_only:
            command.append('--sync-only')
        try:
            return subprocess.Popen(command, env=env, cwd=str(self.runtime_root), stdout=log, stderr=log)
        finally:
            log.close()

    def retry_action(self, work, *, interrupted=False):
        started = read_json(work / 'started.json')
        if (started.get('syncOnly') or int(started.get('attempts', 1)) >= MAX_ATTEMPTS
                or time.time() - started.get('firstStartedAt', time.time()) >= self.timeout):
            return 'stop'
        # A missing checkout file cannot safely allocate the same task again.
        if not (work / 'windows_trial.json').is_file():
            return 'stop'
        return recovery_action(read_json(work / 'ea_full_setup_result.json'), interrupted=interrupted)

    def spawn(self, task, work, processes, *, recovering=False, sync_only=False):
        started = read_json(work / 'started.json')
        attempt = int(started.get('attempts', 1 if recovering else 0)) + (not sync_only)
        if recovering:
            # Only this task's windows may be closed. Failure blocks recovery.
            self.bit.cleanup(read_json(work / 'flow_browsers.json', []), self.sleep)
            if not sync_only:
                self.local_event(work, 'recovery', message=f'同一任务恢复，第 {attempt}/{MAX_ATTEMPTS} 轮')
                self.notify(f'注册恢复：同一账号第 {attempt}/{MAX_ATTEMPTS} 轮')
                self.sleep(min(15, 5 * max(1, attempt - 1)))
        # Persist the budget before spawn, and remove the dead child's deadline.
        started = dict(started, started=True, attempts=attempt,
                       firstStartedAt=started.get('firstStartedAt', time.time()), pid=None, syncOnly=sync_only)
        write_json(work / 'started.json', started)
        write_json(work / 'flow_operation.json', {})
        if not sync_only:
            for name in ('forge_final_result.json', 'forge_receipt.json'):
                (work / name).unlink(missing_ok=True)
        child = self.launch(task, work, sync_only=sync_only)
        write_json(work / 'started.json', dict(started, pid=child.pid))
        processes[task['id']] = (child, time.monotonic(), work, task)

    def mark_interrupted(self, task, directory):
        payload = read_json(directory / 'forge_pending_result.json') or read_json(directory / 'forge_final_result.json')
        latest = read_json(directory / 'ea_full_setup_result.json')
        if payload and latest:
            payload['result'] = latest
        if not payload:
            result = latest
            operation = read_json(directory / 'flow_operation.json')
            if not result.get('error_code') and not completed(result):
                result.update(error_code='runner_interrupted', error_message='注册进程中断，已保存本机已有成果')
            result.setdefault('stage', operation.get('stage') or 'checkout')
            payload = {'finished': True, 'exit_code': 1, 'result': result,
                       'steam_result': read_json(directory / 'steam_register_result.json')}
        payload['finished'] = True
        payload['exit_code'] = payload.get('exit_code') if payload.get('exit_code') is not None else 1
        if payload['exit_code'] != 0 and not payload.get('result', {}).get('error_code') and not completed(payload.get('result', {})):
            payload.setdefault('result', {}).update(error_code='runner_interrupted',
                error_message='注册进程中断，恢复已停止；已保留完成阶段和原账号')
        engine = read_json(directory / 'protocol_register_result.json').get('register_engine')
        if engine and not payload.get('register_engine'):
            payload['register_engine'] = engine
        steam = read_json(directory / 'steam_register_result.json')
        if steam:
            payload['steam_result'] = steam
        updates = read_json(directory / 'windows_trial.json').get('mailbox_updates')
        if updates:
            payload['mailbox_updates'] = updates
        if completed(payload.get('result', {})):
            payload['exit_code'] = 0
        client = HttpAccountProvider(self.base + '/v1/runner/registration-tasks', self.token,
                                    client_version=self.api.client_version, transport=self.api.transport)
        write_json(directory / 'forge_pending_result.json', payload)
        receipt, _ = client._request('PUT', f"/{task['id']}/result", payload=payload)
        write_json(directory / 'forge_receipt.json', receipt)
        write_json(directory / 'forge_final_result.json', payload)
        (directory / 'forge_pending_result.json').unlink(missing_ok=True)
        self.local_event(directory, 'result_upload', message='已有结果已回传', stage=payload.get('result', {}).get('stage'))

    def run_batch(self, batch, ea_driver):
        batch_id = batch['batchId']
        directory = self.root / batch_id
        processes = {}
        try:
            if not self.script.is_file():
                raise RuntimeError('恢复注册批次需要更新 Controller 注册运行模块')
            self.notify(f"注册批次 {batch_id[:8]}：{len(batch['tasks'])} 项，完成后按测试或自动模式继续")
            for task in batch['tasks']:
                work = directory / task['id']
                work.mkdir(parents=True, exist_ok=True)
                if task['state'] not in {'queued', 'running'}:
                    continue
                started = read_json(work / 'started.json')
                if started and process_alive(started.get('pid')):
                    raise RuntimeError('原注册进程仍在运行，等待它退出后回收成果')
                # Do not repeat a possibly successful account creation after a crash.
                if started and not (work / 'windows_trial.json').exists():
                    self.local_event(work, 'recovery', message='恢复已有成果，不重复创建账号')
                    self.mark_interrupted(task, work)
                    continue
                action = self.retry_action(work, interrupted=True) if started else 'create'
                self.spawn(task, work, processes, recovering=bool(started),
                           sync_only=action in {'stop', 'upload'})
            next_heartbeat = 0
            while processes:
                self.pulse('REGISTERING')
                events = []
                for task_id, (child, started_at, work, task) in list(processes.items()):
                    operation = read_json(work / 'flow_operation.json')
                    message = operation.get('message', '注册执行中')
                    events.append({'id': task_id, 'stage': operation.get('stage', 'checkout'),
                                   'message': message, 'name': operation.get('name'),
                                   'seconds': operation.get('seconds'), 'clientVersion': self.release_version})
                    now = time.monotonic()
                    started = read_json(work / 'started.json')
                    timed_out = False
                    expired = (now - started_at > 90 if started.get('syncOnly') else
                               time.time() - started.get('firstStartedAt', time.time()) > self.timeout)
                    if child.poll() is None and (expired or
                            (operation.get('deadline') and now > operation['deadline'] + 45)):
                        self.local_event(work, 'timeout', stage=operation.get('stage'), name=operation.get('name'))
                        child.terminate()
                        try:
                            child.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=10)
                        timed_out = True
                    if child.poll() is not None:
                        self.local_event(work, 'process_exit', exitCode=child.returncode, stage=operation.get('stage'))
                        if child.returncode != 0:
                            action = self.retry_action(work, interrupted=True)
                            if action in {'create', 'resume'}:
                                self.spawn(task, work, processes, recovering=True)
                                continue
                            if timed_out:
                                result = read_json(work / 'ea_full_setup_result.json')
                                if not result.get('error_code') and not completed(result):
                                    result.update(error_code='ea_step_timeout',
                                        error_message='注册步骤或总耗时超过恢复预算，已保留完成的阶段',
                                        stage=operation.get('stage') or result.get('stage', 'checkout'))
                                    write_json(work / 'ea_full_setup_result.json', result)
                            self.mark_interrupted(task, work)
                        receipt = read_json(work / 'forge_receipt.json')
                        self.local_event(work, 'result_upload', outcome=receipt.get('state', 'pending'), message='成果回传已确认' if receipt else '等待确认成果回传')
                        del processes[task_id]
                if time.monotonic() >= next_heartbeat:
                    try:
                        self.heartbeat(batch_id, 'running', events)
                        self.flush_diagnostics(batch, directory)
                    except LeaseProviderError:
                        self.notify('注册心跳暂未送达，本地继续执行并保留成果')
                    next_heartbeat = time.monotonic() + 15
                if processes:
                    self.sleep(1)
            for _ in range(10):
                if not self.flush_diagnostics(batch, directory):
                    break
            self.heartbeat(batch_id, 'cleaning')
            self.pulse('REGISTRATION_CLEANUP')
            ids = []
            for task in batch['tasks']:
                ids.extend(read_json(directory / task['id'] / 'flow_browsers.json', []))
            self.bit.cleanup(ids, self.sleep)
            if self.bit.pids():
                raise RuntimeError('还有非本轮 BitBrowser 窗口，关闭后会自动继续')
            self.heartbeat(batch_id, 'game_preflight')
            self.pulse('EA_PREFLIGHT')
            minimize_bitbrowser(ea_driver)
            ea_driver.ensure_started()
            self.request('POST', f'/{batch_id}/finish', {'browsersClosed': True, 'gameReady': True})
            if self.maintenance:
                self.maintenance.boundary()
        except Exception as exc:
            # Preserve the batch; the next boundary retries upload/cleanup only.
            message = str(exc)[:256]
            self.notify('注册批次等待恢复：' + message)
            try:
                self.heartbeat(batch_id, 'blocked', error=message)
            except LeaseProviderError:
                pass
        finally:
            # Worker updates/F8 must not leave registration children running.
            for child, _, _, _ in processes.values():
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=10)

    def local_event(self, work, kind, **data):
        with (Path(work) / 'worker_diagnostics.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(data, kind=kind, occurredAt=dt.datetime.now(dt.timezone.utc).isoformat(),
                eventId=uuid.uuid4().hex + uuid.uuid4().hex)) + '\n')

    def flush_diagnostics(self, batch, directory):
        # Persist an offset only after acknowledgement. Replays are deduplicated by Forge.
        sent = False
        for task in batch['tasks']:
            work = directory / task['id']
            for name in ('diagnostics.jsonl', 'worker_diagnostics.jsonl'):
                path = work / name
                if not path.exists():
                    continue
                cursor_path = work / (name + '.cursor.json')
                offset = read_json(cursor_path).get('offset', 0)
                events = []
                with path.open('rb') as stream:
                    stream.seek(offset)
                    for _ in range(100):
                        line = stream.readline()
                        if not line or not line.endswith(b'\n'):
                            break
                        try:
                            item = json.loads(line)
                            item.update(taskId=task['id'], clientVersion=self.release_version)
                            events.append(item)
                        except (ValueError, AttributeError):
                            pass
                        offset = stream.tell()
                if events:
                    self.request('POST', f"/{batch['batchId']}/diagnostics", {'events': events})
                    write_json(cursor_path, {'offset': offset})
                    sent = True
            for path in sorted(work.glob('*.support.png'))[-5:]:
                if path.stat().st_size > 512 * 1024:
                    continue
                png = path.read_bytes()
                identity = hashlib.sha256(png).hexdigest()
                receipt = path.with_suffix('.uploaded.json')
                if read_json(receipt).get('id') == identity:
                    continue
                response = self.api.transport.request('PUT',
                    f"{self.base}/v1/runner/registration-batches/{batch['batchId']}/evidence/{task['id']}/{identity}",
                    {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'image/png'}, png, 10)
                if response.status != 200:
                    raise LeaseProviderError('截图等待回传', code='EVIDENCE_UPLOAD_PENDING', retryable=True)
                write_json(receipt, {'id': identity})
        return sent
