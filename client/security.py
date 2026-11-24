from functools import lru_cache
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def data_directory():
    return Path(os.environ["LOCALAPPDATA"]) / "GameUpdater"


def credential_store():
    from keyring.backends.Windows import WinVaultKeyring
    return WinVaultKeyring()


def write_settings(path, settings):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".new")
    try:
        temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def enter_instance():
    import win32api
    import win32event
    import winerror

    mutex = win32event.CreateMutex(None, True, r"Local\GameUpdater.Maintenance")
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        win32api.CloseHandle(mutex)
        return None
    return mutex


def interactive_desktop():
    import ctypes
    from ctypes import wintypes
    import win32ts

    handle = win32ts.WTS_CURRENT_SERVER_HANDLE
    session_id = wintypes.DWORD()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id)):
        return False
    own_session = session_id.value
    sessions = win32ts.WTSEnumerateSessions(handle)
    if not any(item["SessionId"] == own_session and item["State"] == win32ts.WTSActive for item in sessions):
        return False
    for item in sessions:
        session = item["SessionId"]
        if session in (0, own_session):
            continue
        if win32ts.WTSQuerySessionInformation(handle, session, win32ts.WTSUserName):
            return False

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.OpenInputDesktop.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    user.OpenInputDesktop.restype = wintypes.HANDLE
    user.GetUserObjectInformationW.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    )
    user.CloseDesktop.argtypes = (wintypes.HANDLE,)
    desktop = user.OpenInputDesktop(0, False, 0x0001)
    if not desktop:
        return False
    try:
        name, needed = ctypes.create_unicode_buffer(256), wintypes.DWORD()
        ok = user.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed))
        return bool(ok) and name.value.casefold() == "default"
    finally:
        user.CloseDesktop(desktop)


@lru_cache(maxsize=64)
def _signer(path, modification, size):
    if sys.platform != "win32":
        return ""
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = (
        "$s=Get-AuthenticodeSignature -LiteralPath $env:GUP_SIGNATURE_PATH; "
        "if ($s.Status -eq 'Valid') { "
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "$s.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName,$false) }"
    )
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
            env={**os.environ, "GUP_SIGNATURE_PATH": str(path)}, capture_output=True,
            encoding="utf-8", errors="replace", timeout=15, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip().casefold()


def verified_binary(path, publishers, *, fresh=False):
    if not path.is_absolute() or not re.match(r"^[A-Za-z]:$", path.drive):
        return False
    try:
        stat = path.stat()
        check = _signer.__wrapped__ if fresh else _signer
        return check(path, stat.st_mtime_ns, stat.st_size) in {name.casefold() for name in publishers}
    except OSError:
        return False


def launcher_icon(path):
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = (
        "Add-Type -AssemblyName System.Drawing; "
        "$icon=[Drawing.Icon]::ExtractAssociatedIcon($env:GUP_ICON_PATH); "
        "if ($icon) { $bitmap=$icon.ToBitmap(); $stream=New-Object IO.MemoryStream; "
        "$bitmap.Save($stream,[Drawing.Imaging.ImageFormat]::Png); "
        "[Convert]::ToBase64String($stream.ToArray()) }"
    )
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
            env={**os.environ, "GUP_ICON_PATH": str(path)}, capture_output=True,
            timeout=10, check=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if len(result.stdout) <= 256_000:
            return base64.b64decode(result.stdout.strip(), validate=True)
    except (ValueError, OSError, subprocess.SubprocessError):
        pass
    return None
