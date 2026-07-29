from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.cli import validate


MATCH_CONFIG = REPOSITORY_ROOT / "windows" / "config" / "first-match-2560x1440.zh-CN.json"


class ValidateReportingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.payload = json.loads(MATCH_CONFIG.read_text(encoding="utf-8"))
        # Offline OCR needs a downloaded engine, which a unit test must not
        # depend on; template reporting is what these cases are about.
        self.payload["ocr"] = {"enabled": False}
        self.payload["guidedOcr"] = {"enabled": False}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self) -> tuple[int, str]:
        path = Path(self.temporary.name) / "config.json"
        path.write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = validate(path)
        return code, buffer.getvalue()

    def test_a_source_clone_without_samples_skips_rather_than_fails(self) -> None:
        self.payload["offlineCases"] = [
            ["calibration/raw/nope-01.png", "CONTINUE"],
            ["calibration/raw/nope-02.png", "LOBBY_READY"],
        ]
        code, output = self._run()

        self.assertEqual(code, 0)
        self.assertIn("样本未随源码分发", output)
        self.assertNotIn("FAIL", output)
        self.assertIn("没有可验证的样本", output)

    def test_a_genuine_mismatch_still_fails(self) -> None:
        self.payload["offlineCases"] = [
            ["calibration/raw/01-continue.png", "LOBBY_READY"],
        ]
        code, output = self._run()

        self.assertEqual(code, 1)
        self.assertIn("FAIL", output)

    def test_present_samples_are_reported_even_when_others_are_missing(self) -> None:
        self.payload["offlineCases"] = [
            ["calibration/raw/01-continue.png", "CONTINUE"],
            ["calibration/raw/nope.png", "LOBBY_READY"],
        ]
        code, output = self._run()

        self.assertEqual(code, 0)
        self.assertIn("PASS 01-continue.png", output)
        self.assertIn("SKIP nope.png", output)
        self.assertNotIn("没有可验证的样本", output)


if __name__ == "__main__":
    unittest.main()
