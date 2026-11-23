@echo off
title Desativar Memory Integrity (HVCI) - liberar WinRing0
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo.
  echo  ERRO: execute este arquivo como ADMINISTRADOR.
  echo  Clique com o botao direito > "Executar como administrador".
  echo.
  pause
  exit /b 1
)

echo Desativando a Integridade da memoria (HVCI)...
reg add "HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity" /v Enabled /t REG_DWORD /d 0 /f

echo.
echo Pronto. REINICIE o computador para aplicar.
echo Depois rode: python monitor.py --driver
echo.
pause
