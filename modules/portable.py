"""Platform layer: file locks, process identity and background services.

Linux keeps its original behaviour (fcntl, /proc, systemd --user). Windows uses
LockFileEx, the Toolhelp process snapshot and detached processes tracked by
pid files, so csw needs nothing beyond the Python standard library.
"""
import os
from pathlib import Path
import subprocess
import sys

WINDOWS = os.name == "nt"
CSW_BIN = Path.home() / ".local/bin/csw"
DATA_DIR = Path.home() / ".local/share/csw"


def script_path(path):
    """Path as written into hook commands: Claude runs hooks through bash on Windows too."""
    return Path(path).as_posix() if WINDOWS else str(path)


if WINDOWS:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    _kernel32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]

    def flock(stream, exclusive=True, blocking=True):
        flags = (0x2 if exclusive else 0) | (0 if blocking else 0x1)  # LOCKFILE_EXCLUSIVE_LOCK / FAIL_IMMEDIATELY
        handle = msvcrt.get_osfhandle(stream.fileno())
        # The lock lives as long as the handle: closing the stream releases it, like flock.
        if not _kernel32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(_OVERLAPPED())):
            error = ctypes.get_last_error()
            if error == 33:  # ERROR_LOCK_VIOLATION
                raise BlockingIOError(error, "locked")
            raise ctypes.WinError(error)

    def process_start(pid):
        """Creation time of a live process, or None. Same role as /proc starttime."""
        try:
            handle = _kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        except (TypeError, ValueError, OverflowError):
            return None
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not _kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            _kernel32.CloseHandle(handle)

    def process_table():
        """{pid: (parent pid, executable name)} for every process."""
        snapshot = _kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if snapshot in (None, wintypes.HANDLE(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        table = {}
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
                ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            _kernel32.CloseHandle(snapshot)
        return table

    def process_parent(pid, table=None):
        """(parent pid, executable name) of pid; raises OSError when it is gone."""
        row = (table or process_table()).get(int(pid))
        if row is None:
            raise ProcessLookupError(pid)
        return row
else:
    import fcntl

    def flock(stream, exclusive=True, blocking=True):
        fcntl.flock(stream, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | (0 if blocking else fcntl.LOCK_NB))

    def process_start(pid):
        try:
            # comm can contain spaces and parentheses; starttime is field 22.
            fields = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
            return fields[19] if fields[0] != "Z" else None
        except (OSError, ValueError, IndexError, TypeError):
            return None

    def process_parent(pid, table=None):
        directory = Path(f"/proc/{int(pid)}")
        command = (directory / "cmdline").read_bytes().split(b"\0")[0]
        fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[1]), Path(os.fsdecode(command)).name


# --- background services -----------------------------------------------------
# Linux: systemd --user units. Windows: one detached process per unit, its pid
# in ~/.claude/services/<unit>.pid; "enable" adds a per-user Run entry (no admin)
# that starts the unit at logon through pythonw, so no console window appears.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _run_value(unit):
    return "csw " + unit


def _set_autostart(unit, state_root, config_root, enabled):
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            windowless = Path(sys.executable).with_name("pythonw.exe")
            python = windowless if windowless.is_file() else Path(sys.executable)
            command = subprocess.list2cmdline([str(python), str(Path(__file__).resolve()), "start", unit,
                                               str(state_root), str(config_root)])
            winreg.SetValueEx(key, _run_value(unit), 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, _run_value(unit))
            except FileNotFoundError:
                pass


def _autostart_enabled(unit):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, _run_value(unit))
        return True
    except FileNotFoundError:
        return False

def _service_command(unit, config_root):
    home = Path.home()
    if unit == "csw-deepseek-compat.service":
        module = Path(__file__).resolve().parent / "deepseek_compat.py"
        return [sys.executable, str(module)], {}
    if unit == "claude-code-proxy.service":
        return ["claude-code-proxy", "serve", "--no-monitor"], {"CCP_CONFIG_DIR": str(config_root)}
    if unit.startswith("claude-code-proxy@") and unit.endswith(".service"):
        instance = unit[len("claude-code-proxy@"):-len(".service")]
        return (["claude-code-proxy", "serve", "--no-monitor"],
                {"CCP_CONFIG_DIR": str(config_root / "accounts" / instance),
                 "XDG_STATE_HOME": str(home / ".local/state/claude-code-proxy-accounts" / instance)})
    raise ValueError(f"Servicio desconocido: {unit}")


def _pid_file(state_root, unit):
    return Path(state_root) / "services" / (unit + ".pid")


def _service_alive(state_root, unit):
    try:
        pid, start = _pid_file(state_root, unit).read_text().split()
    except (OSError, ValueError):
        return None
    return int(pid) if process_start(pid) == start else None


def systemctl(args, state_root, config_root, check=False, capture_output=False):
    """subprocess.run(["systemctl", "--user", *args]) with a Windows equivalent."""
    if not WINDOWS:
        return subprocess.run(["systemctl", "--user", *args], check=check, capture_output=capture_output)
    action = args[0]
    now = "--now" in args
    unit = [a for a in args[1:] if a != "--now"][-1]
    code = 0
    if action in ("enable", "disable"):
        _service_command(unit, config_root)  # unknown units fail before touching the registry
        _set_autostart(unit, state_root, config_root, action == "enable")
    if action == "is-enabled":
        code = 0 if _autostart_enabled(unit) else 1
    elif action == "is-active":
        code = 0 if _service_alive(state_root, unit) else 3
    elif action in ("start", "restart") or (action == "enable" and now):
        if action == "restart" or not _service_alive(state_root, unit):
            if action == "restart":
                systemctl(["stop", unit], state_root, config_root)
            command, extra = _service_command(unit, config_root)
            environment = {**os.environ, **extra}
            log = _pid_file(state_root, unit).with_suffix(".log")
            log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with log.open("ab") as output:
                process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL, stdout=output,
                                           stderr=subprocess.STDOUT,
                                           creationflags=subprocess.DETACHED_PROCESS
                                           | subprocess.CREATE_NEW_PROCESS_GROUP
                                           | subprocess.CREATE_NO_WINDOW)
            _pid_file(state_root, unit).write_text(f"{process.pid} {process_start(process.pid)}\n")
    elif action == "stop" or (action == "disable" and now):
        pid = _service_alive(state_root, unit)
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        _pid_file(state_root, unit).unlink(missing_ok=True)
    elif action not in ("enable", "disable", "daemon-reload"):
        code = 1
    result = subprocess.CompletedProcess(["systemctl", "--user", *args], code, b"" if capture_output else None,
                                         b"" if capture_output else None)
    if check:
        result.check_returncode()
    return result


if __name__ == "__main__" and sys.argv[1:2] == ["start"]:
    # Logon entry written by "enable": portable.py start <unit> <state_root> <config_root>
    _, _, unit, state_root, config_root = sys.argv
    systemctl(["start", unit], Path(state_root), Path(config_root))
elif __name__ == "__main__":
    # Self-check: locks exclude each other, our own process is visible.
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder, "x.lock")
        with path.open("a") as first, path.open("a") as second:
            flock(first, exclusive=True)
            try:
                flock(second, exclusive=False, blocking=False)
                raise AssertionError("second lock should have failed")
            except BlockingIOError:
                pass
        with path.open("a") as first, path.open("a") as second:
            flock(first, exclusive=False)
            flock(second, exclusive=False, blocking=False)  # shared + shared is fine
    assert process_start(os.getpid())
    assert process_start(99999999) is None
    parent, _ = process_parent(os.getpid())
    assert parent == os.getppid(), (parent, os.getppid())
    print("portable ok")
