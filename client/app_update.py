import base64
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile

import httpx

from .api import VERSION


REPOSITORY = "neezzq/GUP"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=30"
RELEASE_URL = f"https://github.com/{REPOSITORY}/releases/download"
ASSET_HOSTS = {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}
TAG = re.compile(r"v(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})-build\.([1-9][0-9]{0,9})")
HASH_LINE = re.compile(rb"([0-9a-fA-F]{64})[ \t]+\*?GameUpdater\.exe\r?\n?")
MAX_EXE_BYTES = 128 * 1024 * 1024
ERROR_FILE = "app-update-error.txt"


@dataclass(frozen=True, order=True)
class Build:
    version: tuple[int, int, int]
    number: int

    @classmethod
    def from_tag(cls, tag):
        match = TAG.fullmatch(tag) if isinstance(tag, str) else None
        if match:
            major, minor, patch, number = map(int, match.groups())
            return cls((major, minor, patch), number)
        return None

    @property
    def label(self):
        version = ".".join(map(str, self.version))
        return f"{version} · build {self.number}" if self.number else version

    @property
    def tag(self):
        return "v" + ".".join(map(str, self.version)) + f"-build.{self.number}"


def current_build():
    number = 0
    if getattr(sys, "frozen", False):
        metadata = Path(sys._MEIPASS) / "gup-build.txt"
        try:
            number = int(metadata.read_text(encoding="ascii"))
        except (OSError, ValueError):
            pass
    return Build(tuple(map(int, VERSION.split("."))), number)


def latest_build(client):
    response = client.get(API_URL, headers={"Accept": "application/vnd.github+json"})
    response.raise_for_status()
    releases = response.json()
    if not isinstance(releases, list):
        raise ValueError("Invalid release listing")
    builds = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") is not False:
            continue
        build = Build.from_tag(release.get("tag_name"))
        assets = release.get("assets")
        names = {item.get("name") for item in assets if isinstance(item, dict)
                 and isinstance(item.get("name"), str)} if isinstance(assets, list) else set()
        if build and {"GameUpdater.exe", "GameUpdater.exe.sha256"} <= names:
            builds.append(build)
    if not builds:
        raise ValueError("No complete Windows release found")
    return max(builds)


def asset_chunks(client, build, name, limit):
    request = client.build_request("GET", f"{RELEASE_URL}/{build.tag}/{name}")
    for _ in range(4):
        response = client.send(request, stream=True, follow_redirects=False)
        try:
            if response.is_redirect:
                request = response.next_request
                url = request.url if request else None
                if (not url or url.scheme != "https" or url.host not in ASSET_HOSTS
                        or url.port not in (None, 443) or url.userinfo):
                    raise ValueError("Unexpected release asset redirect")
                continue
            response.raise_for_status()
            total = 0
            for chunk in response.iter_bytes(chunk_size=128 * 1024):
                total += len(chunk)
                if total > limit:
                    raise ValueError("Release asset exceeds size limit")
                yield chunk
            return
        finally:
            response.close()
    raise ValueError("Too many release asset redirects")


def is_windows_x64(path):
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            return False
        offset = struct.unpack_from("<I", header, 60)[0]
        stream.seek(offset)
        return stream.read(6) == b"PE\x00\x00\x64\x86"


def download_build(client, build, path):
    checksum = b"".join(asset_chunks(client, build, "GameUpdater.exe.sha256", 256))
    expected = HASH_LINE.fullmatch(checksum)
    if not expected:
        raise ValueError("Invalid release checksum")
    sha = hashlib.sha256()
    with path.open("xb") as stream:
        for chunk in asset_chunks(client, build, "GameUpdater.exe", MAX_EXE_BYTES):
            sha.update(chunk)
            stream.write(chunk)
        stream.flush()
        os.fsync(stream.fileno())
    if sha.hexdigest().encode() != expected[1].lower() or not is_windows_x64(path):
        raise ValueError("Windows x64 executable verification failed")
    return sha.hexdigest()


REPLACE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$target = $env:GUP_UPDATE_TARGET
$stage = $env:GUP_UPDATE_STAGE
$errorFile = $env:GUP_UPDATE_ERROR
$payload = [IO.Path]::Combine($stage, 'payload.exe')
$backup = [IO.Path]::Combine($stage, 'previous.exe')
$failed = [IO.Path]::Combine($stage, 'failed.exe')

function Start-Application {
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $target
    $start.WorkingDirectory = [IO.Path]::GetDirectoryName($target)
    $start.UseShellExecute = $false
    $start.EnvironmentVariables['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    foreach ($key in @('TARGET', 'STAGE', 'ERROR', 'SHA', 'PID')) {
        $start.EnvironmentVariables.Remove("GUP_UPDATE_$key")
    }
    $process = [Diagnostics.Process]::Start($start)
    if ($process.WaitForExit(2000) -and $process.ExitCode -ne 0) {
        throw 'Application failed at startup'
    }
}

$replaced = $false
$keepBackup = $false
try {
    $current = Get-Process -Id ([int]$env:GUP_UPDATE_PID) -ErrorAction SilentlyContinue
    if ($current -and -not $current.WaitForExit(30000)) { throw 'Application is still running' }
    if ((Get-FileHash -LiteralPath $payload -Algorithm SHA256).Hash -ne $env:GUP_UPDATE_SHA) {
        throw 'Update checksum does not match'
    }

    # PyInstaller's parent process can hold the executable briefly after the GUI exits.
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            [IO.File]::Replace($payload, $target, $backup)
            $replaced = $true
            break
        } catch [IO.IOException] {
            if ($attempt -eq 39) { throw }
            Start-Sleep -Milliseconds 250
        }
    }
    Start-Application
} catch {
    try { [IO.File]::WriteAllText($errorFile, 'replace_failed') } catch {}
    if ($replaced -and [IO.File]::Exists($backup)) {
        $keepBackup = $true
        [IO.File]::Replace($backup, $target, $failed)
        $keepBackup = $false
    }
    if ([IO.File]::Exists($target)) { Start-Application }
} finally {
    if (-not $keepBackup -and [IO.File]::Exists($target)) {
        try { [IO.Directory]::Delete($stage, $true) } catch {}
    }
}
"""


def start_replacement(target, stage, sha, data_directory, *, parent_pid=None):
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not re.fullmatch(r"[a-f0-9]{64}", sha) or stage.parent != target.parent:
        raise ValueError("Invalid staged update")
    encoded = base64.b64encode(REPLACE_SCRIPT.encode("utf-16-le")).decode("ascii")
    return subprocess.Popen(
        [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        creationflags=subprocess.CREATE_NO_WINDOW,
        env={
            **os.environ, "GUP_UPDATE_TARGET": str(target), "GUP_UPDATE_STAGE": str(stage),
            "GUP_UPDATE_SHA": sha, "GUP_UPDATE_ERROR": str(data_directory / ERROR_FILE),
            "GUP_UPDATE_PID": str(os.getpid() if parent_pid is None else parent_pid),
        },
    )


def prepare_update(data_directory, status):
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        raise ValueError("Self-update requires the packaged Windows application")
    target = Path(sys.executable).resolve(strict=True)
    stage = None
    try:
        with httpx.Client(timeout=httpx.Timeout(30, connect=8), trust_env=False) as client:
            build = latest_build(client)
            if build <= current_build():
                return None
            status(build)
            stage = Path(tempfile.mkdtemp(prefix=".gup-update-", dir=target.parent))
            sha = download_build(client, build, stage / "payload.exe")
        start_replacement(target, stage, sha, data_directory)
    except BaseException:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        raise
    return build
