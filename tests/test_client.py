from contextlib import contextmanager

import httpx
import pytest

from client.api import API, ServerRejected, ServerUnavailable, normalize_server
from client.automation import active_game
from client.launchers import LAUNCHERS, adapters
from client.launchers.base import Game, Launcher, Observation, Progress, Result, UnsafeDesktop
from client.launchers.epic import Epic
from client.launchers.steam import Steam
from client.updater import Updater


@pytest.mark.parametrize("address", ("http://localhost", "https://user:secret@example.com", "https://example.com/path",
                                     "https://example.com?q=1", "https://example.com/#f"))
def test_https_only(address):
    with pytest.raises(ValueError):
        normalize_server(address)


def test_api_does_not_redirect_and_distinguishes_revocation():
    seen = []

    def redirect(request):
        seen.append((request.url.host, request.headers.get("Authorization")))
        return httpx.Response(302, headers={"Location": "https://other.test/api/v1/devices/heartbeat"})

    with API("https://updates.test", "token", transport=httpx.MockTransport(redirect)) as api:
        with pytest.raises(ServerRejected) as error:
            api.heartbeat()
    assert error.value.status == 302
    assert seen == [("updates.test", "Bearer token")]


def test_inventory_and_running_game(tmp_path, monkeypatch):
    assert list(adapters()) == list(LAUNCHERS)
    steam = Steam()
    steam.exe = tmp_path / "Steam" / "steam.exe"
    apps = steam.exe.parent / "steamapps"
    game_path = apps / "common" / "Example Game"
    game_path.mkdir(parents=True)
    (apps / "appmanifest_123.acf").write_text(
        '"AppState"\n{\n"appid" "123"\n"name" "Example Game"\n"installdir" "Example Game"\n'
        '"StateFlags" "1048578"\n"BytesDownloaded" "100"\n"BytesToDownload" "200"\n"buildid" "8"\n}\n'
    )
    (apps / "appmanifest_456.acf").write_text(
        '"AppState" {"appid" "456" "name" "Outside" "installdir" "../"}'
    )
    games = list(steam.discover_games())
    assert games == [Game("123", "Example Game", game_path)]
    assert active_game(games, [(None, game_path / "bin" / "game.exe")])
    assert not active_game(games, [(None, tmp_path / "different.exe")])
    monkeypatch.setattr(Launcher, "check_updates", lambda self: ("normal", {}))
    assert steam.check_updates()[1]["123"].progress == Progress("downloading", 100, 200)


