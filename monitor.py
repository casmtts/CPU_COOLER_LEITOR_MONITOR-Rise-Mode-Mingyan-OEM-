#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor de CPU Cooler.

Ao iniciar, este programa:
  1. inicia automaticamente o leitor do LibreHardwareMonitor (read_lhm.ps1,
     via PowerShell 32 bits, lendo a DLL diretamente) -- nao precisa abrir o
     aplicativo original;
  2. le a temperatura da CPU (e demais telemetria) do leitor;
  3. envia a telemetria para o display do cooler via HID (VID 0xAA88 / PID 0x8666).

O leitor filho e encerrado automaticamente quando o monitor sai.

Protocolo HID (engenharia reversa do "CPU COOLER V4.0"):
  report ID 0 + payload[24]:
    [0]=0, [1]=CPU temp, [2]=GPU temp, [3]=CPU%, [4]=RAM%, [5]=board temp,
    [6]=GPU%, [7..8]=RPM sys fan, [9..10]=RPM CPU fan

Uso:
  python monitor.py --probe       # inspeciona o HID
  python monitor.py --once        # le uma vez, envia e sai
  python monitor.py --dry-run     # le e mostra o payload, sem gravar no HID
  python monitor.py               # loop continuo (leitor + HID juntos)
  python monitor.py --interval 0.5
"""

import argparse
import collections
import ctypes
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
import winreg
from ctypes import wintypes

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
VID = 0xAA88
PID = 0x8666
PAYLOAD_LEN = 24
FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    # PyInstaller onefile extrai para _MEIPASS e apaga ao sair. Se o leitor
    # PowerShell mantiver arquivos abertos la, a exclusao falha. Por isso
    # copiamos os arquivos de dados para a pasta do executavel.
    HERE = sys._MEIPASS
    DATA_DIR = os.path.dirname(sys.executable)
    for _name in ("read_lhm.ps1", "cooler_icon.png", "cooler_icon.ico",
                  "LibreHardwareMonitorLib.dll",
                  "LibreHardwareMonitorLib_PawnIO.dll",
                  "RAMSPDToolkit-NDD.dll", "install_task.ps1",
                  "uninstall_task.ps1", "desativar_hvci.bat"):
        try:
            shutil.copyfile(os.path.join(HERE, _name),
                            os.path.join(DATA_DIR, _name))
        except Exception:
            pass
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = HERE
READER_SCRIPT = os.path.join(DATA_DIR, "read_lhm.ps1")


# Buffers de log: tudo que o programa imprime fica gardado para o painel
# "Ver log / terminal" do GUI. No .exe tambem vai para monitor.log.
LOG_LINES = collections.deque(maxlen=600)


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            if stream is None:
                continue
            try:
                stream.write(data)
            except Exception:
                pass
        for line in str(data).splitlines():
            if line.strip():
                LOG_LINES.append(line)

    def flush(self):
        for stream in self.streams:
            if stream is None:
                continue
            try:
                stream.flush()
            except Exception:
                pass


_log_file = None
try:
    if getattr(sys, "frozen", False):
        # "w" evita que o log cresca indefinidamente entre execucoes.
        _log_file = open(
            os.path.join(os.path.dirname(sys.executable), "monitor.log"),
            "w", encoding="utf-8")
except Exception:
    _log_file = None

sys.stdout = _Tee(sys.stdout, _log_file)
sys.stderr = _Tee(sys.stderr, _log_file)


_SINGLE_INSTANCE = None


def acquire_single_instance():
    """Evita duas instancias disputando o mesmo dispositivo HID."""
    global _SINGLE_INSTANCE
    try:
        # kernel32 foi criado com use_last_error=True: essencial para ler
        # ERROR_ALREADY_EXISTS logo apos CreateMutexW.
        kernel = kernel32
        kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                        wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        _SINGLE_INSTANCE = kernel.CreateMutexW(
            None, True, "Local\\MonitorCPUCooler_SingleInstance")
        if not _SINGLE_INSTANCE:
            return True
        return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def winring0_present():
    """True se o device do driver WinRing0 estiver carregado no sistema."""
    try:
        handle = kernel32.CreateFileW(
            r"\\.\WinRing0_1_2_0", GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
        if handle and handle != INVALID_HANDLE_VALUE:
            kernel32.CloseHandle(handle)
            return True
    except Exception:
        pass
    return False


def set_app_user_model_id():
    """Fixa o grupo da barra de tarefas no nosso icone (evita icone padrao).

    O ID muda quando o icone e alterado para forcar o Windows a criar um novo
    botao na barra (o antigo fica preso no cache do Explorer).
    """
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "MonitorCPUCooler.IconV2.2026")
        # Avisa o Shell para recarregar associacoes/icones.
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
    except Exception:
        pass


def release_single_instance():
    """Libera o mutex para que a tarefa elevada possa assumir."""
    global _SINGLE_INSTANCE
    if _SINGLE_INSTANCE:
        try:
            kernel32.CloseHandle(_SINGLE_INSTANCE)
        except Exception:
            pass
        _SINGLE_INSTANCE = None


def app_target():
    """Alvo para relancamento/startup: o proprio .exe ou o script .py."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.abspath(__file__)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
hid_dll = ctypes.WinDLL("hid", use_last_error=True)
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8),
    ]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD), ("Reserved", ctypes.c_void_p),
    ]


class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("DevicePath", wintypes.WCHAR * 1)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_void_p),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", ctypes.c_ushort), ("UsagePage", ctypes.c_ushort),
        ("InputReportByteLength", ctypes.c_ushort),
        ("OutputReportByteLength", ctypes.c_ushort),
        ("FeatureReportByteLength", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort * 17),
        ("NumberLinkCollectionNodes", ctypes.c_ushort),
        ("NumberInputButtonCaps", ctypes.c_ushort),
        ("NumberInputValueCaps", ctypes.c_ushort),
        ("NumberInputDataIndices", ctypes.c_ushort),
        ("NumberOutputButtonCaps", ctypes.c_ushort),
        ("NumberOutputValueCaps", ctypes.c_ushort),
        ("NumberOutputDataIndices", ctypes.c_ushort),
        ("NumberFeatureButtonCaps", ctypes.c_ushort),
        ("NumberFeatureValueCaps", ctypes.c_ushort),
        ("NumberFeatureDataIndices", ctypes.c_ushort),
    ]


kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.WriteFile.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
]
kernel32.WriteFile.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

hid_dll.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
hid_dll.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hid_dll.HidD_GetPreparsedData.restype = wintypes.BOOL
hid_dll.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
hid_dll.HidP_GetCaps.restype = wintypes.DWORD
hid_dll.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid_dll.HidD_FreePreparsedData.restype = wintypes.BOOL

setupapi.SetupDiGetClassDevsW.argtypes = [
    ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD,
]
setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, ctypes.POINTER(GUID), wintypes.DWORD,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
]
setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), wintypes.LPVOID,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

DIGCF_PRESENT = 0x00000002
DIGCF_DEVICEINTERFACE = 0x00000010


# ---------------------------------------------------------------------------
# Leitor do LibreHardwareMonitor (processo filho, iniciado automaticamente)
# ---------------------------------------------------------------------------
def powershell32():
    """Caminho do PowerShell 32 bits (necessario para carregar a DLL x86)."""
    for candidate in (
        r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe",
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    ):
        if os.path.exists(candidate):
            return candidate
    return "powershell.exe"


