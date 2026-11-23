# Le os sensores diretamente da DLL do LibreHardwareMonitor (32 bits).
# Chamado por monitor.pyatraves do PowerShell de 32 bits.
# Com -Once: le uma vez e sai. Sem -Once: fica em loop imprimindo uma linha JSON
# a cada IntervalMs milissegundos.
param(
    [int]$IntervalMs = 1000,
    [switch]$Once,
    [string]$Dll = '',
    [ValidateSet('auto','pawnio','winring0')][string]$DriverMode = 'auto'
)

$ErrorActionPreference = 'Continue'
# Compatibilidade com os dois drivers:
#  - PawnIO  -> DLL 0.9.5 (funciona com Memory Integrity/HVCI ligada)
#  - WinRing0 -> DLL 0.9.1 (classica, exige HVCI desligado)
$pawnService = Get-Service -Name 'PawnIO' -ErrorAction SilentlyContinue
$pawnInstalled = ($pawnService -and $pawnService.Status -eq 'Running') -or
                 (Test-Path "$env:ProgramFiles\PawnIO\PawnIO.sys") -or
                 (Test-Path "$env:windir\System32\drivers\PawnIO.sys") -or
                 (Test-Path (Join-Path $PSScriptRoot 'PawnIO.sys')) -or
                 [bool](Get-ChildItem "$env:windir\System32\DriverStore\FileRepository" `
                     -Filter 'pawnio.inf*' -Directory -ErrorAction SilentlyContinue)
$pawnDll = Join-Path $PSScriptRoot 'LibreHardwareMonitorLib_PawnIO.dll'
$legacyDll = Join-Path $PSScriptRoot 'LibreHardwareMonitorLib.dll'
if ([string]::IsNullOrWhiteSpace($Dll)) {
    if ($DriverMode -eq 'pawnio' -and (Test-Path $pawnDll)) {
        $Dll = $pawnDll
    } elseif ($DriverMode -eq 'winring0' -and (Test-Path $legacyDll)) {
        $Dll = $legacyDll
    } elseif ($pawnInstalled -and (Test-Path $pawnDll)) {
        $Dll = $pawnDll
    } elseif (Test-Path $legacyDll) {
        $Dll = $legacyDll
    }
}
$driverMode = if ($Dll -like '*PawnIO*') { 'PawnIO' } else { 'WinRing0' }
if (-not (Test-Path $Dll)) {
    Write-Error "LibreHardwareMonitorLib.dll nao encontrada: $Dll"
    exit 1
}
[System.Threading.Thread]::CurrentThread.CurrentCulture = [System.Globalization.CultureInfo]::InvariantCulture
Add-Type -Path $Dll

# A DLL 0.9.5 ja le a temperatura das DIMMs via PawnIO. Nao carregamos a
# biblioteca SPD separadamente: ela abre outro driver e atrapalha o PawnIO.
$spdReady = $false
$spdBuses = @()

function Get-RamTempSpd {
    if (-not $spdReady) { return 0 }
    $temps = @()
    foreach ($bus in $spdBuses) {
        foreach ($addr in 0x50..0x57) {
            foreach ($name in @('SPD_DDR4_SDRAM', 'SPD_DDR5_SDRAM')) {
                try {
                    $mt = [Enum]::Parse(
                        [RAMSPDToolkit.SPD.Interop.Shared.SPDMemoryType], $name)
                    $det = [RAMSPDToolkit.SPD.SPDDetector]::new(
                        $bus, [byte]$addr, $mt)
                    if ($det.IsValid -and $det.Accessor.HasThermalSensor) {
                        $det.Accessor.UpdateTemperature()
                        $t = [double]$det.Accessor.Temperature
                        if ($t -gt 0 -and $t -lt 125) { $temps += $t }
                    }
                } catch { }
            }
        }
    }
    if ($temps.Count -eq 0) { return 0 }
    return [math]::Round(($temps | Measure-Object -Maximum).Maximum, 1)
}

# Le uma vez no inicio (os dados de SPD nao mudam rapido).
$ramTempSpd = Get-RamTempSpd

# Sensores de temperatura de memoria reportados pelo LHM (DIMM #1..#4).
function Get-RamDimmTemps([array]$all) {
    $vals = @()
    foreach ($s in $all) {
        if ($s.SensorType -eq 'Temperature' -and
            ($s.Name -like 'DIMM*' -or $s.Identifier -like '/ram/temperature/*')) {
            $v = Safe-Value $s.Value
            if ($v -gt 0) { $vals += $v }
        }
    }
    return @($vals | Sort-Object)
}

$computer = New-Object LibreHardwareMonitor.Hardware.Computer
$computer.IsCpuEnabled = $true
$computer.IsGpuEnabled = $true
$computer.IsMotherboardEnabled = $true
$computer.IsMemoryEnabled = $true
$computer.IsStorageEnabled = $true
$computer.Open()
# Aquecimento: apos carregar o WinRing0, o sensor da CPU pode levar alguns
# ciclos para comecar a reportar o valor real.
for ($i = 0; $i -lt 2; $i++) {
    foreach ($hardware in $computer.Hardware) {
        try { $hardware.Update() } catch { }
    }
    Start-Sleep -Milliseconds 50
}

function Safe-Value([object]$raw) {
    # Sensores podem devolver NaN/Infinito; JSON invalido derrubaria o leitor.
    try {
        $v = [double]$raw
        if ([double]::IsNaN($v) -or [double]::IsInfinity($v) -or $v -lt 0 -or $v -gt 10000) {
            return 0
        }
        return [math]::Round($v, 1)
    } catch {
        return 0
    }
}

function Get-MaxValue([array]$all, [string]$idPattern, [string]$namePattern) {
    $vals = @($all | Where-Object {
        $_.Identifier -like $idPattern -and
        ($namePattern -eq '' -or $_.Name -like $namePattern) -and
        $null -ne $_.Value
    } | ForEach-Object { Safe-Value $_.Value })
    if ($vals.Count -eq 0) { return 0 }
    return ($vals | Measure-Object -Maximum).Maximum
}

function Get-Value([array]$all, [string]$idPattern, [string]$namePattern, [bool]$requirePositive) {
    $candidates = @($all | Where-Object {
        $_.Identifier -like $idPattern -and
        ($namePattern -eq '' -or $_.Name -like $namePattern) -and
        $null -ne $_.Value
    })
    if ($requirePositive) {
        $positive = @($candidates | Where-Object {
            $n = Safe-Value $_.Value
            $n -gt 0
        })
        if ($positive.Count -gt 0) { return (Safe-Value $positive[0].Value) }
    }
    if ($candidates.Count -gt 0) { return (Safe-Value $candidates[0].Value) }
    return 0
}

try {
    do {
        $sensors = @()
        foreach ($hardware in $computer.Hardware) {
            try { $hardware.Update() } catch { }
            foreach ($s in $hardware.Sensors) { $sensors += $s }
            foreach ($sub in $hardware.SubHardware) {
                try { $sub.Update() } catch { }
                foreach ($s in $sub.Sensors) { $sensors += $s }
            }
        }

        $cpuTemp = Get-Value $sensors '/amdcpu/*/temperature/*' '*Tctl*' $true
        if ($cpuTemp -eq 0) { $cpuTemp = Get-Value $sensors '/*cpu/*/temperature/*' '' $true }
        $gpuTemp = Get-Value $sensors '/gpu-amd/*/temperature/*' '*Core*' $true
        if ($gpuTemp -eq 0) { $gpuTemp = Get-Value $sensors '/gpu-*/*/temperature/*' '' $true }

        $ramDimms = Get-RamDimmTemps $sensors
        $ramMax = 0
        if ($ramTempSpd -gt 0) { $ramMax = $ramTempSpd }
        elseif ($ramDimms.Count -gt 0) {
            $ramMax = ($ramDimms | Measure-Object -Maximum).Maximum
        }

        $result = [ordered]@{
            ts         = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
            driver     = $driverMode
            cpu_temp   = $cpuTemp
            gpu_temp   = $gpuTemp
            cpu_load   = Get-Value $sensors '/amdcpu/0/load/0' '' $false
            mem_load   = Get-Value $sensors '/ram/load/*' '' $false
            board_temp = Get-Value $sensors '/lpc/*/temperature/*' '' $true
            gpu_load   = Get-Value $sensors '/gpu-*/load/*' '*GPU Core*' $true
            ram_temp   = $ramMax
            ram_dimm1  = if ($ramDimms.Count -ge 1) { $ramDimms[0] } else { 0 }
            ram_dimm2  = if ($ramDimms.Count -ge 2) { $ramDimms[1] } else { 0 }
            ram_dimm3  = if ($ramDimms.Count -ge 3) { $ramDimms[2] } else { 0 }
            ram_dimm4  = if ($ramDimms.Count -ge 4) { $ramDimms[3] } else { 0 }
            ram_total  = [math]::Round((Get-Value $sensors '/ram/data/0' '' $false) +
                                      (Get-Value $sensors '/ram/data/1' '' $false), 1)
            pump_rpm   = 0
            cpu_rpm    = 0
            sys_rpm    = 0
            fan3_rpm   = 0
        }
        # Canais de fan/PUMP do Super I/O (NCT6799D).
        $fanByIndex = @{}
        foreach ($s in $sensors) {
            if ($s.Identifier -like '/lpc/*/fan/*') {
                $idx = [int]($s.Identifier -replace '.*fan/', '')
                $fanByIndex[$idx] = Safe-Value $s.Value
            }
        }
        $fansAtivos = @($fanByIndex.Values | Where-Object { $_ -gt 0 } |
                        Sort-Object -Descending)
        $result.pump_rpm = if ($fanByIndex.ContainsKey(0)) { $fanByIndex[0] } else { 0 }
        $result.cpu_rpm = if ($fanByIndex.ContainsKey(1)) { $fanByIndex[1] } else { 0 }
        $result.sys_rpm = if ($fanByIndex.ContainsKey(4)) { $fanByIndex[4] } else { 0 }
        $result.fan3_rpm = if ($fanByIndex.ContainsKey(2)) { $fanByIndex[2] } else { 0 }
        if ($result.cpu_rpm -eq 0 -and $fansAtivos.Count -ge 1) { $result.cpu_rpm = $fansAtivos[0] }
        if ($result.sys_rpm -eq 0 -and $fansAtivos.Count -ge 2) { $result.sys_rpm = $fansAtivos[1] }

        if ($result.cpu_load -eq 0) { $result.cpu_load = Get-Value $sensors '/intelcpu/0/load/0' '' $false }
        $gpuCore = Get-MaxValue $sensors '/gpu-*/load/*' '*GPU Core*'
        $gpuD3d = Get-MaxValue $sensors '/gpu-*/load/*' '*D3D 3D*'
        $result.gpu_load = [math]::Max($gpuCore, $gpuD3d)
        if ($result.ram_temp -eq 0) { $result.ram_temp = Get-Value $sensors '/*ram*/temperature/*' '' $true }

        $result | ConvertTo-Json -Compress
        if ($Once) { break }
        Start-Sleep -Milliseconds $IntervalMs
    } while ($true)
}
finally {
    $computer.Close()
}
