"""Real processes and a real venv; no game, credentials or third-party packages."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import venv

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "windows"))
import managed_launcher as launcher
from managed_telemetry import snapshot


class ManagedProcessTest(unittest.TestCase):
    def round_trip(self, *, wrapper):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = root / "venv"
            venv.EnvBuilder(with_pip=False).create(environment)
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            script = root / "worker.py"
            script.write_text('''
import time
from apex_automation.managed_runtime import ManagedRuntime, ManagedUpdateRequested
runtime = ManagedRuntime.from_environment()
try:
    while True:
        runtime.pulse("APEX_PLAYING")
        if (runtime.root / "boundary.allowed").exists():
            runtime.boundary()
        time.sleep(0.05)
except ManagedUpdateRequested:
    raise SystemExit(75)
''', encoding="utf-8")
            app = launcher.Launcher(SimpleNamespace(root=REPO, worker_python=python), root=root / "managed")
            popen = subprocess.Popen

            def launch_worker(args, **kwargs):
                command = [args[0], "-u", str(script)]
                if wrapper:
                    # Model Windows venv's redirector on every test platform.
                    command = [args[0], "-c",
                               "import subprocess,sys; sys.exit(subprocess.call(sys.argv[1:]))", *command]
                return popen(command, **kwargs)

            try:
                with patch.object(launcher.subprocess, "Popen", side_effect=launch_worker):
                    app.start("a" * 40)
                deadline = time.monotonic() + 15
                status_path = app.root / "worker.json"
                while not status_path.exists() and time.monotonic() < deadline:
                    self.assertIsNone(app.worker.poll(), "worker exited before its first heartbeat")
                    time.sleep(0.02)
                status = launcher.read_json(status_path)
                self.assertEqual(status.get("session"), app.session)
                if wrapper or os.name == "nt":
                    self.assertNotEqual(status["pid"], app.worker.pid)
                # A healthy main loop must remain healthy beyond the hang timeout.
                app.started -= 600
                app.monitor("a" * 40)
                self.assertEqual(app.state["stage"], "RUNNING")
                self.assertIsNone(app.request_mode)
                report = snapshot(app.state, device_id="test", generation=1, sequence=1, now=time.time())
                self.assertTrue(report["workerResponding"])
                self.assertEqual(report["runningCommit"], "a" * 40)
                # A new release waits for the actual account boundary, then hands off.
                app.monitor("b" * 40)
                self.assertEqual(app.request_mode, "boundary")
                self.assertIsNone(app.worker.poll())
                (app.root / "boundary.allowed").touch()
                self.assertEqual(app.worker.wait(timeout=15), launcher.UPDATE_EXIT)
                app.monitor("b" * 40)
                self.assertEqual(app.state["stage"], "UPDATE_READY")
                self.assertEqual(app.state["failures"], {})
            finally:
                if app.worker is not None:
                    launcher.terminate_owned(app.worker)
                if app.job is not None:
                    app.job.close()

    def test_real_venv_heartbeat_and_update_handoff(self):
        self.round_trip(wrapper=False)

    def test_redirected_worker_heartbeat_and_update_handoff(self):
        self.round_trip(wrapper=True)


if __name__ == "__main__":
    unittest.main()
