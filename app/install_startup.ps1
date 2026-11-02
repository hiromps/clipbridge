$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'server.py'
$python = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if (-not $python) {
    $pythonExe = Get-Command python.exe -ErrorAction Stop
    $candidate = Join-Path (Split-Path $pythonExe.Source) 'pythonw.exe'
    if (-not (Test-Path $candidate)) { throw 'pythonw.exe が見つかりません。Python を確認してください。' }
    $pythonPath = $candidate
} else { $pythonPath = $python.Source }

$action = New-ScheduledTaskAction -Execute $pythonPath -Argument ('"' + $scriptPath + '"') -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'ClipBridge' -Action $action -Trigger $trigger -Principal $principal -Description 'ClipBridge local clipboard server' -Force | Out-Null
Write-Host 'ClipBridge タスクを登録・更新しました。次回ログオン時に起動します。'
