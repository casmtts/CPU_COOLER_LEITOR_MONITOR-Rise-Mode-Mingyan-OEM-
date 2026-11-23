$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $dir 'auto_start.log'
$lines = @("=== $(Get-Date) ===")
try {
    $out = & schtasks /Delete /TN 'MonitorCPUCooler' /F 2>&1
    $lines += "schtasks: $out"
    Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'MonitorCPUCooler' -ErrorAction SilentlyContinue
    $lines += 'OK'
} catch {
    $lines += "ERRO: $($_.Exception.Message)"
}
$lines | Set-Content -LiteralPath $log -Encoding UTF8