class LhmReader:
    """Gerencia o processo que le a DLL do LibreHardwareMonitor."""

    def __init__(self, interval_ms=500, once=False, driver_mode="auto"):
        cmd = [
            powershell32(), "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", READER_SCRIPT, "-IntervalMs", str(int(interval_ms)),
        ]
        # O script escolhe PawnIO ou WinRing0 (ou o modo forçado).
        cmd.extend(["-DriverMode", driver_mode])
        if once:
            cmd.append("-Once")
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.interval_ms = interval_ms
        self._job = None
        self._bind_to_job()

    def _bind_to_job(self):
        """Mata o leitor automaticamente se o monitor encerrar abruptamente."""
        try:
            k = kernel32
            k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            k.CreateJobObjectW.restype = wintypes.HANDLE
            k.SetInformationJobObject.argtypes = [
                wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
            k.SetInformationJobObject.restype = wintypes.BOOL
            k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k.AssignProcessToJobObject.restype = wintypes.BOOL

            self._job = k.CreateJobObjectW(None, None)
            if not self._job:
                return
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = \
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k.SetInformationJobObject(
                    self._job, JobObjectExtendedLimitInformation,
                    ctypes.byref(info), ctypes.sizeof(info)):
                return
            k.AssignProcessToJobObject(self._job, self.proc.handle)
        except Exception:
            self._job = None

        self._lines = queue.Queue()
        self._reader_thread = threading.Thread(target=self._drain, daemon=True)
        self._reader_thread.start()

    def _drain(self):
        """Le o stdout em thread separada para read() poder ter timeout."""
        try:
            for line in self.proc.stdout:
                self._lines.put(line)
        except Exception:
            pass
        self._lines.put(None)

    def read_nowait(self):
        """Retorna None se ainda nao ha leitura; nunca bloqueia a GUI."""
        try:
            line = self._lines.get_nowait()
        except queue.Empty:
            return None
        return self._parse_line(line)

    def read(self, timeout=30):
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError("O leitor do LibreHardwareMonitor travou (timeout).")
        return self._parse_line(line)

    def _parse_line(self, line):
        # Se o loop principal ficou bloqueado (ex.: pop-up modal), a fila pode
        # ter acumulado leituras antigas. Descarta todas e usa a mais recente
        # para que o visor nunca fique "atrasado" em relacao ao sensor.
        while True:
            try:
                newer = self._lines.get_nowait()
            except queue.Empty:
                break
            if newer is None:
                break
            line = newer

        if line is None:
            raise RuntimeError(
                "O leitor do LibreHardwareMonitor encerrou inesperadamente."
            )
        return json.loads(line)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream:
                stream.close()
        if getattr(self, "_job", None):
            try:
                kernel32.CloseHandle(self._job)
            except Exception:
                pass
            self._job = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop()


# ---------------------------------------------------------------------------
# Leitura via WMI (fallback, exige o aplicativo LHM original em execucao)
# ---------------------------------------------------------------------------
def read_sensors_wmi():
    import wmi  # so importado se necessario

    sensors = wmi.WMI(namespace="root/LibreHardwareMonitor").Sensor()

    def pick(candidates):
        for ident, name_hint in candidates:
            for s in sensors:
                sid = (s.Identifier or "").lower()
                if ident and ident.lower() not in sid:
                    continue
                if name_hint and name_hint.lower() not in (s.Name or "").lower():
                    continue
                if s.Value is not None:
                    return int(s.Value)
        return 0

    return {
        "cpu_temp": pick([("/amdcpu/0/temperature/", "tctl"),
                          ("/intelcpu/0/temperature/", "package"),
                          ("/amdcpu/0/temperature/", None)]),
        "gpu_temp": pick([("/gpu-amd/", "gpu core"), ("/gpu-nvidia/", "gpu core")]),
        "cpu_load": pick([("/amdcpu/0/load/0", None), ("/intelcpu/0/load/0", None)]),
        "mem_load": pick([("/ram/load/", None)]),
        "board_temp": pick([("/lpc/", "system"), ("/lpc/", None)]),
        "gpu_load": pick([("/gpu-amd/0/load/0", None)]),
        "cpu_rpm": 0,
        "sys_rpm": 0,
    }


ZERO_SENSORS = {k: 0 for k in ("cpu_temp", "gpu_temp", "cpu_load", "mem_load",
                               "board_temp", "gpu_load", "ram_temp",
                               "ram_total", "ram_dimm1", "ram_dimm2",
                               "ram_dimm3", "ram_dimm4",
                               "pump_rpm", "cpu_rpm", "sys_rpm", "fan3_rpm")}


def normalize(values):
    """Garante todas as chaves, converte para float e elimina NaN/Inf.

    Sensores do LHM podem devolver NaN/Infinity; sem esse filtro o int()
    usado no payload e na GUI lanca ValueError e derruba o programa.
    """
    out = dict(ZERO_SENSORS)
    for key in out:
        try:
            number = float(values.get(key, 0) or 0)
            if not math.isfinite(number) or number < 0 or number > 10000:
                number = 0.0
            out[key] = number
        except (TypeError, ValueError, OverflowError):
            out[key] = 0.0
    return out


# ---------------------------------------------------------------------------
# HID
# ---------------------------------------------------------------------------
def find_hid_paths(vid=VID, pid=PID):
    guid = GUID()
    hid_dll.HidD_GetHidGuid(ctypes.byref(guid))
    info_set = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    )
    if not info_set or info_set == INVALID_HANDLE_VALUE:
        return []
    paths = []
    try:
        for index in range(64):
            iface = SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                info_set, None, ctypes.byref(guid), index, ctypes.byref(iface)
            ):
                break
            required = wintypes.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                info_set, ctypes.byref(iface), None, 0, ctypes.byref(required), None
            )
            if required.value == 0:
                continue
            buf = ctypes.create_string_buffer(required.value)
            detail = ctypes.cast(buf, ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W))
            detail.contents.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W)
            if setupapi.SetupDiGetDeviceInterfaceDetailW(
                info_set, ctypes.byref(iface), buf, required.value,
                ctypes.byref(required), None,
            ):
                path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
                low = path.lower()
                if f"vid_{vid:04x}" in low and f"pid_{pid:04x}" in low:
                    paths.append(path)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info_set)
    return paths


class CoolerHID:
    """Conexao HID no mesmo modelo do driver de referencia (FILE_FLAG_OVERLAPPED)."""

    def __init__(self, path):
        self.path = path
        kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                          wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.GetOverlappedResult.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(OVERLAPPED),
            ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
        ]
        kernel32.GetOverlappedResult.restype = wintypes.BOOL
        kernel32.ResetEvent.argtypes = [wintypes.HANDLE]

        self.handle = kernel32.CreateFileW(
            path, GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING,
            FILE_FLAG_OVERLAPPED, None,
        )
        if self.handle == INVALID_HANDLE_VALUE or not self.handle:
            raise ctypes.WinError(ctypes.get_last_error()) from None
        self._event = kernel32.CreateEventW(None, True, False, None)
        if not self._event:
            err = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(err)
        self.ov = OVERLAPPED()
        self.ov.hEvent = self._event

        self.output_len = 0
        preparsed = ctypes.c_void_p()
        if hid_dll.HidD_GetPreparsedData(self.handle, ctypes.byref(preparsed)):
            try:
                caps = HIDP_CAPS()
                if hid_dll.HidP_GetCaps(preparsed, ctypes.byref(caps)) == 0:
                    self.output_len = caps.OutputReportByteLength
            finally:
                hid_dll.HidD_FreePreparsedData(preparsed)
        self.output_len = 11  # tamanho real do relatorio deste dispositivo

    def send(self, payload, layout="direct"):
        # Protocolo validado em hardware (Rise Mode / Mingyan):
        # [0]=Report ID, [1]=valor no display, [2]=GPU, [3]=CPU%,
        # [4]=RAM%, [5]=board, [6]=GPU%, [7..10]=0
        report = bytearray(11)
        report[0] = 0x00
        if layout == "direct":
            report[1:11] = payload[1:11]
        else:
            report[1:11] = payload[:10]

        # A DOC do Windows pede zerar a estrutura OVERLAPPED entre operacoes.
        ctypes.memset(ctypes.byref(self.ov), 0, ctypes.sizeof(self.ov))
        self.ov.hEvent = self._event
        kernel32.ResetEvent(self.ov.hEvent)
        written = wintypes.DWORD(0)
        ok = kernel32.WriteFile(
            self.handle, bytes(report), len(report),
            ctypes.byref(written), ctypes.byref(self.ov)
        )
        if not ok and ctypes.get_last_error() == ERROR_IO_PENDING:
            ok = kernel32.GetOverlappedResult(
                self.handle, ctypes.byref(self.ov), ctypes.byref(written), True
            )
        if not ok or written.value == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        return bytes(report[:written.value])

    def close(self):
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None
        if getattr(self, "_event", None):
            kernel32.CloseHandle(self._event)
            self._event = None
        if getattr(self, "ov", None):
            self.ov.hEvent = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def open_cooler(path):
    for tentativa in range(5):
        try:
            return CoolerHID(path)
        except OSError as exc:
            # Se estivermos elevados (tarefa), podemos encerrar o programa
            # original que esta segurando o visor em modo exclusivo.
            if getattr(exc, "winerror", 0) == 32 and is_admin():
                print("[HID] Programa original segura o visor — encerrando...")
                for nome in ("SendTemp.exe", "CPU COOLER V4.0.exe"):
                    subprocess.run(["taskkill", "/F", "/IM", nome],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, check=False)
                time.sleep(1.5)
                continue
            print(f"[ERRO] Falha ao abrir o HID: {exc}")
            print("        Feche o 'CPU COOLER V4.0' e o 'SendTemp.exe' originais.")
            raise SystemExit(1) from None
    print("[ERRO] Nao foi possivel liberar o visor apos 5 tentativas.")
    raise SystemExit(1)


