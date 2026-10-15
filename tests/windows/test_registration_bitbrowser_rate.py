"""BitBrowser Local API calls take turns machine-wide and survive refused bursts."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

WINDOWS = Path(__file__).resolve().parents[2] / "windows"
sys.path.insert(0, str(WINDOWS))
from apex_registration.recovery import recovery_action
from apex_registration.utils import bitbrowser
from apex_registration.utils.bitbrowser import BitBrowserBusy, BitBrowserClient, BitBrowserError

BUSY = {"success": False, "msg": "请求频繁，请调整请求频率"}


def response(body):
    item = Mock()
    item.raise_for_status.return_value = None
    item.json.return_value = body
    return item


class LocalApiTurnTests(unittest.TestCase):
    def setUp(self):
        lock_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"APEX_BITBROWSER_LOCK_DIR": lock_dir}))
        self.sleeps = []
        self.enterContext(patch.object(bitbrowser.time, "sleep", side_effect=self.sleeps.append))

    def client(self, *bodies):
        client = BitBrowserClient("http://127.0.0.1:54345")
        client.session = Mock()
        client.session.post.side_effect = [
            body if isinstance(body, BaseException) else response(body) for body in bodies
        ]
        return client

    def test_refused_burst_is_retried_with_backoff_then_succeeds(self):
        client = self.client(BUSY, BUSY, {"success": True, "data": {"id": "p1"}})
        self.assertEqual(client._request("/browser/update", {}), {"id": "p1"})
        self.assertEqual(client.session.post.call_count, 3)
        self.assertIn(bitbrowser.BUSY_DELAYS[0], self.sleeps)
        self.assertIn(bitbrowser.BUSY_DELAYS[1], self.sleeps)

    def test_persistent_refusal_is_reported_as_busy_not_as_missing_api(self):
        client = self.client(*([BUSY] * (len(bitbrowser.BUSY_DELAYS) + 1)))
        with self.assertRaises(BitBrowserBusy) as raised:
            client._request("/browser/open", {"id": "p1"})
        self.assertIn("请求频繁", str(raised.exception))
        self.assertTrue(self.client(BUSY, BUSY, BUSY, BUSY, BUSY).health())

    def test_unreachable_api_is_retried_briefly_then_named(self):
        down = requests.ConnectionError("refused")
        client = self.client(*([down] * (len(bitbrowser.UNREACHABLE_DELAYS) + 1)))
        with self.assertRaises(BitBrowserError) as raised:
            client._request("/browser/list", {})
        self.assertIn("not running", str(raised.exception))
        self.assertEqual(client.session.post.call_count, len(bitbrowser.UNREACHABLE_DELAYS) + 1)
        self.assertFalse(self.client(*([down] * 3)).health())

    def test_open_is_queued_and_a_failed_open_removes_the_new_profile(self):
        client = self.client(
            {"success": True, "data": {"id": "p1"}},
            {"success": False, "msg": "浏览器启动失败"},
            {"success": True, "data": {}},
            {"success": True, "data": {}},
        )
        with self.assertRaises(BitBrowserError):
            client.ensure_open(seed="s", proxy_url="socks5://u:p@proxy.example:3000")
        paths = [call.args[0].rsplit("/", 2)[-2:] for call in client.session.post.call_args_list]
        self.assertEqual(paths, [["browser", "update"], ["browser", "open"], ["browser", "close"], ["browser", "delete"]])
        open_call = client.session.post.call_args_list[1]
        self.assertEqual(open_call.kwargs["json"], {"id": "p1", "queue": True})

    def test_no_separate_health_call_before_creating_a_window(self):
        client = self.client({"success": True, "data": {"id": "p1"}}, {"success": True, "data": {"ws": "ws://x"}})
        opened = client.ensure_open(seed="s", proxy_url="http://proxy.example:8080")
        self.assertEqual(opened["cdp"], "ws://x")
        self.assertEqual(client.session.post.call_count, 2)

    def test_consecutive_calls_keep_the_minimum_gap(self):
        client = self.client({"success": True, "data": {}}, {"success": True, "data": {}})
        client._request("/browser/list", {})
        client._request("/browser/list", {})
        self.assertTrue(self.sleeps and 0 < self.sleeps[-1] <= bitbrowser.MIN_INTERVAL)


class CrossProcessTurnTests(unittest.TestCase):
    def test_two_processes_do_not_call_inside_each_other_turn(self):
        lock_dir = self.enterContext(tempfile.TemporaryDirectory())
        log = Path(lock_dir) / "calls.log"
        script = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(WINDOWS)!r})\n"
            "from apex_registration.utils import bitbrowser\n"
            "for _ in range(2):\n"
            "    with bitbrowser.api_slot():\n"
            f"        open({str(log)!r}, 'a').write(f'start {{time.time()}}\\n')\n"
            "        time.sleep(0.3)\n"
            f"        open({str(log)!r}, 'a').write(f'end {{time.time()}}\\n')\n"
        )
        env = dict(os.environ, APEX_BITBROWSER_LOCK_DIR=lock_dir)
        children = [subprocess.Popen([sys.executable, "-c", script], env=env) for _ in range(2)]
        for child in children:
            self.assertEqual(child.wait(timeout=30), 0)
        events = [line.split() for line in log.read_text().splitlines()]
        self.assertEqual([kind for kind, _ in events], ["start", "end"] * 4)
        stamps = [float(value) for _, value in events]
        for index in range(1, len(stamps) - 1, 2):
            # Each turn begins at least MIN_INTERVAL after the previous one ended.
            self.assertGreaterEqual(stamps[index + 1] - stamps[index], bitbrowser.MIN_INTERVAL - 0.05)


class BrowserFailureRecoveryTests(unittest.TestCase):
    def test_browser_failure_before_ea_submission_recreates_with_same_resources(self):
        state = dict(ea_password="generated", ea_creation_submitted=False, ea_registered=False,
                     stage="open_register", error_code="browser_unavailable")
        self.assertEqual(recovery_action(state), "create")

    def test_browser_failure_after_ea_submission_resumes_instead_of_recreating(self):
        state = dict(ea_password="saved", ea_creation_submitted=True, ea_registered=True,
                     stage="open_register", error_code="browser_unavailable")
        self.assertEqual(recovery_action(state), "resume")


if __name__ == "__main__":
    unittest.main()
