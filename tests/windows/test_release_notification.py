import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

spec = importlib.util.spec_from_file_location("notify_release", Path(__file__).resolve().parents[2] / "scripts/notify-forge-release.py")
notify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notify)


class ReleaseNotificationTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        event = root / "event.json"
        event.write_text(json.dumps({"head_commit": {"message": "Fix page\nMore details"}}))
        self.enterContext(patch.dict(notify.os.environ, {
            "FORGE_RELEASE_URL": "https://forge.example/v1/runner/controller-releases",
            "FORGE_RELEASE_TOKEN": "test-release-secret", "GITHUB_EVENT_PATH": str(event),
            "GITHUB_REPOSITORY": "flyingPokonyan/apex-controller-lab", "GITHUB_RUN_NUMBER": "42", "GITHUB_SHA": "a" * 40,
        }))

    def test_successful_push_notifies_exact_commit_and_monotonic_run_number(self):
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = json.dumps({"accepted": True, "commit": "a" * 40}).encode()
        with patch.object(notify, "build_opener", return_value=opener):
            notify.main()
        body = json.loads(opener.open.call_args.args[0].data)
        self.assertEqual(body["commit"], "a" * 40)
        self.assertEqual(body["sequence"], 42)
        self.assertEqual(body["title"], "Fix page More details")

    def test_missing_configuration_does_not_silently_succeed(self):
        with patch.dict(notify.os.environ, {"FORGE_RELEASE_TOKEN": ""}):
            with self.assertRaises(SystemExit):
                notify.main()

    def test_failed_notification_retries_and_does_not_expose_request_secret(self):
        opener = Mock()
        opener.open.side_effect = OSError("test-release-secret")
        with patch.object(notify, "build_opener", return_value=opener), patch.object(notify.time, "sleep"):
            with self.assertRaises(SystemExit) as error:
                notify.main()
        self.assertEqual(opener.open.call_count, 4)
        self.assertNotIn("test-release-secret", str(error.exception))
