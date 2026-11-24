from pathlib import Path
import subprocess
import sys

import vdf

from client.automation import registry_values
from client.security import verified_binary
from .base import Game, Launcher, Observation, Progress, game_directory


UPDATE_REQUIRED = 0x2
FULLY_INSTALLED = 0x4
UPDATE_PAUSED = 0x100
UPDATE_STARTED = 0x400
RECONFIGURING = 0x10000
VALIDATING = 0x20000
ADDING_FILES = 0x40000
PREALLOCATING = 0x80000
DOWNLOADING = 0x100000
STAGING = 0x200000
COMMITTING = 0x400000
STOPPING = 0x800000
WRITING = RECONFIGURING | VALIDATING | ADDING_FILES | PREALLOCATING | DOWNLOADING | STAGING | COMMITTING | STOPPING


def read_vdf(path):
    try:
        if path.stat().st_size <= 16 * 1024 * 1024:
            with path.open(encoding="utf-8-sig") as file:
                return vdf.load(file)
    except (OSError, ValueError, SyntaxError):
        pass
    return {}


def number(data, field):
    try:
        return max(0, int(data.get(field, 0)))
    except (ValueError, TypeError):
        return 0


class Steam(Launcher):
    key = "steam"
    login_hosts = ('store.steampowered.com', 'login.steampowered.com')
    display_names = ("steam",)
    publishers = ("Valve Corp.", "Valve Corporation", "Valve")
    suffixes = ("steam.exe",)
    paths = (("ProgramFiles(x86)", "Steam/steam.exe"), ("ProgramFiles", "Steam/steam.exe"))
    helper_paths = ("bin/cef/cef.win7x64/steamwebhelper.exe", "bin/cef/cef.win7/steamwebhelper.exe")

    def discover_paths(self):
        if sys.platform == "win32":
            import winreg

            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                for entry in registry_values(hive, r"Software\Valve\Steam", ("SteamExe", "SteamPath", "InstallPath")):
                    for key, value in entry.items():
                        if isinstance(value, str) and value:
                            yield Path(value) if key == "SteamExe" else Path(value) / "steam.exe"
        yield from super().discover_paths()

    def library_paths(self):
        if not self.exe:
            return
        root = self.exe.parent
        yield root
        groups = read_vdf(root / "steamapps/libraryfolders.vdf").get("libraryfolders", {})
        if isinstance(groups, dict):
            for index, item in groups.items():
                if index.isdecimal():
                    value = item.get("path") if isinstance(item, dict) else item
                    if path := game_directory(value):
                        if path != root:
                            yield path

    def manifests(self):
        seen = set()
        for library in self.library_paths():
            for path in (library / "steamapps").glob("appmanifest_*.acf"):
                app = read_vdf(path).get("AppState", {})
                identifier = app.get("appid") if isinstance(app, dict) else None
                if not isinstance(identifier, str) or not identifier.isdecimal() or identifier in seen:
                    continue
                name, subdirectory = app.get("name"), app.get("installdir")
                if not isinstance(name, str) or not name or not isinstance(subdirectory, str):
                    continue
                root = (library / "steamapps/common").resolve()
                directory = game_directory(str(root / subdirectory))
                if directory and directory != root and directory.is_relative_to(root):
                    seen.add(identifier)
                    yield Game(identifier, name, directory), app

    def discover_games(self):
        return (game for game, _ in self.manifests())

    def open_downloads(self):
        self.safety()
        if verified_binary(self.exe, self.publishers):
            subprocess.Popen([str(self.exe), "steam://nav/downloads"], cwd=self.exe.parent)

    def session_evidence(self, windows):
        # Installed manifests survive logout. Require a known title or the actual downloads surface.
        if super().session_evidence(windows):
            return True
        return any(
            (window.window_text() or "").strip().casefold() in
            ("steam — downloads", "steam - downloads", "steam — загрузки", "steam - загрузки")
            for window in windows
        )

    def check_updates(self):
        state, observations = super().check_updates()
        if state in ("challenge", "steam_guard", "login", "external_login"):
            return state, observations
        for game, app in self.manifests():
            flags = number(app, "StateFlags")
            if flags & WRITING:
                total, downloaded = number(app, "BytesToDownload"), number(app, "BytesDownloaded")
                phase = "downloading" if flags & DOWNLOADING else "verifying" if flags & VALIDATING else "installing"
                progress = Progress(phase, downloaded, total) if flags & DOWNLOADING and 0 <= downloaded <= total and total else Progress(phase)
                observations[game.id] = Observation("busy", progress)
            elif flags & (UPDATE_REQUIRED | UPDATE_STARTED | UPDATE_PAUSED):
                observations[game.id] = Observation("pending")
            elif flags == FULLY_INSTALLED and self._build_changed(game.id, app):
                observations[game.id] = Observation("installed")
        return state, observations

    def launch(self):
        self.builds = {game.id: app.get("buildid") for game, app in self.manifests()}
        super().launch()

    def _build_changed(self, game_id, app):
        before = getattr(self, "builds", {}).get(game_id)
        after = app.get("buildid")
        return bool(before and after and before != after)

    def start_updates(self, game_id):
        state, observations = Launcher.check_updates(self)
        item = observations.get(game_id)
        return (state in ("normal", "active") and item is not None and item.state == "pending"
                and self.ui.invoke_update(item.window, item.button, self.safety))
