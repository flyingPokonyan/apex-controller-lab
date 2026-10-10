import base64
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'windows'))
from apex_automation.ea_evidence import EaLoginEvidence
from apex_automation.ea_screenshot_outbox import EaScreenshotUploader, MAX_PENDING
from apex_automation.ocr_obstacles import OcrToken


class EaScreenshotTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidence = EaLoginEvidence(self.root / 'ea-login')
        self.settings = SimpleNamespace(device_id='dev_1', report_token='test-only',
                                        report_url='https://example.test/v1/runner/reports')

    def capture(self):
        frame = np.full((100, 200, 3), 180, dtype=np.uint8)
        self.evidence.protect('fixture@example.test')
        self.evidence.step('account-typed', page='EMAIL', frame=frame,
                           tokens=[OcrToken('fixture@example.test', .99, (0, 0, 50, 50))],
                           rect=(10, 10, 100, 80), identifierVerified=True)
        return frame

    def test_original_desktop_is_not_redacted_or_cropped_and_lease_survives_rotation(self):
        self.evidence.bind_lease('lease_fixture')
        self.evidence.rotate()
        frame = self.capture()
        stored = cv2.imread(str(next(self.evidence.dir.glob('*.png'))))
        np.testing.assert_array_equal(stored, frame)
        pending = json.loads(next((self.root / 'diagnostics/ea-evidence').glob('*.json')).read_text())
        self.assertEqual(pending['leaseId'], 'lease_fixture')
        self.assertEqual((pending['width'], pending['height']), (200, 100))
        self.assertTrue(pending['details']['identifierVerified'])
        decoded = cv2.imdecode(np.frombuffer(base64.b64decode(pending['imageBase64']), np.uint8), cv2.IMREAD_COLOR)
        self.assertGreater(int(decoded[20, 20, 0]), 100)

    def test_failed_upload_survives_worker_restart_and_exact_ack_removes_it(self):
        self.capture()
        class Transport:
            status = 503
            sent = None
            def send(inner, url, token, payload, timeout):
                inner.sent = payload
                return inner.status, {'schemaVersion': 1, 'deviceId': 'dev_1', 'eventId': payload['eventId']}, {}
        transport = Transport()
        EaScreenshotUploader(self.settings, self.root, transport).process_once()
        self.assertEqual(len(list((self.root / 'diagnostics/ea-evidence').glob('*.json'))), 1)
        transport.status = 200
        uploader = EaScreenshotUploader(self.settings, self.root, transport)
        uploader.process_once()
        self.assertFalse(list((self.root / 'diagnostics/ea-evidence').glob('*.json')))
        self.assertTrue(uploader.url.endswith('/v1/runner/ea-evidence'))

    def test_outbox_is_bounded_and_clearing_lease_does_not_attribute_the_next_attempt(self):
        self.evidence.bind_lease('lease_old')
        self.evidence.bind_lease(None)
        for _ in range(MAX_PENDING + 2):
            self.capture()
        files = list((self.root / 'diagnostics/ea-evidence').glob('*.json'))
        self.assertEqual(len(files), MAX_PENDING)
        self.assertTrue(all(json.loads(p.read_text())['leaseId'] is None for p in files))

    def test_invalid_server_response_keeps_a_valid_screenshot_for_retry(self):
        self.capture()
        transport = Mock()
        transport.send.side_effect = ValueError('response was not JSON')
        EaScreenshotUploader(self.settings, self.root, transport).process_once()
        self.assertEqual(len(list((self.root / 'diagnostics/ea-evidence').glob('*.json'))), 1)

    def test_screenshot_encoder_failure_does_not_fail_the_login_step(self):
        with patch('apex_automation.ea_screenshot_outbox.enqueue', side_effect=cv2.error('encoder failed')):
            self.capture()
        self.assertIn('account-typed', self.evidence.steps_path.read_text())
        self.assertEqual(len(list(self.evidence.dir.glob('*.png'))), 1)
