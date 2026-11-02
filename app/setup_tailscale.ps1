# Windows PowerShell 5.1 treats native stderr (including --help text) as a PowerShell error.
# Check each native command's exit code explicitly.
$ErrorActionPreference = 'Continue'

if (-not (Get-Command tailscale -ErrorAction SilentlyContinue)) {
    throw 'Tailscale が見つかりません。Tailscale をインストールしてください。'
}

$helpText = (& tailscale serve --help 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) { throw 'tailscale serve --help を確認できませんでした。' }
if ($helpText -notmatch '--bg' -or $helpText -notmatch '--https') {
    throw 'この Tailscale の serve 構文に --bg / --https がありません。Tailscale を更新してください。'
}

$serveOutput = (& tailscale serve --bg --https=443 127.0.0.1:8787 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) {
    if ($serveOutput -match 'Access is denied|アクセスが拒否') {
        throw 'Tailscale の管理者パイプにアクセスできません。管理者として開いた PowerShell でこのスクリプトを再実行してください。'
    }
    throw ('HTTPS Serve に失敗しました。管理画面で MagicDNS と HTTPS Certificates が有効か確認してください。詳細: ' + $serveOutput.Trim())
}
& tailscale serve status
if ($LASTEXITCODE -ne 0) { throw 'Serve の状態を取得できませんでした。' }

try {
    $status = (& tailscale status --json | Out-String | ConvertFrom-Json)
    $dnsName = [string]$status.Self.DNSName
    if (-not $dnsName) { throw 'empty DNS name' }
    $dnsName = $dnsName.TrimEnd('.')
    Write-Host "URL: https://$dnsName/"
} catch {
    Write-Host 'MagicDNS 名を取得できませんでした。Tailscale の接続状態を確認してください。'
}
