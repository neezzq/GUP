import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx
import pytest

from client.app_update import Build, ERROR_FILE, download_build, latest_build, start_replacement


def release(tag, *, draft=False, complete=True):
    assets = [{"name": "GameUpdater.exe"}, {"name": "GameUpdater.exe.sha256"}]
    return {"tag_name": tag, "draft": draft, "assets": assets if complete else assets[:1]}


def test_release_selection_uses_version_then_build():
    data = [
        release("v0.1.2-build.70"), release("v0.2.0-build.1"), release("v0.2.0-build.2"),
        release("v1.0.0-build.80", draft=True), release("v2.0.0-build.92", complete=False),
        release("v3.0.0"), release("v4.0.0-build.2/../../other"),
    ]
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data))) as client:
        assert latest_build(client) == Build((0, 2, 0), 2)
    assert Build.from_tag("v0.2.0-build.2").label == "0.2.0 · build 2"


def test_download_verifies_hash_and_x64(tmp_path):
    payload = bytearray(70)
    payload[:2] = b"MZ"
    payload[60] = 64
    payload[64:] = b"PE\x00\x00\x64\x86"
    digest = hashlib.sha256(payload).hexdigest()
    build = Build((0, 1, 2), 3)

    def respond(request):
        if request.url.path.endswith(".sha256"):
            return httpx.Response(200, text=f"{digest}  GameUpdater.exe\r\n")
        if request.url.host == "github.com":
            return httpx.Response(302, headers={"Location": "https://release-assets.githubusercontent.com/test"})
        return httpx.Response(200, content=bytes(payload))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        destination = tmp_path / "download.exe"
        assert download_build(client, build, destination) == digest
        assert destination.read_bytes() == payload
        payload[10] = 12
        with pytest.raises(ValueError, match="verification failed"):
            download_build(client, build, tmp_path / "modified.exe")


def test_download_rejects_untrusted_redirect(tmp_path):
    def respond(request):
        assert request.url.host == "github.com"
        return httpx.Response(302, headers={"Location": "https://example.org/fake"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match="Unexpected"):
            download_build(client, Build((0, 1, 2), 3), tmp_path / "download.exe")


@pytest.mark.skipif(sys.platform != "win32", reason="Runs the real Windows replacement helper")
@pytest.mark.parametrize("valid", [True, False])
def test_windows_replacement_restarts_with_correct_exe(tmp_path, valid):
    csc = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not csc.is_file():
        pytest.skip("Windows .NET Framework compiler unavailable")
    target = tmp_path / "Game Updater тест.exe"
    stage = tmp_path / ".gup-update-test"
    stage.mkdir()
    payload = stage / "payload.exe"
    for path, text in ((target, "previous"), (payload, "updated")):
        source = stage / "probe.cs"
        source.write_text(
            'class Probe { static void Main() { System.IO.File.WriteAllText('
            'System.IO.Path.Combine(System.AppDomain.CurrentDomain.BaseDirectory, "restarted.txt"),'
            f'"{text}"); }} }}',
            encoding="ascii",
        )
        subprocess.run([str(csc), "/nologo", f"/out:{path}", str(source)], check=True)
    expected = hashlib.sha256((payload if valid else target).read_bytes()).hexdigest()
    actual = hashlib.sha256(payload.read_bytes()).hexdigest()
    process = start_replacement(
        target, stage, actual if valid else "0" * 64, tmp_path, parent_pid=2147483647,
    )
    assert process.wait(timeout=45) == 0
    assert hashlib.sha256(target.read_bytes()).hexdigest() == expected
    marker = tmp_path / "restarted.txt"
    for _ in range(100):
        if marker.is_file() and marker.stat().st_size:
            break
        time.sleep(.05)
    assert marker.read_text() == ("updated" if valid else "previous")
    assert (tmp_path / ERROR_FILE).exists() is not valid
