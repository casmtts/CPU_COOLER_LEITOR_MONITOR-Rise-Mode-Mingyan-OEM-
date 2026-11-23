# Executar como ADMINISTRADOR.
# Adiciona exclusoes no Microsoft Defender para a pasta do programa e para o
# PowerShell 32 bits usado pelo leitor de sensores.
$pasta = Split-Path -Parent $MyInvocation.MyCommand.Path
try {
    Add-MpPreference -ExclusionPath $pasta -ErrorAction Stop
    Write-Output "Exclusao de pasta: $pasta"
} catch { Write-Output "Erro ao excluir pasta: $($_.Exception.Message)" }

foreach ($ps in @(
    "$env:WINDIR\SysWOW64\WindowsPowerShell\v1.0\powershell.exe",
    "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
)) {
    try {
        Add-MpPreference -ExclusionProcess $ps -ErrorAction Stop
        Write-Output "Exclusao de processo: $ps"
    } catch { Write-Output "Erro: $($_.Exception.Message)" }
}

Write-Output "`nSe o powershell.exe foi colocado em quarentena, restaure-o em:"
Write-Output "Seguranca do Windows > Protecao contra virus > Historico > Quarentena"
Read-Host "Pressione Enter para sair"