def test_epic_manifest_does_not_discover_incomplete_or_outside_executables(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("ProgramData", str(tmp_path))
    directory = tmp_path / "Epic/EpicGamesLauncher/Data/Manifests"
    directory.mkdir(parents=True)
    game = tmp_path / "Game"
    game.mkdir()
    exe = game / "game.exe"
    exe.touch()
    data = {"InstallLocation": str(game), "LaunchExecutable": exe.name, "AppName": "abc", "DisplayName": "Game"}
    for index, metadata in enumerate((data, {**data, "AppName": "incomplete", "bIsIncompleteInstall": True},
                                            {**data, "AppName": "traversal", "LaunchExecutable": "../outside.exe"})):
        (directory / f"{index}.item").write_text(json.dumps(metadata))
    assert list(Epic().discover_games()) == [Game("abc", "Game", game, exe)]


def test_completion_waits_for_installation_not_download(tmp_path, monkeypatch):
    monkeypatch.setattr("client.launchers.base.time.sleep", lambda seconds: None)
    launcher = Launcher()
    launcher.games = [Game("id", "Game", tmp_path)]
    sequence = iter([
        ("normal", {"id": Observation("pending")}),
        ("active", {"id": Observation("busy", Progress("downloading", 100, 100))}),
        ("active", {"id": Observation("busy", Progress("installing"))}),
        ("normal", {"id": Observation("ready")}),
        ("normal", {"id": Observation("ready")}),
        ("normal", {"id": Observation("ready")}),
    ])
    monkeypatch.setattr(launcher, "get_progress", lambda: next(sequence))
    invoked, progress = [], []
    monkeypatch.setattr(launcher, "start_updates", lambda game: invoked.append(game) or True)
    result = launcher.wait_for_completion(lambda **flags: True, lambda *args: progress.append(args))
    assert result == Result("updated", updated=1)
    assert invoked == ["id"]
    assert [item[2].phase for item in progress if item[2]] == ["downloading", "installing"]


class ExampleLauncher:
    key = "steam"
    locked = False
    auth = "ready"

    def __init__(self, path, programs):
        self.trace = []

    def detect(self):
        return True

    def discover_games(self):
        return []

    def safety(self):
        pass

    def launch(self):
        self.trace.append("launch")

    def check_auth(self):
        return self.auth

    def login(self, credentials, revalidate):
        assert credentials() == ("test-user", "test-password")
        revalidate()
        self.auth = "ready"
        return True

    def wait_for_completion(self, tick, emit):
        tick()
        return Result("current", current=1)

    def close(self):
        self.trace.append("close")


class ExampleAPI:
    def __init__(self, network="online"):
        self.network = network
        self.calls = []
        self.statuses = []

    def heartbeat(self):
        if self.network == "offline":
            raise ServerUnavailable
        if self.network == "denied":
            raise ServerRejected(401)

    def configured(self):
        return {"steam"}

    def report(self, key, status, updated, current):
        self.statuses.append((key, status))

    @contextmanager
    def lease(self, key):
        self.calls.append(key)
        yield self

    def credentials(self):
        self.calls.append("credentials")
        return "test-user", "test-password"

    def confirm(self):
        pass

    def renew_if_due(self):
        pass


@pytest.mark.parametrize(
    ("network", "auth", "results", "calls"), [
        ("online", "ready", ["current", "current"], ["steam"]),
        ("online", "login", ["current", "current"], ["steam", "credentials"]),
        ("online", "challenge", ["confirmation_required", "current"], ["steam"]),
        ("offline", "ready", ["current", "current"], []),
        ("offline", "login", ["login_required", "current"], []),
        ("denied", "ready", [], []),
    ]
)
def test_queue_local_session_login_offline_and_revocation(network, auth, results, calls):
    steam = type("Steam", (ExampleLauncher,), {"auth": auth})
    epic = type("Epic", (ExampleLauncher,), {"key": "epic"})
    notices, api = [], ExampleAPI(network)
    updater = Updater({"maintenance": True}, api, notices.append, {"steam": steam, "epic": epic})
    updater.delay = lambda seconds: None
    updater.run()
    assert [item.status for item in notices if item.launcher and item.result] == results
    assert api.calls == calls
    assert notices[-1].status == "finished"


def test_retry_and_skip_running_game_instead_of_launching():
    class SteamLauncher(ExampleLauncher):
        attempts = 0

        def launch(self):
            type(self).attempts += 1
            if self.attempts < 3:
                raise OSError
            raise UnsafeDesktop("running_game")

    events = []
    updater = Updater({"maintenance": True, "enabled": ["steam"]}, ExampleAPI(), events.append, {"steam": SteamLauncher})
    updater.delay = lambda seconds: None
    updater.run()
    assert SteamLauncher.attempts == 3
    assert next(item.result.reason for item in events if item.result and item.launcher) == "running_game"


def test_lease_wait_occurs_before_official_launcher_starts():
    trace = []

    class SteamLauncher(ExampleLauncher):
        def launch(self):
            trace.append("launch")

    class BusyOnce(ExampleAPI):
        @contextmanager
        def lease(self, key):
            trace.append("lease_attempt")
            if len(trace) == 1:
                raise ServerRejected(409)
            yield self

    updater = Updater({"maintenance": True}, BusyOnce(), lambda event: None, {"steam": SteamLauncher})
    updater.delay = lambda seconds: None
    updater.run()
    assert trace == ["lease_attempt", "lease_attempt", "launch"]
