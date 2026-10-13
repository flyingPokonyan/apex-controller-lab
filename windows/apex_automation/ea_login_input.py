"""Exact readback of the focused account field; never used for passwords/OTP."""
import base64
import json
import os
import subprocess


def read_focused_account(hwnd):
    if os.name != 'nt':
        return None
    # Snapshot clipboard formats before copying, and restore them only if our
    # copy is still current. A fresh sequence plus the same foreground window
    # prevents an old clipboard value from confirming a failed input.
    script = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class AccountReadback {
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern uint GetClipboardSequenceNumber();
}
'@
$expectedWindow = [IntPtr]WINDOW_HANDLE
$saved = New-Object System.Windows.Forms.DataObject
$previous = [System.Windows.Forms.Clipboard]::GetDataObject()
if ($null -ne $previous) {
 foreach ($format in $previous.GetFormats($false)) {
  $saved.SetData($format, $false, $previous.GetData($format, $false))
 }
}
$copySequence = $null
try {
 if ([AccountReadback]::GetForegroundWindow() -ne $expectedWindow) { return }
 $before = [AccountReadback]::GetClipboardSequenceNumber()
 [System.Windows.Forms.SendKeys]::SendWait('^a')
 [System.Windows.Forms.SendKeys]::SendWait('^c')
 for ($i=0; $i -lt 12; $i++) {
  Start-Sleep -Milliseconds 50
  $sequence = [AccountReadback]::GetClipboardSequenceNumber()
  if ($sequence -ne $before) {
   $copySequence = $sequence
   if ([AccountReadback]::GetForegroundWindow() -ne $expectedWindow) { return }
   $value = [System.Windows.Forms.Clipboard]::GetText()
   if ($value.Length -le 512) { ConvertTo-Json -InputObject $value -Compress }
   return
  }
 }
} finally {
 if ($null -ne $copySequence -and [AccountReadback]::GetClipboardSequenceNumber() -eq $copySequence) {
  if ($null -eq $previous) { [System.Windows.Forms.Clipboard]::Clear() }
  else { [System.Windows.Forms.Clipboard]::SetDataObject($saved, $true) }
 }
}
'''.replace('WINDOW_HANDLE', str(int(hwnd)))
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    try:
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-STA', '-EncodedCommand', encoded],
            capture_output=True, timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        if result.returncode:
            return None
        value = json.loads(result.stdout.decode('utf-8-sig').strip())
        return value if isinstance(value, str) and len(value) <= 512 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
