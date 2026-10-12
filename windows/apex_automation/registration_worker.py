"""Run finite Forge batches using the bundled Controller registration runtime."""
from __future__ import annotations

import json
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

    def mark_interrupted(self, task, directory):
        payload = read_json(directory / 'forge_pending_result.json')
        if not payload:
            payload = {'finished': True, 'exit_code': 1, 'result': {'error_code': 'runner_interrupted',
                       'error_message': '执行中断，已有成果保留，请复核后继续'}, 'steam_result': {}}
        payload['finished'] = True
        payload['exit_code'] = payload.get('exit_code') if payload.get('exit_code') is not None else 1
        client = HttpAccountProvider(self.base + '/v1/runner/registration-tasks', self.token,
                                    client_version=self.api.client_version, transport=self.api.transport)
        client._request('PUT', f"/{task['id']}/result", payload=payload)

    def run_batch(self, batch, ea_driver):
        batch_id = batch['batchId']
        directory = self.root / batch_id
        processes = {}
        try:
            if not self.script.is_file():
                raise RuntimeError('恢复注册批次需要更新 Controller 注册运行模块')
            self.notify(f"注册批次 {batch_id[:8]}：{len(batch['tasks'])} 项，完成后切回游戏")
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
                    self.mark_interrupted(task, work)
                    continue
                write_json(work / 'started.json', {'started': True})
                child = self.launch(task, work, sync_only=bool(started))
                write_json(work / 'started.json', {'started': True, 'pid': child.pid})
                processes[task['id']] = (child, time.monotonic(), work, task)
            next_heartbeat = 0
            while processes:
                self.pulse('REGISTERING')
                events = []
                for task_id, (child, started_at, work, task) in list(processes.items()):
                    operation = read_json(work / 'flow_operation.json')
                    events.append({'id': task_id, 'stage': operation.get('stage', 'checkout'),
                                   'message': operation.get('message', '注册执行中')})
                    now = time.monotonic()
                    if child.poll() is None and (now - started_at > self.timeout or
                            (operation.get('deadline') and now > operation['deadline'] + 45)):
                        child.terminate()
                        try:
                            child.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=10)
                        self.mark_interrupted(task, work)
                    if child.poll() is not None:
                        if child.returncode != 0:
                            self.mark_interrupted(task, work)
                        del processes[task_id]
                if time.monotonic() >= next_heartbeat:
                    try:
                        self.heartbeat(batch_id, 'running', events)
                    except LeaseProviderError:
                        self.notify('注册心跳暂未送达，本地继续执行并保留成果')
                    next_heartbeat = time.monotonic() + 15
                if processes:
                    self.sleep(1)
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
