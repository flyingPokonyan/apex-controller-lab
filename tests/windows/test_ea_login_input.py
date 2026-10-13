"""Real Windows edit-control readback, including stale clipboard rejection."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'windows'))
from apex_automation.ea_login_input import read_focused_account


@unittest.skipUnless(os.name == 'nt', 'Windows clipboard and edit control')
class NativeAccountReadbackTest(unittest.TestCase):
    def test_exact_zero_survives_copy_and_clipboard_is_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / 'ready.txt'
            fixture = Path(directory) / 'field.ps1'
            fixture.write_text(r'''
Add-Type -AssemblyName System.Windows.Forms
$form = New-Object System.Windows.Forms.Form
$form.Text = 'Account input readback fixture'
$box = New-Object System.Windows.Forms.TextBox
$box.Text = 'user0@example.test'
$box.Width = 260
$form.Controls.Add($box)
$form.Add_Shown({
 $form.Activate()
 $box.Focus() | Out-Null
 [System.Windows.Forms.Clipboard]::SetText('clipboard-before-test')
 [System.IO.File]::WriteAllText($args[0], $form.Handle.ToInt64().ToString())
}.GetNewClosure())
[System.Windows.Forms.Application]::Run($form)
'''.replace('$args[0]', "'" + str(ready).replace("'", "''") + "'"), encoding='utf-8')
            process = subprocess.Popen(['powershell.exe', '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass', '-File', str(fixture)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 8
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(ready.exists(), 'native edit fixture did not open')
                hwnd = int(ready.read_text())
                foreground = ctypes.windll.user32.GetForegroundWindow
                foreground.restype = ctypes.c_void_p
                if foreground() != hwnd:
                    self.skipTest('CI desktop does not permit foreground activation')
                self.assertEqual(read_focused_account(hwnd), 'user0@example.test')
                # A wrong window cannot validate text already in the clipboard.
                self.assertIsNone(read_focused_account(hwnd + 1))
                result = subprocess.run(['powershell.exe', '-NoProfile', '-STA', '-Command',
                    "Add-Type -AssemblyName System.Windows.Forms; ConvertTo-Json -InputObject ([System.Windows.Forms.Clipboard]::GetText()) -Compress"],
                    capture_output=True, timeout=8, check=True)
                self.assertEqual(json.loads(result.stdout.decode().strip()), 'clipboard-before-test')
            finally:
                process.terminate()
                process.wait(timeout=8)
