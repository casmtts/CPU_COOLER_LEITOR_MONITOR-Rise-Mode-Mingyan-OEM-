# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['monitor.py'],
    pathex=[],
    binaries=[],
    datas=[('read_lhm.ps1', '.'), ('cooler_icon.png', '.'), ('cooler_icon.ico', '.'), ('LibreHardwareMonitorLib.dll', '.'), ('LibreHardwareMonitorLib_PawnIO.dll', '.'), ('RAMSPDToolkit-NDD.dll', '.'), ('install_task.ps1', '.'), ('uninstall_task.ps1', '.'), ('desativar_hvci.bat', '.')],
    hiddenimports=['wmi', 'win32com.client', 'pythoncom', 'pywintypes'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MonitorCPUCooler',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['cooler_icon.ico'],
)
