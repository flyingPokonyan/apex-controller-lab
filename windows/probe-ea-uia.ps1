$ErrorActionPreference = "Stop"

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$WindowsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$OutputDir = Join-Path $WindowsDir "runs"
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$OutputPath = Join-Path $OutputDir "ea-uia-$Timestamp.txt"
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

function Protect-VisibleName([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ""
    }
    $Safe = $Value -replace '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '[EMAIL]'
    $Safe = $Safe -replace '(?<!\d)\d{6,8}(?!\d)', '[NUMBER]'
    $Safe = $Safe -replace '[\r\n\t]+', ' '
    if ($Safe.Length -gt 160) {
        return $Safe.Substring(0, 160) + "..."
    }
    return $Safe
}

$Processes = Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.MainWindowHandle -ne 0 -and $_.ProcessName -match '^EA'
}
if (-not $Processes) {
    throw "No visible EA App window found. Open EA App and leave it on the login page."
}

$Lines = [System.Collections.Generic.List[string]]::new()
$Lines.Add("EA UI Automation probe")
$Lines.Add("CapturedAt=$((Get-Date).ToUniversalTime().ToString('o'))")
$Lines.Add("This file omits ValuePattern data and redacts email addresses and numeric codes.")

foreach ($Process in $Processes) {
    $Root = [System.Windows.Automation.AutomationElement]::FromHandle(
        [IntPtr]$Process.MainWindowHandle
    )
    if ($null -eq $Root) {
        continue
    }
    $Lines.Add("")
    $Lines.Add("WINDOW process=$($Process.ProcessName) pid=$($Process.Id)")
    $Elements = $Root.FindAll(
        [System.Windows.Automation.TreeScope]::Subtree,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    $Limit = [Math]::Min($Elements.Count, 2500)
    for ($Index = 0; $Index -lt $Limit; $Index++) {
        $Element = $Elements.Item($Index)
        try {
            $Current = $Element.Current
            $Type = $Current.ControlType.ProgrammaticName
            $Name = Protect-VisibleName $Current.Name
            $Rectangle = $Current.BoundingRectangle
            $Lines.Add(
                "CONTROL index=$Index type=$Type automationId=$($Current.AutomationId) " +
                "class=$($Current.ClassName) enabled=$($Current.IsEnabled) " +
                "offscreen=$($Current.IsOffscreen) rect=$Rectangle name=$Name"
            )
        }
        catch {
            $Lines.Add("CONTROL index=$Index unavailable=true")
        }
    }
    if ($Elements.Count -gt $Limit) {
        $Lines.Add("TRUNCATED total=$($Elements.Count) limit=$Limit")
    }
}

$Lines | Set-Content -Path $OutputPath -Encoding UTF8
Write-Host "EA UIA probe saved: $OutputPath"
