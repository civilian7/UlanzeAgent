# Register/remove a Task Scheduler entry that starts the STT app at logon.
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads BOM-less UTF-8 as
# CP949 and non-ASCII bytes can swallow line breaks.
#   .\autostart.ps1            register (updates if it already exists)
#   .\autostart.ps1 -Remove    remove
param([switch]$Remove)

$name = "UlanzeSTT"
if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed: $name"
    return
}

$app = $PSScriptRoot
$pyw = Join-Path $app ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) { throw "pythonw.exe not found: $pyw (run 'uv sync' first)" }

$action  = New-ScheduledTaskAction -Execute $pyw -Argument "stt.py" -WorkingDirectory $app
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT15S"   # give USB devices and the audio service time to come up

$settingsArgs = @{
    AllowStartIfOnBatteries    = $true
    DontStopIfGoingOnBatteries = $true
    ExecutionTimeLimit         = [TimeSpan]::Zero
    RestartCount               = 3
    RestartInterval            = (New-TimeSpan -Minutes 1)
    MultipleInstances          = "IgnoreNew"
}
$settings  = New-ScheduledTaskSettingsSet @settingsArgs
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

$regArgs = @{
    TaskName    = $name
    Action      = $action
    Trigger     = $trigger
    Settings    = $settings
    Principal   = $principal
    Description = "AU05 Voice Key to Whisper voice input"
    Force       = $true
}
Register-ScheduledTask @regArgs | Out-Null
Write-Host "Registered: $name (starts 15s after logon)"
