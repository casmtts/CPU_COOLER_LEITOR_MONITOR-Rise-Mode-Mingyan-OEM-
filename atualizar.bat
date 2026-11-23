@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  Monitor CPU Cooler - recompilar e reinstalar no logon
echo ============================================================
echo.
echo  O .exe versionado no repositorio e antigo. Este script gera
echo  o executavel com o codigo atual e recria a tarefa agendada
echo  com --start-minimized (abre direto na bandeja apos reiniciar).
echo.

tasklist /FI "IMAGENAME eq MonitorCPUCooler.exe" 2>NUL | find /I "MonitorCPUCooler.exe" >NUL
if not errorlevel 1 (
    echo [AVISO] O monitor esta em execucao.
    echo Feche-o pelo icone da bandeja ^> Sair e execute este script de novo.
    echo.
    pause
    exit /b 1
)

where python >NUL 2>NUL
if errorlevel 1 (
    echo [ERRO] Python nao encontrado no PATH.
    echo Instale o Python 3.12 (64 bits) marcando "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

echo [1/3] Instalando dependencias...
python -m pip install -r requirements.txt
if errorlevel 1 goto falha
python -m pip install pyinstaller
if errorlevel 1 goto falha

echo.
echo [2/3] Compilando o executavel...
python -m PyInstaller --noconfirm --onefile --noconsole ^
  --name MonitorCPUCooler ^
  --icon cooler_icon.ico ^
  --add-data "read_lhm.ps1;." ^
  --add-data "cooler_icon.png;." ^
  --add-data "cooler_icon.ico;." ^
  --add-data "LibreHardwareMonitorLib.dll;." ^
  --add-data "LibreHardwareMonitorLib_PawnIO.dll;." ^
  --add-data "RAMSPDToolkit-NDD.dll;." ^
  --add-data "install_task.ps1;." ^
  --add-data "uninstall_task.ps1;." ^
  --add-data "desativar_hvci.bat;." ^
  --hidden-import=wmi --hidden-import=win32com.client ^
  --hidden-import=pythoncom --hidden-import=pywintypes monitor.py
if errorlevel 1 goto falha

copy /Y "dist\MonitorCPUCooler.exe" "MonitorCPUCooler.exe" >NUL
if errorlevel 1 goto falha

echo.
echo [3/3] Recriando a tarefa agendada (pode pedir UAC uma vez)...
"MonitorCPUCooler.exe" --install-startup
if errorlevel 1 goto falha

echo.
echo ============================================================
echo  CONCLUIDO! Reinicie o PC para testar.
echo  No logon o monitor fica na bandeja (sem janela na tela).
echo  Para abrir: clique no icone da bandeja ^> Abrir monitor.
echo ============================================================
echo.
pause
exit /b 0

:falha
echo.
echo [ERRO] Falha na etapa anterior. Leia a mensagem acima.
echo.
pause
exit /b 1
