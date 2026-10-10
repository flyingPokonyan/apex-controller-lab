"""In-place managed updater. Standard library only; never import the game runner.

Git fetch runs while the worker is alive; checkout/pip only run under play.lock
after the owned worker has exited. Worker requests are bound to a launch UUID.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

UPDATE_EXIT = 75
RETRY_EXIT = 74
RELOAD_EXIT = 76
BUSY_EXIT = 73
MAX_FAILURES = 3


class LauncherError(RuntimeError):
    pass


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(12):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 11:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


class FileLock:
    """Same byte-range lock as apex_automation.instance_lock on Windows."""
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise LauncherError("RUNNER_ALREADY_RUNNING") from error
        self.handle = handle
        return self

    def __exit__(self, *_):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


class WorkerJob:
    """Closing the launcher must not orphan its Windows input worker.

    https://learn.microsoft.com/windows/win32/procthread/job-objects
    The job handle is non-inheritable. Only this launcher's child is assigned.
    """
    def __init__(self, process):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes as c
        from ctypes import wintypes as w

        class Basic(c.Structure):
            _fields_ = [("processTime", c.c_int64), ("jobTime", c.c_int64),
                        ("flags", w.DWORD), ("minWorkingSet", c.c_size_t),
                        ("maxWorkingSet", c.c_size_t), ("processLimit", w.DWORD),
                        ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class Limits(c.Structure):
            _fields_ = [("basic", Basic), ("io", c.c_uint64 * 6),
                        ("processMemory", c.c_size_t), ("jobMemory", c.c_size_t),
                        ("peakProcessMemory", c.c_size_t), ("peakJobMemory", c.c_size_t)]

        api = c.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        api.CreateJobObjectW.restype = w.HANDLE
        api.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        api.CloseHandle.argtypes = [w.HANDLE]
        self.api = api
        handle = api.CreateJobObjectW(None, None)
        if not handle:
            raise LauncherError("WORKER_JOB_CREATE_FAILED")
        limits = Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not api.SetInformationJobObject(handle, 9, c.byref(limits), c.sizeof(limits))
                or not api.AssignProcessToJobObject(handle, int(process._handle))):
            api.CloseHandle(handle)
            raise LauncherError("WORKER_JOB_ASSIGN_FAILED")
        self.handle = handle

    def close(self):
        if self.handle is not None:
            self.api.CloseHandle(self.handle)
            self.handle = None


def terminate_owned(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=20)


def run_command(args, *, cwd, timeout=120, env=None):
    process = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                               start_new_session=os.name != "nt")
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except BaseException:
        terminate_owned(process)
        raise
    if process.returncode:
        # Git/pip exceptions can contain authenticated remote URLs. Keep them out
        # of shared state and console output; the operation name is sufficient.
        raise LauncherError(f"{Path(str(args[0])).name}: command failed ({process.returncode})")
    return stdout.decode("utf-8", errors="replace").strip()


class Repository:
    def __init__(self, root, git="git"):
        self.root = Path(root).resolve()
        self.git_exe = git
        self.env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never",
                        GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=15")

    def git(self, *args):
        return run_command([self.git_exe, "-c", "credential.interactive=never", *args],
                           cwd=self.root, timeout=90, env=self.env)

    def head(self):
        return self.git("rev-parse", "HEAD")

    def fetch(self):
        self.git("fetch", "--no-tags", "origin", "refs/heads/main")
        commit = self.git("rev-parse", "FETCH_HEAD")
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise LauncherError("INVALID_TARGET_COMMIT")
        if self.git("merge-base", self.head(), commit) != self.head():
            raise LauncherError("BRANCH_DIVERGED")
        return commit

    def fast_forward(self, commit):
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise LauncherError("INVALID_TARGET_COMMIT")
        if self.git("symbolic-ref", "--short", "HEAD") != "main":
            raise LauncherError("EXPECTED_MAIN_BRANCH")
        if self.git("status", "--porcelain", "--untracked-files=no"):
            raise LauncherError("TRACKED_LOCAL_CHANGES")
        if self.git("merge-base", self.head(), commit) != self.head():
            raise LauncherError("BRANCH_DIVERGED")
        self.git("merge", "--ff-only", commit)

    @property
    def worker_python(self):
        return self.root / "windows" / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def dependency_hash(self):
        return hashlib.sha256((self.root / "windows/requirements.txt").read_bytes()).hexdigest()

    def install(self, state):
        python = self.worker_python
        new_environment = not python.is_file()
        if new_environment:
            run_command([sys.executable, "-m", "venv", str(python.parents[1])], cwd=self.root)
        digest = self.dependency_hash()
        marker = python.parents[1] / ".managed-dependencies.json"
        try:
            environment_digest = read_json(marker).get("requirements")
        except (OSError, ValueError):
            environment_digest = None
        # The launcher cache can survive deletion/recreation of .venv. Only a
        # marker written after pip succeeds proves this environment is ready.
        if new_environment or state.get("dependencies") != digest or environment_digest != digest:
            env = dict(os.environ)
            env.setdefault("PIP_INDEX_URL", "https://mirrors.aliyun.com/pypi/simple/")
            run_command([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                         "--no-input", "--timeout", "30", "--retries", "2", "-r",
                         str(self.root / "windows/requirements.txt")], cwd=self.root, env=env, timeout=900)
            write_json(marker, {"requirements": digest})
        # Import only. Do not invoke account-cycle or its EA preflight here.
        env = dict(os.environ, PYTHONPATH=str(self.root / "windows"))
        run_command([str(python), "-c", "import apex_automation.cli"], cwd=self.root, env=env, timeout=120)
        return digest


def update_mode(status, *, now, started, phase_started, unresponsive_s=300):
    last = float(status.get("at", started))
    if now - last > unresponsive_s:
        return "recover", "WORKER_UNRESPONSIVE"
    # The game's own state machine owns stall recovery and safe termination.
    # An update must not race that recovery while its main loop is responding.
    if status.get("phase") == "APEX_PLAYING":
        return "boundary", None
    if status.get("blocked"):
        return "recover", str(status.get("reason") or "WORKER_BLOCKED")
    return "boundary", None


class Launcher:
    def __init__(self, repo, *, root=None, publisher=None):
        self.repo = repo
        self.launcher_digest = self.source_digest()
        self.root = Path(root) if root else repo.root / "windows/runs/managed"
        self.state_path = self.root / "launcher.json"
        self.state = read_json(self.state_path)
        self.state.setdefault("failures", {})
        self.state.setdefault("recoveryFailures", {})
        self.worker = None
        self.job = None
        self.session = None
        self.started = 0.0
        self.phase_started = 0.0
        self.phase = None
        self.last_worker_status = {}
        self.requested_at = None
        self.request_mode = None
        self.completed = 0
        self.next_start = time.monotonic() + max(0, float(self.state.get("retryAfter", 0)) - time.time())
        self._last_notice = None
        self._next_status = 0.0
        self.publisher = publisher

    def save(self, stage, **values):
        self.state.update(values, stage=stage, at=time.time())
        write_json(self.state_path, self.state)
        notice = (stage, self.state.get("error"), self.state.get("checkError"),
                  self.state.get("running"), self.state.get("installed"), self.state.get("target"))
        if notice != self._last_notice:
            reason = notice[1] or notice[2]
            versions = " ".join(f"{name}={str(self.state.get(name) or '-')[:8]}"
                                for name in ("running", "installed", "target"))
            print(f"[managed] {stage} {versions}" + (f" ({reason})" if reason else ""), flush=True)
            if stage == "RUNNING" and self.state.get("workerConfirmedAt") and (
                    self._last_notice is None or self._last_notice[0] != "RUNNING"):
                print("[managed] 已收到当前版本的主循环心跳；业务是否正常请结合当前阶段和 Forge 上报查看。", flush=True)
            self._last_notice = notice

    def failure(self, commit):
        failures = self.state["failures"]
        failures[commit] = int(failures.get(commit, 0)) + 1
        self.state["failures"] = dict(list(failures.items())[-32:])
        self.next_start = time.monotonic() + min(300, 30 * 2 ** min(failures[commit] - 1, 4))

    def can_start(self, commit):
        return (not self.state.get("operatorStopped")
                and self.state["failures"].get(commit, 0) < MAX_FAILURES
                and time.monotonic() >= self.next_start)

    def install(self, target):
        if self.worker is not None:
            raise LauncherError("WORKER_MUST_EXIT_BEFORE_UPDATE")
        with FileLock(self.repo.root / "windows/runs/play.lock"):
            self.save("INSTALLING", installPending=True, target=target, error=None)
            self.repo.fast_forward(target)
            digest = self.repo.install(self.state)
            self.next_start = 0
            self.save("INSTALLED", installed=target, installedAt=time.time(), dependencies=digest, installPending=False, error=None, retryAfter=0)

    def needs_reload(self):
        return self.source_digest() != self.launcher_digest

    @staticmethod
    def source_digest():
        source = Path(__file__)
        publisher = source.with_name("managed_telemetry.py")
        return hashlib.sha256(source.read_bytes() + (publisher.read_bytes() if publisher.exists() else b"")).digest()

    def start(self, commit):
        self.session = uuid.uuid4().hex
        self.started = self.phase_started = time.time()
        self.phase = None
        self.last_worker_status = {}
        self.completed = 0
        self.requested_at = self.request_mode = None
        env = dict(os.environ, PYTHONPATH=str(self.repo.root / "windows"),
                   PYTHONUNBUFFERED="1", APEX_MANAGED_ROOT=str(self.root),
                   APEX_MANAGED_SESSION=self.session)
        # A retry of the same revision must retain the checkpoint's pause and
        # EA recovery budget. Explicit operator resume or a new revision gets
        # one recovery attempt, still subject to remote lease fences.
        args = [str(self.repo.worker_python), "-u", "-m", "apex_automation", "account-cycle",
                "--runner-config", str(self.repo.root / "windows/account-cycle.private.json")]
        if self.state.get("resumeWorker") or self.state.get("running") != commit:
            args.append("--resume")
        self.worker = subprocess.Popen(args, cwd=self.repo.root, env=env,
                                       creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                                       start_new_session=os.name != "nt")
        try:
            self.job = WorkerJob(self.worker)
        except BaseException:
            terminate_owned(self.worker)
            self.worker = None
            raise
        self.save("WORKER_STARTING", running=commit, resumeWorker=False, pid=self.worker.pid, session=self.session,
                  workerStartedAt=self.started, workerConfirmedAt=None, workerHeartbeatAt=None,
                  workerPid=None, workerPhase=None, workerBlocked=False, workerReason=None, completed=0, error=None)

    def request(self, mode):
        if self.request_mode in {"recover", "stop"} and mode == "boundary":
            # Once shutdown has begun, don't withdraw it on one recovered
            # observation or keep extending its deadline.
            return
        if self.request_mode == mode:
            return
        write_json(self.root / "request.json", {"session": self.session, "mode": mode})
        self.request_mode = mode
        if mode != "boundary":
            self.requested_at = time.monotonic()

    def worker_status(self):
        try:
            status = read_json(self.root / "worker.json")
        except (OSError, ValueError):
            return {}
        # Windows venv's python.exe redirects to a child interpreter. Popen.pid
        # belongs to that wrapper, while os.getpid() in worker.json belongs to
        # the interpreter. The per-launch UUID binds the heartbeat to this run;
        # requiring equal PIDs discards every valid Windows venv heartbeat.
        if (status.get("session") != self.session
                or type(status.get("pid")) is not int or status["pid"] <= 0):
            return {}
        return status

    def monitor(self, target):
        code = self.worker.poll()
        commit = self.state["running"]
        if code is not None:
            final_status = self.worker_status() or self.last_worker_status
            operator_stopped = final_status.get("operatorStopped")
            if self.job:
                self.job.close()
            self.worker = self.job = None
            if code == 0 or operator_stopped:
                self.save("OPERATOR_STOPPED", operatorStopped=True, pid=None)
            elif code == BUSY_EXIT:
                self.next_start = time.monotonic() + 30
                self.save("WAITING_LOCAL_RUNNER", pid=None, error=None)
            elif code == RETRY_EXIT:
                attempts = int(self.state["recoveryFailures"].get(commit, 0)) + 1
                self.state["recoveryFailures"][commit] = attempts
                self.state["recoveryFailures"] = dict(list(self.state["recoveryFailures"].items())[-32:])
                delay = min(300, 30 * 2 ** min(attempts - 1, 4))
                self.next_start = time.monotonic() + delay
                self.save("WAITING_RETRY", pid=None, retryAfter=time.time() + delay,
                          error=final_status.get("reason") or "RECOVERABLE_ENVIRONMENT_ERROR")
            elif code == UPDATE_EXIT and self.request_mode is not None and target and target != commit:
                self.save("UPDATE_READY", pid=None, error=None)
            else:
                self.failure(commit)
                self.save("WAITING_RETRY", pid=None, error=f"WORKER_EXIT_{code}")
            return
        status = self.worker_status()
        if status:
            self.last_worker_status = status
        else:
            # A failed read is not a main-loop hang. Retain the current
            # session's last status and its original timeout timestamp.
            status = self.last_worker_status
        now = time.time()
        fresh = status and 0 <= now - float(status.get("at", 0)) <= 30
        if fresh:
            self.state.update(workerHeartbeatAt=status["at"], workerPid=status["pid"], workerPhase=status.get("phase"),
                              workerBlocked=bool(status.get("blocked")), workerReason=status.get("reason"),
                              completed=int(status.get("completed", 0)))
            if not self.state.get("workerConfirmedAt"):
                self.state["workerConfirmedAt"] = now
                if self.state.get("stage") == "WORKER_STARTING":
                    self.save("RUNNING", error=None)
        if status.get("operatorStopped"):
            self.state["operatorStopped"] = True
            self.request("stop")
        phase = status.get("phase")
        if phase != self.phase:
            self.phase, self.phase_started = phase, time.time()
        completed = int(status.get("completed", 0))
        if completed > self.completed:
            self.completed = completed
            self.state["failures"][commit] = 0
            self.state["recoveryFailures"][commit] = 0
            self.state["retryAfter"] = 0
            self.save("RUNNING", error=None)
        mode, reason = update_mode(status, now=time.time(), started=self.started, phase_started=self.phase_started)
        if self.state.get("operatorStopped"):
            self.save("STOPPING", error=None)
        elif target and target != commit:
            self.request(mode)
            self.save("WAITING_WORKER_EXIT" if self.request_mode == "recover" else "WAITING_ACCOUNT_END", target=target, error=reason)
        elif reason == "WORKER_UNRESPONSIVE":
            self.request("recover")
            self.save("RECOVERING_UNRESPONSIVE", error=reason)
        if self.requested_at is not None and time.monotonic() - self.requested_at > 90:
            cleanup_responding = (self.request_mode == "recover" and fresh
                and phase in {"APEX_STOPPING", "EA_SIGNING_OUT", "LEASE_COMPLETING"}
                and time.monotonic() - self.requested_at <= 600)
            if not cleanup_responding:
                terminate_owned(self.worker)
            # poll() then records a failed attempt on the next iteration.
        if time.monotonic() >= self._next_status:
            self._next_status = time.monotonic() + 10
            self.save(self.state.get("stage", "WORKER_STARTING"))

    def shutdown(self):
        self.save("OPERATOR_STOPPED", operatorStopped=True)
        if self.worker is None:
            return
        self.request("stop")
        try:
            self.worker.wait(timeout=90)
        except subprocess.TimeoutExpired:
            terminate_owned(self.worker)
        finally:
            if self.job:
                self.job.close()

    def run(self, *, resume=False, update_only=False):
        if resume:
            self.state["resumeWorker"] = True
            self.state["operatorStopped"] = False
            self.state["failures"] = {}
            self.state["recoveryFailures"] = {}
            self.state["retryAfter"] = 0
            self.next_start = 0
        target = None
        next_fetch = 0.0
        next_install = 0.0
        future = None
        checked = False
        executor = ThreadPoolExecutor(max_workers=1)
        self.save("STARTING", error=None, pid=None, workerConfirmedAt=None, workerHeartbeatAt=None,
                  workerPid=None, workerPhase=None, workerBlocked=False, workerReason=None)
        if self.publisher:
            try:
                self.publisher.start()
            except Exception:
                self.publisher = None
                print("[managed] 版本上报暂不可用；自动更新继续运行。", flush=True)
        try:
            while True:
                now = time.monotonic()
                check_now = self.root / "check-now.json"
                if check_now.exists():
                    control = read_json(check_now)
                    if control.get("resume"):
                        self.state["resumeWorker"] = True
                        self.state["operatorStopped"] = False
                        self.state["failures"] = {}
                        self.state["recoveryFailures"] = {}
                        self.state["retryAfter"] = 0
                        self.next_start = 0
                    check_now.unlink(missing_ok=True)
                    next_fetch = 0
                if future is None and now >= next_fetch:
                    future = executor.submit(self.repo.fetch)
                    next_fetch = now + 60
                if future is not None and future.done():
                    checked = True
                    try:
                        fetched = future.result()
                        if fetched != target:
                            next_install = 0
                            if fetched != self.state.get("installed"):
                                self.next_start = 0
                        target = fetched
                        self.save(self.state["stage"], target=target, lastCheckedAt=time.time(), checkError=None)
                    except Exception as error:
                        reason = str(error) if isinstance(error, LauncherError) else type(error).__name__
                        self.save(self.state["stage"], checkError="UPDATE_CHECK_FAILED: " + reason)
                        if update_only:
                            return 1
                    future = None
                if self.worker is not None:
                    self.monitor(target)
                else:
                    head = self.repo.head()
                    wanted = target or head
                    needs_install = (wanted != head or self.state.get("installPending")
                                     or self.state.get("installed") != head)
                    if needs_install:
                        if now >= next_install and future is None:
                            try:
                                self.install(wanted)
                            except Exception as error:
                                self.save("UPDATE_FAILED", error=str(error) if isinstance(error, LauncherError) else type(error).__name__)
                                next_install = time.monotonic() + 60
                                if update_only:
                                    return 1
                            else:
                                head = wanted
                                if self.needs_reload():
                                    # The PowerShell parent restarts this stdlib
                                    # launcher only after releasing its lock.
                                    return RELOAD_EXIT
                    if not self.state.get("installPending") and self.state.get("installed") == head:
                        if update_only:
                            if checked and future is None:
                                return 0
                        elif self.can_start(head):
                            try:
                                self.start(head)
                            except Exception as error:
                                self.failure(head)
                                self.save("START_FAILED", error=type(error).__name__)
                        elif self.state.get("operatorStopped"):
                            self.save("OPERATOR_STOPPED")
                        elif self.state["failures"].get(head, 0) >= MAX_FAILURES:
                            self.save("WAITING_FIX")
                time.sleep(1)
        except KeyboardInterrupt:
            self.shutdown()
            return 0
        finally:
            if self.publisher:
                try:
                    self.publisher.stop()
                except Exception:
                    pass
            if self.worker is not None:
                terminate_owned(self.worker)
            if self.job:
                self.job.close()
            executor.shutdown(wait=True, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser(description="Managed Apex Runner launcher")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--git", default="git")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--update-only", action="store_true")
    options = parser.parse_args()
    if os.name != "nt":
        parser.error("The managed launcher runs on Windows only")
    repo = Repository(options.repo, options.git)
    root = repo.root / "windows/runs/managed"
    try:
        with FileLock(root / "launcher.lock"):
            # -I excludes the script directory. Add only this stdlib launcher
            # directory; the publisher never imports the worker environment.
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            try:
                from managed_telemetry import VersionPublisher
                publisher = VersionPublisher(repo.root, root, write_json=write_json)
            except Exception:
                publisher = None
                print("[managed] 版本上报暂不可用；自动更新继续运行。", flush=True)
            return Launcher(repo, publisher=publisher).run(resume=options.resume, update_only=options.update_only)
    except LauncherError as error:
        if str(error) == "RUNNER_ALREADY_RUNNING":
            write_json(root / "check-now.json", {"at": time.time(), "resume": options.resume})
            print("启动器已在运行，已发送继续/检查请求；正常账号结束后自动更新。")
            return 0
        print(f"启动器失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