def stop_original():
    subprocess.run(["taskkill", "/IM", "SendTemp.exe", "/F"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_NAME = "MonitorCPUCooler"


def startup_command():
    """Comando de inicializacao (usa pythonw.exe para nao abrir console).

    No logon do Windows o monitor vai direto para a bandeja, sem abrir a
    janela na tela: basta clicar no icone para restaurar.
    """
    flags = "--driver --start-minimized"
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {flags}'
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        pw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pw):
            exe = pw
    return f'"{exe}" "{os.path.abspath(__file__)}" {flags}'


def install_startup():
    """Retorna (ok, mensagem)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, STARTUP_NAME, 0, winreg.REG_SZ,
                              startup_command())
        return True, "Registrado para iniciar com o Windows."
    except OSError as exc:
        return False, f"Falha ao registrar inicializacao: {exc}"


def uninstall_startup():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, STARTUP_NAME)
        return True, "Inicializacao automatica removida."
    except FileNotFoundError:
        return True, "Nao estava registrado."
    except OSError as exc:
        return False, f"Falha ao remover inicializacao: {exc}"


def startup_status():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY) as key:
            return winreg.QueryValueEx(key, STARTUP_NAME)[0]
    except OSError:
        return None


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class SHELLEXECUTEINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE), ("lpIDList", wintypes.LPVOID),
        ("lpClass", wintypes.LPCWSTR), ("hKeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def relaunch_as_admin(params_override=None, wait=True):
    """Reinicia elevado (UAC). Se wait=False, apenas lanca e retorna."""
    if params_override is None:
        arg_list = [a for a in sys.argv[1:] if a not in ("--elevate", "--driver")]
        if "--driver" in sys.argv[1:]:
            arg_list.append("--no-fallback")
        params = " ".join('"' + a + '"' for a in arg_list)
    else:
        params = params_override
    sei = SHELLEXECUTEINFO()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = sys.executable
    if getattr(sys, "frozen", False):
        sei.lpParameters = params
    else:
        sei.lpParameters = f'"{os.path.abspath(__file__)}" {params}'
    # HERE aponta para _MEIPASS, que e apagado quando o processo sai.
    sei.lpDirectory = DATA_DIR if getattr(sys, "frozen", False) else HERE
    sei.nShow = 1
    try:
        if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
            return False
        if sei.hProcess:
            if not wait:
                ctypes.windll.kernel32.CloseHandle(sei.hProcess)
                return True
            ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 0xFFFFFFFF)
            code = wintypes.DWORD()
            ctypes.windll.kernel32.GetExitCodeProcess(
                sei.hProcess, ctypes.byref(code)
            )
            ctypes.windll.kernel32.CloseHandle(sei.hProcess)
            return code.value
    except Exception:
        return False
    return True


AUTO_TASK = "MonitorCPUCooler"
AUTO_LOG = os.path.join(DATA_DIR, "auto_start.log")


def auto_start_status():
    """True se a tarefa agendada elevada estiver registrada.

    O marcador local evita pedir UAC de novo se o schtasks falhar
    momentaneamente (por exemplo, com a tarefa em execucao).
    """
    try:
        if os.path.exists(os.path.join(DATA_DIR, "task_configured")):
            return True
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["schtasks", "/Query", "/TN", AUTO_TASK],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=10,
        )
        if out.returncode == 0:
            try:
                with open(os.path.join(DATA_DIR, "task_configured"), "w",
                          encoding="utf-8") as fh:
                    fh.write("ok\n")
            except Exception:
                pass
            return True
    except Exception:
        pass
    return False


def _run_script_elevated(script, pump=None):
    """Executa um .ps1 elevado uma vez. Depois disso, sem novos UAC."""
    try:
        sei = SHELLEXECUTEINFO()
        sei.cbSize = ctypes.sizeof(sei)
        sei.fMask = 0x00000040
        sei.lpVerb = "open" if is_admin() else "runas"
        sei.lpFile = powershell32()
        sei.lpParameters = f'-NoProfile -ExecutionPolicy Bypass -File "{script}"'
        sei.lpDirectory = DATA_DIR
        sei.nShow = 0
        if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
            return False
        if sei.hProcess:
            # Espera em fatias para que a GUI continue animando o spinner.
            for _ in range(300):
                if ctypes.windll.kernel32.WaitForSingleObject(
                        sei.hProcess, 100) != 0x102:  # WAIT_TIMEOUT
                    break
                if pump:
                    pump()
            code = wintypes.DWORD()
            ctypes.windll.kernel32.GetExitCodeProcess(
                sei.hProcess, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(sei.hProcess)
            return code.value == 0
    except Exception:
        return False
    return True


def _auto_result():
    try:
        with open(AUTO_LOG, encoding="utf-8", errors="replace") as fh:
            text = fh.read().strip()
        return text.splitlines()[-1] if text else "concluido"
    except Exception:
        return "concluido"


def _helper_script(name):
    """Arquivo auxiliar: pasta do exe, ou a extracao do PyInstaller."""
    candidate = os.path.join(DATA_DIR, name)
    if os.path.exists(candidate):
        return candidate
    return os.path.join(HERE, name)


def install_auto_start(pump=None):
    script = _helper_script("install_task.ps1")
    if not os.path.exists(script):
        return False, "Script de instalacao ausente."
    ok = _run_script_elevated(script, pump=pump)
    if ok:
        try:
            with open(os.path.join(DATA_DIR, "task_configured"), "w",
                      encoding="utf-8") as fh:
                fh.write("ok\n")
        except Exception:
            pass
    return ok, ("Inicializacao automatica ativa (sem UAC no logon)."
                if ok else "Falha ao criar tarefa: " + _auto_result())


def uninstall_auto_start(pump=None):
    script = _helper_script("uninstall_task.ps1")
    if not os.path.exists(script):
        return False, "Script de remocao ausente."
    ok = _run_script_elevated(script, pump=pump)
    if ok:
        try:
            os.remove(os.path.join(DATA_DIR, "task_configured"))
        except Exception:
            pass
    return ok, ("Inicializacao automatica removida."
                if ok else "Falha ao remover tarefa: " + _auto_result())


def clamp_byte(value):
    return max(0, min(255, int(value)))


def build_payload(v):
    p = bytearray(PAYLOAD_LEN)
    p[0] = 0
    p[1] = clamp_byte(v["cpu_temp"])
    p[2] = clamp_byte(v["gpu_temp"])
    p[3] = clamp_byte(v["cpu_load"])
    p[4] = clamp_byte(v["mem_load"])
    p[5] = clamp_byte(v["board_temp"])
    p[6] = clamp_byte(v["gpu_load"])
    p[7] = clamp_byte(int(v["sys_rpm"]) >> 8)
    p[8] = clamp_byte(int(v["sys_rpm"]))
    p[9] = clamp_byte(int(v["cpu_rpm"]) >> 8)
    p[10] = clamp_byte(int(v["cpu_rpm"]))
    return bytes(p)


# ---------------------------------------------------------------------------
# Janela do leitor (abre imediatamente ao iniciar o programa)
# ---------------------------------------------------------------------------
class Spinner(tk.Canvas):
    """Arco giratorio para indicar que algo esta sendo carregado/configurado."""

    def __init__(self, parent, size=20, color="#00d4ff"):
        super().__init__(parent, width=size, height=size,
                         highlightthickness=0, bg=parent.cget("bg"))
        self.arc = self.create_arc(2, 2, size - 2, size - 2, start=0,
                                   extent=300, style="arc", width=3,
                                   outline=color)
        self._after = None
        self._running = False

    def start(self):
        if self._running:
            return
        self._running = True
        self._tick(0)

    def _tick(self, angle):
        if not self._running:
            return
        try:
            self.itemconfig(self.arc, start=angle % 360)
            self._after = self.after(60, lambda: self._tick(angle + 30))
        except tk.TclError:
            self._running = False

    def stop(self):
        self._running = False
        if self._after:
            try:
                self.after_cancel(self._after)
            except Exception:
                pass
            self._after = None


class ToolTip:
    """Dica explicativa ao passar o mouse sobre um widget."""

    def __init__(self, widget, text, delay=350):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tip = None
        self.after_id = None
        widget.bind("<Enter>", self._schedule)
        widget.bind("<Leave>", self._hide)

    def _schedule(self, _event=None):
        self.after_id = self.widget.after(self.delay, self._show)

    def _show(self):
        if self.tip:
            return
        text = self.text() if callable(self.text) else self.text
        try:
            x = self.widget.winfo_rootx() + 15
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry(f"+{x}+{y}")
            tk.Label(
                self.tip, text=text, justify="left", wraplength=340,
                bg="#fffbe6", fg="#20242a", padx=9, pady=7,
                font=("Segoe UI", 9), relief="solid", bd=1,
            ).pack()
        except Exception:
            self.tip = None

    def _hide(self, _event=None):
        if self.after_id:
            try:
                self.widget.after_cancel(self.after_id)
            except Exception:
                pass
            self.after_id = None
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


def to_display(celsius, unit):
    """Converte apenas para exibicao; o protocolo HID continua em Celsius."""
    c = float(celsius or 0)
    if unit == "F":
        return int(round(c * 9.0 / 5.0 + 32))
    return int(round(c))


class MonitorGUI:
    """Painel do monitor: mostrador circular, metricas e botoes em grade."""

    THEMES = {
        "dark": {
            "BG": "#0f1419", "CARD": "#1b222b", "ACCENT": "#00d4ff",
            "TEXT": "#e6edf3", "MUTED": "#8b98a5", "GOOD": "#34d399",
            "BAD": "#f87171", "BTN": "#26313d", "BTN_ACTIVE": "#354454",
            "DANGER": "#3a2226", "GAUGE_BG": "#2a3441", "FOOTER": "#4d5b69",
            "LOG_BG": "#0b0f13", "LOG_FG": "#c9e8d0", "PRIMARY_FG": "#08121a",
        },
        "light": {
            "BG": "#eef2f7", "CARD": "#ffffff", "ACCENT": "#007fa8",
            "TEXT": "#16212e", "MUTED": "#5c6b7a", "GOOD": "#0f9d6e",
            "BAD": "#d64b4b", "BTN": "#dde6f0", "BTN_ACTIVE": "#ccd9e8",
            "DANGER": "#f6dede", "GAUGE_BG": "#dde6f0", "FOOTER": "#8b9aab",
            "LOG_BG": "#ffffff", "LOG_FG": "#1c2a38", "PRIMARY_FG": "#ffffff",
        },
    }

    def __init__(self, unit="C", start_minimized=False):
        from tkinter import messagebox
        self.messagebox = messagebox
        self._start_minimized = bool(start_minimized)
        self.theme = "dark"
        for key, value in self.THEMES[self.theme].items():
            setattr(self, key, value)
        self.unit = unit
        self.last_values = None
        self.last_blocked = False
        self.tray = None
        self._tray_hidden = False
        self._permission_pending = False
        self.ui_queue = queue.Queue()
        self.root = tk.Tk()
        self.root.title("CPU Cooler — Monitor")
        self.root.geometry("430x742+80+60")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=self.BG)
        self.running = True
        self._maximized = False
        self._normal_geometry = "430x742+80+60"
        # Barra de titulo propria (botoes estilizados)
        self.root.overrideredirect(True)
        self.root.protocol("WM_DELETE_WINDOW", self.minimize_to_tray)
        self._set_icon(self.root)
        self._build()
        self._fix_taskbar()
        if self._start_minimized:
            # Inicializacao pelo Windows (logon/reinicio): comeca na bandeja.
            self.minimize_to_tray()
        elif not os.path.exists(os.path.join(DATA_DIR, "first_run.done")):
            self.root.after(700, self._show_first_run)

    def _fix_taskbar(self):
        """Janela sem borda nativa precisa ser registrada como app na taskbar."""
        try:
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(int(self.root.frame()))
            GWL_EXSTYLE = -20
            WS_EX_APPWINDOW = 0x00040000
            WS_EX_TOOLWINDOW = 0x00000080
            u32 = ctypes.windll.user32
            style = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                               (style | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW)
            self.root.withdraw()
            self.root.deiconify()
        except Exception:
            pass

    # --- construcao da interface ------------------------------------------
    def _card(self, parent, **kwargs):
        return tk.Frame(parent, bg=self.CARD, highlightthickness=0, **kwargs)

    def _button(self, parent, text, command, primary=False, danger=False):
        bg = self.ACCENT if primary else (self.DANGER if danger else self.BTN)
        fg = self.PRIMARY_FG if primary else self.TEXT
        active = self.ACCENT if primary else self.BTN_ACTIVE
        btn = tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                        activebackground=active,
                        activeforeground=fg, relief="flat", bd=0,
                        font=("Segoe UI", 9, "bold" if primary else "normal"),
                        cursor="hand2", pady=7)
        return btn

    # --- barra de titulo personalizada ------------------------------------
    def _build_titlebar(self):
        bar = tk.Frame(self.root, bg="#0b1016", height=32)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        tk.Label(bar, text="●", fg=self.ACCENT, bg="#0b1016",
                 font=("Segoe UI", 10)).pack(side="left", padx=(10, 4))
        tk.Label(bar, text="CPU COOLER  ·  MONITOR", fg=self.TEXT,
                 bg="#0b1016", font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Label(bar, text="  by casmtts", fg=self.MUTED,
                 bg="#0b1016", font=("Segoe UI", 8)).pack(side="left")
        self.title_bar = bar

        def btn(text, command, hover, color=self.TEXT):
            b = tk.Label(bar, text=text, fg=color, bg="#0b1016",
                         font=("Segoe UI", 12), width=3, cursor="hand2")
            b.pack(side="right", fill="y")
            b.bind("<Button-1>", lambda _e, c=command: c())
            b.bind("<Enter>", lambda _e, w=b, h=hover: w.configure(bg=h))
            b.bind("<Leave>", lambda _e, w=b: w.configure(bg="#0b1016"))
            return b

        btn("✕", self.minimize_to_tray, "#a33b3b")
        self.max_btn = btn("□", self._toggle_maximize, "#25384a")
        btn("─", self._minimize_window, "#25384a")

        bar.bind("<ButtonPress-1>", self._start_drag)
        bar.bind("<B1-Motion>", self._do_drag)

        # Area rolavel para o conteudo (botoes inclusive)
        self.canvas_area = tk.Canvas(self.root, bg=self.BG, highlightthickness=0)
        self.scrollbar = tk.Scrollbar(
            self.root, orient="vertical", command=self.canvas_area.yview,
            bg=self.BTN, troughcolor=self.BG, activebackground=self.ACCENT)
        self.canvas_area.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas_area.pack(side="left", fill="both", expand=True)
        self.content = tk.Frame(self.canvas_area, bg=self.BG)
        self._win_id = self.canvas_area.create_window(
            (0, 0), window=self.content, anchor="nw")
        self.content.bind(
            "<Configure>",
            lambda _e: self.canvas_area.configure(
                scrollregion=self.canvas_area.bbox("all")))
        self.canvas_area.bind(
            "<Configure>",
            lambda e: self.canvas_area.itemconfig(self._win_id, width=e.width))
        self.canvas_area.bind("<MouseWheel>", self._on_wheel)
        self.content.bind("<MouseWheel>", self._on_wheel)

    def _on_wheel(self, event):
        # Dentro da janela de log, a roda deve rolar o texto, nao o painel.
        if isinstance(getattr(event, "widget", None), tk.Text):
            return
        try:
            self.canvas_area.yview_scroll(-int(event.delta / 120), "units")
        except Exception:
            pass

    def _minimize_window(self):
        self.root.iconify()

    def _toggle_maximize(self):
        if self._maximized:
            self.root.geometry(self._normal_geometry)
            self._maximized = False
            self.max_btn.configure(text="□")
        else:
            self._normal_geometry = self.root.geometry()
            self.root.geometry(
                f"{self.root.winfo_screenwidth()}x{self.root.winfo_screenheight() - 40}+0+0")
            self._maximized = True
            self.max_btn.configure(text="❐")

    def _start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _do_drag(self, event):
        if self._maximized:
            return
        x = self.root.winfo_pointerx() - self._drag_x
        y = self.root.winfo_pointery() - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _build(self):
        self._build_titlebar()

        self.cpu_var = tk.StringVar(value="...")
        self.gpu_var = tk.StringVar(value="-- °C")
        self.load_var = tk.StringVar(value="-- %")
        self.status_var = tk.StringVar(
            value="Carregando sensores... (o LibreHardwareMonitor leva "
                  "alguns segundos na primeira leitura)")
        self.startup_var = tk.StringVar(value=self._startup_text())
        self.unit_var = tk.StringVar(value="°C" if self.unit == "C" else "°F")
        self.theme_var = tk.StringVar(value=self._theme_text())

        # Cabeçalho
        header = tk.Frame(self.content, bg=self.BG)
        header.pack(fill="x", padx=18, pady=(14, 8))
        tk.Label(header, text="CPU COOLER", fg=self.TEXT, bg=self.BG,
                 font=("Segoe UI", 17, "bold")).pack(side="left")
        tk.Label(header, text="  MONITOR", fg=self.ACCENT, bg=self.BG,
                 font=("Segoe UI", 9, "bold")).pack(side="left", pady=(6, 0))

        self.driver_var = tk.StringVar(value="WINRING0: --")
        self.driver_label = tk.Label(
            header, textvariable=self.driver_var, fg=self.MUTED, bg=self.BG,
            font=("Segoe UI", 8, "bold"))
        self.driver_label.pack(side="right", padx=(0, 6), pady=(6, 0))

        self.dot = tk.Canvas(header, width=12, height=12, bg=self.BG,
                             highlightthickness=0)
        self.dot.pack(side="right", pady=(4, 0))
        self.dot_id = self.dot.create_oval(1, 1, 11, 11, fill=self.MUTED)

        # Mostrador circular
        gauge = self._card(self.content)
        gauge.pack(fill="x", padx=18, pady=(0, 10))
        gauge.configure(height=232)
        gauge.pack_propagate(False)
        tk.Label(gauge, text="TEMPERATURA DA CPU", fg=self.MUTED, bg=self.CARD,
                 font=("Segoe UI", 9, "bold")).pack(pady=(10, 0))
        self.canvas = tk.Canvas(gauge, width=200, height=200, bg=self.CARD,
                                highlightthickness=0)
        self.canvas.pack(pady=(2, 0))
        self.canvas.create_arc(12, 12, 188, 188, start=90, extent=-360,
                               style="arc", width=13, outline=self.GAUGE_BG)
        self.ring = self.canvas.create_arc(12, 12, 188, 188, start=90,
                                           extent=0, style="arc", width=13,
                                           outline=self.ACCENT)
        self.txt_temp = self.canvas.create_text(100, 92, text="--",
                                                font=("Segoe UI", 42, "bold"),
                                                fill=self.TEXT)
        self.txt_unit = self.canvas.create_text(100, 134, text="°C",
                                                font=("Segoe UI", 14, "bold"),
                                                fill=self.MUTED)

        # Cartões de métricas (temperatura e uso de GPU/CPU/RAM/placa/fans)
        self.ram_var = tk.StringVar(value="-- %")
        self.ram_temp_var = tk.StringVar(value="N/D")
        self.gpu_pct_var = tk.StringVar(value="-- %")
        self.gpu_temp_var = self.gpu_var
        self.board_temp_var = tk.StringVar(value="N/D")
        self.fan_var = tk.StringVar(value="N/D")
        self.sysfan_var = tk.StringVar(value="N/D")
        self.pump_var = tk.StringVar(value="N/D")
        self.fan3_var = tk.StringVar(value="N/D")

        stats = tk.Frame(self.content, bg=self.BG)
        stats.pack(fill="x", padx=14, pady=(0, 10))
        self.stats_frame = stats
        if not hasattr(self, "details_visible"):
            self.details_visible = True
        if not self.details_visible:
            stats.pack_forget()

        # Colunas por componente: cada item fica agrupado com o seu.
        secoes = [
            ("CPU", [("USO CPU", self.load_var, self.ACCENT),
                     ("PUMP", self.pump_var, self.ACCENT),
                     ("SYSFAN 1", self.fan_var, self.ACCENT),
                     ("SYSFAN 2", self.sysfan_var, self.MUTED),
                     ("SYSFAN 3", self.fan3_var, self.MUTED)]),
            ("GPU", [("USO GPU", self.gpu_pct_var, self.ACCENT),
                     ("TEMP GPU", self.gpu_temp_var, self.MUTED)]),
            ("MEMÓRIA", [("USO RAM", self.ram_var, self.MUTED)]),
            ("PLACA MÃE", [("TEMP MB", self.board_temp_var, self.MUTED)]),
        ]
        for col, (titulo, itens) in enumerate(secoes):
            coluna = tk.Frame(stats, bg=self.BG)
            coluna.grid(row=0, column=col, sticky="n",
                        padx=(0 if col == 0 else 5))
            stats.columnconfigure(col, weight=1)
            tk.Label(coluna, text=titulo, fg=self.ACCENT, bg=self.BG,
                     font=("Segoe UI", 8, "bold")).pack(pady=(0, 5))
            for title, var, color in itens:
                card = self._card(coluna)
                card.pack(fill="x", pady=(0, 5))
                tk.Label(card, text=title, fg=self.MUTED, bg=self.CARD,
                         font=("Segoe UI", 7, "bold")).pack(pady=(6, 0))
                tk.Label(card, textvariable=var, fg=color, bg=self.CARD,
                         font=("Segoe UI", 10, "bold")).pack(pady=(1, 6))

        # Status
        status_card = self._card(self.content)
        status_card.pack(fill="x", padx=18, pady=(0, 10))
        self.status_card = status_card
        status_row = tk.Frame(status_card, bg=self.CARD)
        status_row.pack(fill="x", padx=10, pady=8)
        self.spinner = Spinner(status_row, color=self.ACCENT)
        self.spinner.pack(side="left", padx=(0, 6))
        tk.Label(status_row, textvariable=self.status_var, fg=self.MUTED,
                 bg=self.CARD, font=("Segoe UI", 9), wraplength=350,
                 justify="left").pack(side="left", fill="x")

        # Anuncio visivel sobre o driver + dica ao passar o mouse
        explain = (
            "WinRing0 é um driver de LEITURA de hardware (código aberto, usado "
            "pelo LibreHardwareMonitor). Ele permite que o programa leia a "
            "temperatura real da CPU e os sensores da placa-mãe.\n\n"
            "Não é vírus nem malware: não coleta dados, não envia nada para a "
            "internet e não altera o sistema. Apenas faz leitura de hardware.\n\n"
            "Para funcionar, precisa de permissão de Administrador uma vez "
            "(via tarefa agendada, sem pedir toda vez)."
        )
        self.notice_var = tk.StringVar(
            value="ℹ Ative o WinRing0 para ler a CPU real  (passe o mouse aqui)")
        self.notice_label = tk.Label(
            self.content, textvariable=self.notice_var, fg=self.ACCENT,
            bg=self.BG, font=("Segoe UI", 9, "bold"), wraplength=390,
            justify="left", cursor="hand2")
        self.notice_label.pack(fill="x", padx=20, pady=(0, 6))
        ToolTip(self.notice_label, explain)

        # Botoes
        btns = tk.Frame(self.content, bg=self.BG)
        btns.pack(fill="x", padx=18, pady=(0, 8))
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)

        self._button(btns, "Mudar °C / °F", self.toggle_unit,
                     primary=True).grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=3)
        self._button(btns, "Iniciar com Windows: " +
                     ("SIM" if auto_start_status() else "NÃO"),
                     self.toggle_startup).grid(row=0, column=1, sticky="ew",
                                               padx=(4, 0), pady=3)
        # atualiza o texto do botao de startup via variavel
        self.startup_btn = btns.grid_slaves(row=0, column=1)[0]
        self.startup_btn.configure(textvariable=self.startup_var)

        self.driver_btn = self._button(
            btns, "Ativar driver WinRing0 (CPU real)", self.enable_driver)
        self.driver_btn.grid(row=1, column=0, columnspan=2,
                             sticky="ew", pady=3)
        ToolTip(self.driver_btn, explain)
        self._button(btns, "Ver log / terminal", self.open_log,
                     primary=True).grid(row=2, column=0, sticky="ew",
                                        padx=(0, 4), pady=3)
        self._button(btns, "Sair", self.exit_app,
                     danger=True).grid(row=2, column=1, sticky="ew",
                                       padx=(4, 0), pady=3)
        self._button(btns, "", self.toggle_theme).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=3)
        btns.grid_slaves(row=3, column=0)[0].configure(
            textvariable=self.theme_var)
        self.details_var = tk.StringVar(value=self._details_text())
        self._button(btns, "", self.toggle_details).grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=3)
        btns.grid_slaves(row=4, column=0)[0].configure(
            textvariable=self.details_var)

        tk.Label(self.content,
                 text="by casmtts  ·  VID 0xAA88  ·  PID 0x8666  ·  HID",
                 fg=self.FOOTER, bg=self.BG,
                 font=("Segoe UI", 8)).pack(pady=(2, 8))
        self.root.update()
        # Roda do mouse em qualquer lugar da janela (bindings de filho nao
        # sobem para o Canvas, por isso e global).
        self.root.bind_all("<MouseWheel>", self._on_wheel)
        self.spinner.start()

    # --- acoes ------------------------------------------------------------
    def _set_icon(self, window):
        icon_path = os.path.join(DATA_DIR, "cooler_icon.ico")
        if not os.path.exists(icon_path):
            return
        try:
            window.iconbitmap(icon_path)
        except Exception:
            pass
        # Define explicitamente os icones pequeno (bandeja/taskbar) e grande.
        try:
            user32 = ctypes.windll.user32
            user32.LoadImageW.argtypes = [
                wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                ctypes.c_int, ctypes.c_int, wintypes.UINT]
            user32.LoadImageW.restype = wintypes.HANDLE
            user32.SendMessageW.argtypes = [
                wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.SendMessageW.restype = wintypes.LPARAM
            window.update_idletasks()
            hwnd = wintypes.HWND(int(window.winfo_id()))
            for size, which in ((16, 0), (32, 1)):  # ICON_SMALL, ICON_BIG
                hicon = user32.LoadImageW(
                    None, icon_path, 1, size, size, 0x00000010)  # LR_LOADFROMFILE
                if hicon:
                    user32.SendMessageW(hwnd, 0x0080, which, int(hicon))  # WM_SETICON
        except Exception:
            pass

    @staticmethod
    def _startup_text():
        return ("Iniciar com Windows: SIM (sem UAC)" if auto_start_status()
                else "Iniciar com Windows: NÃO")

    def toggle_startup(self):
        if auto_start_status():
            # Ja esta ativo: nao desfaz (evita um novo UAC sem querer).
            self.messagebox.showinfo(
                "Iniciar com Windows",
                "Ja esta ativo com privilegios maximos.\n\n"
                "O Windows inicia o monitor no logon SEM pedir permissao.\n"
                "Para remover, use a opcao --uninstall-startup.")
            self.startup_var.set(self._startup_text())
            return
        self.spinner.start()
        try:
            ok, msg = install_auto_start(pump=self.pump)
        finally:
            self.spinner.stop()
        self.startup_var.set(self._startup_text())
        if ok:
            self.messagebox.showinfo(
                "Iniciar com Windows",
                "Pronto! Tarefa elevada criada.\n\n"
                "O UAC apareceu apenas uma vez (agora). A partir do proximo "
                "logon o monitor abre automaticamente SEM pedir permissao.")
        else:
            self.messagebox.showerror("Inicializacao automatica", msg)

    def enable_driver(self, ask=True):
        if is_admin():
            self.messagebox.showinfo("Driver", "O monitor ja esta rodando como administrador.")
            return
        if auto_start_status():
            # A tarefa agendada ja tem privilegios maximos: nao pede UAC.
            self.messagebox.showinfo(
                "Permissão de administrador",
                "A permissão de administrador JÁ está configurada neste PC.\n\n"
                "Ela foi criada uma única vez pela tarefa agendada. Por isso o "
                "monitor vai reiniciar elevado pelo Agendador de Tarefas, "
                "sem mostrar o UAC novamente.\n\n"
                "Isso é normal — não é um erro.")
            try:
                release_single_instance()
                subprocess.run(["schtasks", "/Run", "/TN", AUTO_TASK],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10)
                self.running = False
                self.exit_app()
                return
            except Exception as exc:
                self.messagebox.showerror(
                    "Erro", f"Nao foi possivel iniciar pela tarefa: {exc}\n"
                            "Nao sera pedido UAC novamente.")
                return
        if ask and not self.messagebox.askyesno(
                "Driver WinRing0",
                "O monitor vai reiniciar como administrador para ler a temperatura "
                "real da CPU.\n\nSe a Integridade da memoria (HVCI) estiver ligada, "
                "desative-a e reinicie antes.\n\nContinuar?"):
            return
        params = f'"{app_target()}" --driver' if not getattr(sys, "frozen", False) else "--driver"
        result = relaunch_as_admin(params_override=params, wait=False)
        if result is False:
            self.messagebox.showerror("Erro", "Nao foi possivel elevar (UAC negado).")
            return
        self.running = False
        self.exit_app()

    def pump(self):
        """Processa eventos da janela sem precisar de um novo sensor."""
        try:
            self.root.update()
        except tk.TclError:
            self.running = False

    def toggle_unit(self):
        self.unit = "F" if self.unit == "C" else "C"
        self.unit_var.set("°F" if self.unit == "F" else "°C")
        if self.last_values is not None:
            self.update(self.last_values, self.last_blocked)

    def _details_text(self):
        return ("Ocultar detalhes" if self.details_visible
                else "Mostrar detalhes")

    def toggle_details(self):
        self.details_visible = not self.details_visible
        if self.details_visible:
            self.stats_frame.pack(fill="x", padx=18, pady=(0, 10),
                                  before=self.status_card)
        else:
            self.stats_frame.pack_forget()
        self.details_var.set(self._details_text())
        self.root.update()

    def _theme_text(self):
        # Mostra o tema para o qual vai mudar ao clicar.
        return "Tema: escuro  ☾" if self.theme == "light" else "Tema: claro  ☀"

    def toggle_theme(self):
        if getattr(self, "spinner", None):
            self.spinner.stop()
        self.theme = "light" if self.theme == "dark" else "dark"
        for key, value in self.THEMES[self.theme].items():
            setattr(self, key, value)
        self.root.configure(bg=self.BG)
        for widget in list(self.root.winfo_children()):
            if isinstance(widget, tk.Toplevel):
                continue
            widget.destroy()
        self._build()
        if self.last_values is not None:
            self.update(self.last_values, self.last_blocked)

    def _finish_first_run(self, dialog):
        try:
            with open(os.path.join(DATA_DIR, "first_run.done"), "w",
                      encoding="utf-8") as fh:
                fh.write("configurado\n")
        except Exception:
            pass
        try:
            dialog.destroy()
        except Exception:
            pass

    def _show_first_run(self):
        """Tela da primeira execucao: requisitos e instalacao automatica."""
        top = tk.Toplevel(self.root)
        top.title("Configuração inicial — Monitor CPU Cooler")
        top.geometry("500x420+150+120")
        top.resizable(False, False)
        top.attributes("-topmost", True)
        top.configure(bg=self.BG)
        self._set_icon(top)

        txt = (
            "Bem-vindo ao Monitor CPU Cooler — by casmtts\n\n"
            "REQUISITOS / O QUE SERÁ CONFIGURADO:\n\n"
            "  • Windows 10 ou 11;\n"
            "  • Display do cooler USB (VID 0xAA88 / PID 0x8666);\n"
            "  • Driver de leitura da CPU: WinRing0 ou PawnIO;\n"
            "  • Permissão de Administrador — solicitada UMA única vez;\n"
            "  • Tarefa agendada para iniciar elevado no logon, sem UAC;\n"
            "  • Todas as DLLs, scripts e ícones já vêm dentro do .exe —\n"
            "    não é preciso instalar Python nem baixar nada.\n\n"
            "Clique em INSTALAR para configurar a inicialização automática\n"
            "e liberar a leitura da temperatura. Se preferir, pule esta etapa."
        )
        tk.Label(top, text=txt, fg=self.TEXT, bg=self.BG, justify="left",
                 font=("Segoe UI", 10), wraplength=460).pack(padx=16,
                                                              pady=(14, 10))

        def do_install():
            top.destroy()
            self.spinner.start()
            try:
                ok, msg = install_auto_start(pump=self.pump)
            finally:
                self.spinner.stop()
            self.startup_var.set(self._startup_text())
            self._finish_first_run(top)
            self.messagebox.showinfo(
                "Configuração inicial",
                ("Concluído!\n\n" if ok else "Atenção:\n\n") + msg +
                ("\n\nSe o driver estiver bloqueado, use o botão "
                 "Ativar driver WinRing0 ou instale o PawnIO."))

        self._button(top, "Instalar / configurar agora", do_install,
                     primary=True).pack(pady=(0, 6))
        self._button(top, "Pular (configurar depois)",
                     lambda: self._finish_first_run(top)).pack(pady=(0, 10))
        top.transient(self.root)

    def show_permission_dialog(self):
        """Aviso explicativo (nao modal: nao congela a janela principal)."""
        if getattr(self, "_tray_hidden", False):
            # Com a janela na bandeja, um Toplevel topmost apareceria na tela
            # e pareceria que o aplicativo "abriu" no logon. Guarda o aviso
            # para exibi-lo somente quando o usuario restaurar a janela.
            self._permission_pending = True
            return
        if getattr(self, "perm_dialog", None) and self.perm_dialog.winfo_exists():
            self.perm_dialog.lift()
            return
        top = tk.Toplevel(self.root)
        self.perm_dialog = top
        top.title("Permissão necessária para ler a CPU")
        top.geometry("470x300+140+140")
        top.resizable(False, False)
        top.attributes("-topmost", True)
        top.configure(bg=self.BG)
        self._set_icon(top)

        txt = (
            "A temperatura da CPU está bloqueada — por isso o visor ficou em 0.\n\n"
            "Motivo: o LibreHardwareMonitor precisa do driver de kernel WinRing0 "
            "para ler o sensor da CPU. O Windows bloqueia esse driver quando:\n\n"
            "  1) o programa não está como Administrador; e/ou\n"
            "  2) a 'Integridade da memória' (HVCI) está ligada.\n\n"
            "Como resolver:\n"
            "  • Execute 'desativar_hvci.bat' como Admin e reinicie o PC;\n"
            "  • Depois clique no botão abaixo e aceite o UAC.\n\n"
            "Até lá, nenhum valor 0 será enviado ao display do cooler."
        )
        tk.Label(top, text=txt, fg=self.TEXT, bg=self.BG, justify="left",
                 font=("Segoe UI", 10), wraplength=440).pack(padx=14, pady=(14, 10))

        self._button(top, "Ativar driver agora (pedir UAC)",
                     lambda: (top.destroy(), self.enable_driver(ask=False)),
                     primary=True).pack(pady=(0, 6))
        self._button(top, "Já desativei o HVCI — tentar de novo",
                     lambda: (top.destroy(), self.enable_driver(ask=False))).pack(pady=(0, 6))
        self._button(top, "Continuar sem leitura", top.destroy).pack(pady=(0, 4))
        self._button(top, "Sair do monitor",
                     lambda: (top.destroy(), self.exit_app()), danger=True).pack(pady=(0, 10))

        top.transient(self.root)
        # Sem grab_set/wait_window: o loop principal continua respondendo.

    def open_log(self):
        """Abre uma janela com tudo que o programa esta executando."""
        top = tk.Toplevel(self.root)
        top.title("Log / terminal — Monitor CPU Cooler")
        top.geometry("760x430+120+120")
        top.configure(bg=self.BG)
        self._set_icon(top)

        frame = tk.Frame(top, bg=self.BG)
        frame.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        text = tk.Text(frame, bg=self.LOG_BG, fg=self.LOG_FG,
                       font=("Consolas", 9), wrap="none", bd=0)
        scroll = tk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        state = {"count": -1}

        def refresh():
            if not top.winfo_exists():
                return
            lines = list(LOG_LINES)
            if len(lines) != state["count"]:
                text.delete("1.0", "end")
                text.insert("end", "\n".join(lines) + "\n")
                text.see("end")
                state["count"] = len(lines)
            top.after(400, refresh)

        def clear():
            LOG_LINES.clear()
            state["count"] = -1
            text.delete("1.0", "end")

        refresh()
        self._button(top, "Limpar log", clear).pack(pady=(0, 8))

    # --- bandeja / fechar -------------------------------------------------
    def minimize_to_tray(self):
        self.root.withdraw()
        self._tray_hidden = True
        if self.tray is not None:
            return
        try:
            import threading
            import pystray
            from PIL import Image

            image = Image.open(os.path.join(DATA_DIR, "cooler_icon.png"))
            menu = pystray.Menu(
                pystray.MenuItem("Abrir monitor", self._tray_open, default=True),
                pystray.MenuItem("Sair", self._tray_quit),
            )
            self.tray = pystray.Icon("MonitorCPUCooler", image,
                                     "Monitor CPU Cooler", menu)
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception as exc:
            print(f"[AVISO] Bandeja indisponivel: {exc}")
            self._tray_hidden = False
            self.root.deiconify()
            self.messagebox.showinfo(
                "Bandeja", "Nao foi possivel criar o icone da bandeja. "
                "A janela permanece aberta; use o botao Sair para encerrar.")

    def _tray_open(self, icon=None, item=None):
        self.ui_queue.put("open")

    def _tray_quit(self, icon=None, item=None):
        self.ui_queue.put("quit")

    def process_queue(self):
        try:
            while True:
                action = self.ui_queue.get_nowait()
                if action == "open":
                    self._tray_hidden = False
                    self.root.deiconify()
                    # Quando comecou minimizado, a configuracao inicial so
                    # aparece quando o usuario abrir o monitor pela bandeja.
                    if (self._start_minimized and not os.path.exists(
                            os.path.join(DATA_DIR, "first_run.done"))):
                        self.root.after(100, self._show_first_run)
                        self._permission_pending = False
                    elif self._permission_pending:
                        self._permission_pending = False
                        self.root.after(100, self.show_permission_dialog)
                elif action == "quit":
                    self.exit_app()
        except queue.Empty:
            pass

    def update(self, values, blocked=False):
        self.last_values = values
        self.last_blocked = blocked
        if getattr(self, "spinner", None) and self.spinner._running:
            self.spinner.stop()
        temp = to_display(values["cpu_temp"], self.unit)
        self.cpu_var.set(str(temp))
        self.canvas.itemconfig(self.txt_temp, text=str(temp))
        self.canvas.itemconfig(self.txt_unit,
                               text="°F" if self.unit == "F" else "°C")
        drv = "PawnIO" if "Pawn" in getattr(self, "driver_mode", "") else "WinRing0"
        driver_on = is_admin() and (winring0_present()
                                    or values["cpu_temp"] > 0)
        # Azul = enviando normal; verde = driver ativo; vermelho = bloqueado.
        state_color = self.BAD if blocked else (self.GOOD if driver_on
                                                else self.ACCENT)
        driver_color = state_color

        pct = max(0.0, min(100.0, float(values["cpu_temp"])))
        self.canvas.itemconfig(self.ring, extent=-3.6 * pct,
                               outline=state_color)
        self.gpu_var.set(f"{to_display(values['gpu_temp'], self.unit)}"
                         f"{'°F' if self.unit == 'F' else '°C'}")
        self.load_var.set(f"{int(values['cpu_load'])} %")
        self.ram_var.set(f"{int(values['mem_load'])} %")
        if values["ram_temp"] > 0:
            self.ram_temp_var.set(
                f"{to_display(values['ram_temp'], self.unit)}"
                f"{'°F' if self.unit == 'F' else '°C'}")
        else:
            self.ram_temp_var.set("N/D")
        self.gpu_pct_var.set(f"{int(values['gpu_load'])} %")
        self.board_temp_var.set(
            f"{to_display(values['board_temp'], self.unit)}"
            f"{'°F' if self.unit == 'F' else '°C'}"
            if values["board_temp"] > 0 else "N/D")
        self.fan_var.set(
            f"{int(values['cpu_rpm'])} RPM" if values["cpu_rpm"] > 0 else "N/D")
        self.sysfan_var.set(
            f"{int(values['sys_rpm'])} RPM" if values["sys_rpm"] > 0 else "N/D")
        self.pump_var.set(
            f"{int(values['pump_rpm'])} RPM" if values["pump_rpm"] > 0 else "N/D")
        self.fan3_var.set(
            f"{int(values['fan3_rpm'])} RPM" if values["fan3_rpm"] > 0 else "N/D")

        self.driver_var.set(
            f"{drv.upper()}: ATIVO ✓" if driver_on else
            (f"{drv.upper()}: BLOQUEADO" if blocked else f"{drv.upper()}: INATIVO"))
        self.driver_label.configure(fg=driver_color)
        self.notice_var.set(
            f"✓ {drv} ativo — lendo a temperatura real da CPU" if driver_on
            else (f"⚠ {drv} bloqueado — clique para ativar "
                  "(passe o mouse para saber o que é)" if blocked
                  else f"ℹ Ative o {drv} para ler a CPU real  "
                       "(passe o mouse aqui)"))
        self.notice_label.configure(fg=state_color)
        self.driver_btn.configure(
            text=(f"{drv} ATIVO ✓" if driver_on else
                  f"Ativar driver {drv} (CPU real)"),
            bg=driver_color if driver_on else self.BTN,
            fg=("#08121a" if driver_on else self.TEXT),
            activebackground=driver_color)
        self.dot.itemconfig(self.dot_id, fill=state_color)

        if blocked:
            self.status_var.set(f"CPU bloqueada: use o botão 'Ativar driver {drv}'.\n"
                                "Se HVCI estiver ligado, instale o PawnIO.")
        elif driver_on:
            self.status_var.set(f"{drv} ATIVO — lendo a CPU real e enviando ao cooler.")
        else:
            self.status_var.set("Enviando temperatura para o display do cooler.")
        try:
            self.root.update()
        except tk.TclError:
            # A janela foi fechada enquanto o loop ainda atualizava.
            self.running = False

    def exit_app(self):
        self.running = False
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.destroy()

    def close(self):
        self.exit_app()

    def destroy(self):
        try:
            self.root.unbind_all("<MouseWheel>")
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Monitor de CPU Cooler - inicia o leitor LHM e envia a temperatura ao cooler"
    )
    ap.add_argument("--probe", action="store_true", help="apenas inspecionar o HID")
    ap.add_argument("--once", action="store_true", help="ler uma vez, enviar e sair")
    ap.add_argument("--dry-run", action="store_true", help="nao escrever no HID")
    ap.add_argument("--interval", type=float, default=0.5,
                    help="intervalo de leitura/envio em segundos (padrao 0,5)")
    ap.add_argument("--source", choices=("auto", "dll", "wmi"), default="auto",
                    help="origem dos sensores (padrao: auto)")
    ap.add_argument("--stop-sendtemp", action="store_true",
                    help="tenta encerrar o SendTemp.exe original antes de abrir o HID")
    ap.add_argument("--elevate", action="store_true",
                    help="solicitar administrador para carregar o driver WinRing0")
    ap.add_argument("--driver", action="store_true",
                    help="usa o driver WinRing0: eleva, le a CPU real e desliga o fallback da GPU")
    ap.add_argument("--no-gui", action="store_true",
                    help="nao abrir a janela do leitor (apenas console)")
    ap.add_argument("--start-minimized", action="store_true",
                    help="iniciar minimizado na bandeja (usado no logon do Windows)")
    ap.add_argument("--no-fallback", action="store_true",
                    help="(padrao) nunca usar a GPU no lugar da CPU")
    ap.add_argument("--fallback-gpu", action="store_true",
                    help="usar a GPU apenas se a CPU estiver bloqueada")
    ap.add_argument("--layout", choices=("direct", "original"), default="direct",
                    help="posicao da temperatura no relatorio HID (padrao: direct)")
    ap.add_argument("--unit", choices=("C", "F", "c", "f"), default="C",
                    help="unidade de exibicao (padrao C; envio ao cooler e sempre em C)")
    ap.add_argument("--test-display", type=int, metavar="N",
                    help="envia um valor fixo N (0-99) para testar o display do cooler")
    ap.add_argument("--install-startup", action="store_true",
                    help="inicia automaticamente com o Windows")
    ap.add_argument("--uninstall-startup", action="store_true",
                    help="remove a inicializacao automatica")
    ap.add_argument("--startup-status", action="store_true",
                    help="mostra se a inicializacao automatica esta ativa")
    args = ap.parse_args()
    set_app_user_model_id()

    if args.install_startup:
        ok, msg = install_auto_start()
        print(("[OK] " if ok else "[ERRO] ") + msg)
        return 0 if ok else 1
    if args.uninstall_startup:
        ok, msg = uninstall_auto_start()
        print(("[OK] " if ok else "[ERRO] ") + msg)
        return 0 if ok else 1
    if args.startup_status:
        print("Iniciar com Windows: " + ("SIM (tarefa elevada, sem UAC)"
                                          if auto_start_status() else "NAO"))
        return 0

    # Somente o monitoramento em si e unico; comandos de configuracao acima
    # precisam funcionar mesmo com outra instancia rodando.
    if not acquire_single_instance():
        print("[INFO] Ja existe uma instancia do Monitor CPU Cooler em execucao.")
        return 0

    if args.driver:
        args.elevate = True
        args.no_fallback = True
        args.fallback_gpu = False

    # O LibreHardwareMonitor carrega o driver WinRing0 embutido somente quando
    # o processo e elevado. --driver/--eleva faz o UAC e espera o filho.
    if args.elevate and not is_admin():
        print("[INFO] Solicitando administrador para carregar o driver WinRing0...")
        if args.driver:
            print("        Se a Integridade da memoria (HVCI) estiver ligada, o")
            print("        driver sera bloqueado — desative-a e reinicie primeiro.")
        result = relaunch_as_admin()
        if result is not False:
            return int(result) if isinstance(result, int) else 0
        print("[AVISO] Elevacao negada; continuando sem privilegios.")

    # A janela do leitor abre imediatamente, antes de qualquer leitura.
    gui = None
    if not args.no_gui:
        try:
            gui = MonitorGUI(args.unit.upper(),
                             start_minimized=args.start_minimized)
        except Exception as exc:
            print(f"[AVISO] Nao foi possivel abrir a janela: {exc}")

    paths = find_hid_paths()
    if not paths:
        print(f"[ERRO] Nenhum HID com VID {VID:04X} / PID {PID:04X} encontrado.")
        if gui:
            gui.destroy()
        return 2
    print(f"[HID] Dispositivo: {paths[0]}")

    if args.stop_sendtemp:
        stop_original()
        print("[AVISO] Tentativa de encerrar o SendTemp.exe original.")

    if args.probe:
        with open_cooler(paths[0]) as dev:
            print(f"[HID] Output report: {dev.output_len} bytes (ID 0 + 24 bytes)")
        if gui:
            gui.destroy()
        return 0

    dev = None
    if not args.dry_run:
        dev = open_cooler(paths[0])
        print(f"[HID] Conectado. Output report: {dev.output_len} bytes")

    interval_ms = max(50, int(args.interval * 1000))
    warned_temp = False
    permission_shown = False  # aviso so uma vez por execucao
    driver_mode = ""
    active_driver_mode = "auto"
    tried_drivers = set()
    zero_cycles = 0
    last_cpu_temp = 0.0
    last_cpu_time = 0.0
    last_valid = {}
    reader_retry_at = 0.0
    reader = None

    try:
        # Inicia o leitor do LibreHardwareMonitor junto com o monitor.
        if args.source in ("auto", "dll"):
            try:
                reader = LhmReader(interval_ms=interval_ms, once=args.once,
                                   driver_mode=active_driver_mode)
                print(f"[LEITOR] LibreHardwareMonitor iniciado "
                      f"({active_driver_mode}, {interval_ms} ms).")
            except Exception as exc:
                print(f"[AVISO] Nao foi possivel iniciar o leitor DLL: {exc}")
                if args.source == "dll":
                    return 1
                reader = None

        try:
            deadline = time.monotonic() + 30
            while gui is None or gui.running:
                values = None  # pode nao haver leitura nesta iteracao
                # Se o leitor morreu, tenta recria-lo (o WMI so e provisorio).
                if (reader is None and args.source in ("auto", "dll")
                        and time.monotonic() >= reader_retry_at):
                    try:
                        reader = LhmReader(interval_ms=interval_ms,
                                           driver_mode=active_driver_mode)
                        print("[LEITOR] Leitor reiniciado automaticamente.")
                    except Exception as exc:
                        print(f"[AVISO] Falha ao reiniciar leitor: {exc}")
                        reader_retry_at = time.monotonic() + 5
                # Nunca bloqueia o loop esperando o PowerShell: a janela
                # precisa continuar respondendo enquanto ele inicializa.
                if reader is not None:
                    try:
                        raw = reader.read_nowait()
                        if raw is not None:
                            driver_mode = str(raw.get("driver", "") or "")
                            if gui:
                                gui.driver_mode = driver_mode
                            values = normalize(raw)
                    except (RuntimeError, ValueError) as exc:
                        print(f"[AVISO] Leitor DLL falhou: {exc}")
                        reader.stop()
                        reader = None
                        reader_retry_at = time.monotonic() + 2
                        values = None
                else:
                    values = None

                if values is None:
                    if reader is not None and time.monotonic() < deadline:
                        if gui:
                            gui.pump()
                        time.sleep(0.05)
                        continue
                    try:
                        values = normalize(read_sensors_wmi())
                    except Exception as exc:
                        print(f"[AVISO] Falha ao ler WMI: {exc}")
                        values = dict(ZERO_SENSORS)

                if args.test_display is not None:
                    values["cpu_temp"] = max(0, min(99, args.test_display))

                # Troca automatica de driver: se o atual nao consegue ler a
                # CPU por varios ciclos, tenta o outro uma vez.
                if values.get("cpu_temp", 0) > 0:
                    zero_cycles = 0
                else:
                    zero_cycles += 1
                if (zero_cycles >= 12 and reader is not None
                        and len(tried_drivers) < 2):
                    atual = (driver_mode or "").lower()
                    outro = ("pawnio" if atual.startswith("winring")
                             else "winring0")
                    if outro in tried_drivers:
                        outro = ("winring0" if outro == "pawnio" else "pawnio")
                    if outro not in tried_drivers:
                        tried_drivers.add(outro)
                        active_driver_mode = outro
                        print(f"[DRIVER] Leitura zerada — trocando para {outro}.")
                        reader.stop()
                        reader = LhmReader(interval_ms=interval_ms,
                                           driver_mode=outro)
                        driver_mode = ""
                        zero_cycles = 0
                        continue

                # O sensor da CPU as vezes devolve 0 por um ciclo. Mantem o
                # ultimo valor valido por alguns segundos para o visor nao
                # piscar/ficar "bloqueado" sem motivo.
                if values["cpu_temp"] > 0:
                    last_cpu_temp = values["cpu_temp"]
                    last_cpu_time = time.monotonic()
                elif last_cpu_temp > 0 and \
                        time.monotonic() - last_cpu_time < 15:
                    values["cpu_temp"] = last_cpu_temp

                # Mesmo tratamento para os demais sensores: o LHM as vezes
                # devolve 0 por um ciclo e isso nao deve piscar no painel/HID.
                now = time.monotonic()
                for key in ("gpu_temp", "cpu_load", "mem_load", "gpu_load",
                            "board_temp", "ram_temp", "ram_total",
                            "ram_dimm1", "ram_dimm2", "ram_dimm3", "ram_dimm4",
                            "pump_rpm", "cpu_rpm", "sys_rpm", "fan3_rpm"):
                    if values.get(key, 0) > 0:
                        last_valid[key] = (values[key], now)
                    elif key in last_valid and now - last_valid[key][1] < 15:
                        values[key] = last_valid[key][0]

                fallback = False
                if (args.fallback_gpu and values["cpu_temp"] <= 0
                        and values["gpu_temp"] > 0):
                    values["cpu_temp"] = values["gpu_temp"]
                    fallback = True

                # NUNCA enviar 0 como temperatura da CPU para o visor.
                cpu_blocked = values["cpu_temp"] <= 0 and not fallback
                if cpu_blocked:
                    if not warned_temp:
                        print("[AVISO] Leitura da CPU bloqueada — nada sera enviado ao visor.")
                        print("        Motivo: WinRing0 barrado pelo HVCI e/ou falta de Admin.")
                        warned_temp = True
                    if not permission_shown:
                        if gui and not args.once:
                            gui.show_permission_dialog()
                        permission_shown = True
                    if gui:
                        gui.update(values, blocked=True)
                else:
                    warned_temp = False
                    if gui:
                        gui.update(values, blocked=False)

                if gui:
                    gui.process_queue()

                if not cpu_blocked:
                    # O display mostra somente a CPU (byte 1); os demais campos
                    # continuam sendo enviados normalmente no relatorio.
                    payload = build_payload(values)
                    unit = "°C" if args.unit.upper() == "C" else "°F"
                    print(f"CPU {to_display(values['cpu_temp'], args.unit.upper()):3d}{unit} | "
                          f"GPU {to_display(values['gpu_temp'], args.unit.upper()):3d}{unit} | "
                          f"CPU {int(values['cpu_load']):3d}% | RAM {int(values['mem_load']):3d}% | "
                          f"pkg {payload[1]:02X} {payload[2]:02X} {payload[3]:02X} ...",
                          flush=True)

                    if dev is not None:
                        try:
                            dev.send(payload, layout=args.layout)
                        except OSError as exc:
                            # USB pode ter sido desconectado: tenta reabrir uma vez.
                            print(f"[AVISO] Falha ao gravar no HID: {exc}")
                            try:
                                dev.close()
                                again = find_hid_paths()
                                if not again:
                                    raise RuntimeError("cooler nao encontrado")
                                dev = CoolerHID(again[0])
                                dev.send(payload, layout=args.layout)
                                print("[HID] Reconectado com sucesso.")
                            except Exception as exc2:
                                print(f"[ERRO] Falha ao reconectar: {exc2}")
                                return 1

                if args.once:
                    break
                if reader is None:
                    time.sleep(max(0.05, args.interval))
        except KeyboardInterrupt:
            print("\nEncerrado.")
        except Exception:
            print("[ERRO] Falha no monitor:")
            traceback.print_exc()
            if gui:
                gui.status_var.set("ERRO — veja o console. A janela ficara aberta.")
                while gui.running:
                    gui.root.update()
                    time.sleep(0.05)
    finally:
        if dev is not None:
            dev.close()
        if reader is not None:
            reader.stop()
            print("[LEITOR] Leitor do LibreHardwareMonitor encerrado.")
        if gui:
            gui.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
