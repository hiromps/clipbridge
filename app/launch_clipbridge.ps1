param(
    [switch]$SkipDesktopShortcut,
    [switch]$SkipBrowser
)

$ErrorActionPreference = 'Stop'
$serverPath = Join-Path $PSScriptRoot 'server.py'
$configPath = Join-Path $PSScriptRoot 'config.json'
$launcherPath = Join-Path $PSScriptRoot 'ClipBridge.bat'
$baseUrl = 'http://127.0.0.1:8787/'

if (-not $SkipDesktopShortcut) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $shortcutPath = Join-Path $desktop 'ClipBridge.lnk'
    if (-not (Test-Path -LiteralPath $shortcutPath)) {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $launcherPath
        $shortcut.WorkingDirectory = $PSScriptRoot
        $shortcut.Description = 'ClipBridge を起動して開く'
        $pythonw = Get-Command pythonw.exe -ErrorAction SilentlyContinue
        if ($pythonw) { $shortcut.IconLocation = $pythonw.Source }
        $shortcut.Save()
        Write-Host "デスクトップに ClipBridge ショートカットを作成しました: $shortcutPath"
    }
}

function Get-ClipBridgeToken {
    if (-not (Test-Path -LiteralPath $configPath)) { return $null }
    return (Get-Content -LiteralPath $configPath -Raw -Encoding utf8 | ConvertFrom-Json).token
}

function Test-ClipBridge {
    $token = Get-ClipBridgeToken
    if (-not $token) { return $false }
    try {
        $result = Invoke-RestMethod -Uri ($baseUrl + 'api/health') -Headers @{ 'X-Token' = $token } -TimeoutSec 2
        return ($result.ok -eq $true)
    } catch { return $false }
}

if (-not (Test-ClipBridge)) {
    $pythonw = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $pythonw) { throw 'pythonw.exe が見つかりません。Python のインストールを確認してください。' }
    Start-Process -FilePath $pythonw.Source -ArgumentList ('"' + $serverPath + '"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 300
        if (Test-ClipBridge) { $ready = $true; break }
    }
    if (-not $ready) { throw 'サーバーが起動しませんでした。127.0.0.1:8787 の使用状況と Python を確認してください。' }
}

$token = Get-ClipBridgeToken
if (-not $token) { throw 'config.json からトークンを取得できませんでした。' }
if (-not $SkipBrowser) {
    $browserUrl = $baseUrl + '#t=' + $token
    try {
        $setup = Invoke-RestMethod -Uri ($baseUrl + 'api/setup') -Headers @{ 'X-Token' = $token } -TimeoutSec 7
        $setupUri = [Uri]$setup.url
        if ($setupUri.Scheme -eq 'https' -and $setupUri.Host.EndsWith('.ts.net')) {
            $httpsBase = $setupUri.GetLeftPart([UriPartial]::Authority) + '/'
            $health = Invoke-RestMethod -Uri ($httpsBase + 'api/health') -Headers @{ 'X-Token' = $token } -TimeoutSec 5
            if ($health.ok -eq $true) {
                $browserUrl = $httpsBase + '#t=' + $token
            }
        }
    } catch {
        Write-Host 'Tailscale の HTTPS 接続を確認できないため、このPC用の画面を開きます。'
    }
    Start-Process -FilePath $browserUrl
}
Write-Host 'ClipBridge を開きました。'
