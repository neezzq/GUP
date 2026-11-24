from dataclasses import dataclass
from itertools import chain
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import psutil

from client.automation import (
    BUSY_TEXT, PLAY_BUTTONS, UPDATE_BUTTONS, LauncherUI, active_game,
    installed_programs, normalized, running_programs, show_launcher_window,
)
from client.security import desktop_blocker, verified_binary


class UnsafeDesktop(Exception):
    pass


@dataclass(frozen=True)
class Game:
    id: str
    name: str
    path: Path
    executable: Path | None = None


@dataclass(frozen=True)
class Progress:
    phase: str
    download_bytes: int | None = None
    total_bytes: int | None = None


@dataclass
class Observation:
    state: str = "unknown"
    progress: Progress | None = None
    window: object = None
    button: object = None


@dataclass(frozen=True)
class Result:
    status: str
    reason: str = ""
    updated: int = 0
    current: int = 0
    queue_safe: bool = True


def read_json(path):
    try:
        if path.stat().st_size <= 16 * 1024 * 1024:
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        pass
    return {}


def local_directory(variable, relative):
    root = os.environ.get(variable)
    return Path(root) / relative if root else None


def game_directory(path):
    if not isinstance(path, str) or not path:
        return None
    directory = Path(path)
    if directory.is_absolute() and directory.parent != directory and directory.is_dir():
        return directory.resolve()
    return None


class Launcher:
    key = ""
    paths = ()
    suffixes = ()
    publishers = ()
    display_names = ()
    helper_paths = ()
    login_hosts = ()
    minimum_free = 1024 ** 3

    def __init__(self, override="", programs=None):
        self.override = override
        self.programs = programs
        self.running_exes = ()
        self.exe = None
        self.spawned = None
        self.games = []
        self.protected_games = []
        self.locked = False
        self.ui = LauncherUI(self.owns_window, self.login_hosts)

    def inventory(self):
        return self.programs if self.programs is not None else tuple(installed_programs())

    def discover_paths(self):
        for program in self.inventory():
            if normalized(program.name) in self.display_names:
                if program.icon:
                    yield program.icon
                if program.location:
                    for relative in self.suffixes:
                        yield program.location / relative
        for env, relative in self.paths:
            if path := local_directory(env, relative):
                yield path

    def detect(self):
        if self.override:
            path = Path(self.override)
            paths = (path / suffix for suffix in self.suffixes) if path.is_dir() else (path,)
        else:
            paths = self.discover_paths()
        valid_names = {Path(suffix).name.casefold() for suffix in self.suffixes}
        installed = False
        running = () if self.override else self.running_exes
        candidates = chain(((path, True) for path in paths), ((path, False) for path in running))
        for path, expected_installation in candidates:
            try:
                if path.name.casefold() not in valid_names or not path.is_file():
                    continue
                installed = installed or expected_installation
                if not verified_binary(path, self.publishers):
                    continue
                resolved = path.resolve(strict=True)
                if verified_binary(resolved, self.publishers):
                    self.exe = resolved
                    return resolved
            except OSError:
                continue
        self.locked = bool(self.override or installed)
        return None

    def matches_process(self, exe):
        return self.exe is not None and (
            exe == self.exe or exe in {(self.exe.parent / path).resolve() for path in self.helper_paths}
        )

    def owns_window(self, window, *, fresh=False):
        try:
            import win32process

            _, pid = win32process.GetWindowThreadProcessId(window.handle)
            return pid == window.element_info.process_id and self.owns_process(pid, fresh=fresh)
        except (OSError, psutil.Error, AttributeError):
            return False

    def owns_process(self, pid, *, fresh=False):
        if self.exe is None:
            return False
        try:
            process = psutil.Process(pid)
            own = psutil.Process()
            if process.username().casefold() != own.username().casefold():
                return False
            import ctypes
            from ctypes import wintypes

            window_session, own_session = wintypes.DWORD(), wintypes.DWORD()
            api = ctypes.WinDLL("kernel32", use_last_error=True).ProcessIdToSessionId
            if not api(process.pid, ctypes.byref(window_session)) or not api(own.pid, ctypes.byref(own_session)):
                return False
            exe = Path(process.exe()).resolve(strict=True)
            return (window_session.value == own_session.value
                    and self.matches_process(exe) and verified_binary(exe, self.publishers, fresh=fresh))
        except (OSError, psutil.Error, AttributeError):
            return False

    def safety(self, *, check_space=True):
        try:
            if blocker := desktop_blocker():
                raise UnsafeDesktop(blocker)
            if active_game(self.protected_games or self.games, running_programs()):
                raise UnsafeDesktop("running_game")
            if check_space:
                paths = {game.path for game in self.games}
                if self.exe:
                    paths.add(self.exe.parent)
                for path in paths:
                    if shutil.disk_usage(path).free < self.minimum_free:
                        raise UnsafeDesktop("disk")
        except UnsafeDesktop:
            raise
        except Exception:
            raise UnsafeDesktop("desktop") from None

    def launch(self):
        self.safety()
        if not verified_binary(self.exe, self.publishers):
            raise UnsafeDesktop("signature")
        names = {self.exe.name.casefold(), *(Path(path).name.casefold() for path in self.helper_paths)}
        pids = {process.pid for process, exe in running_programs()
                if exe.name.casefold() in names and self.matches_process(exe)}
        if pids and show_launcher_window(pids, self.owns_process):
            return
        process = subprocess.Popen([str(self.exe)], cwd=self.exe.parent)
        if not pids:
            self.spawned = process

    def discover_games(self):
        return self.registry_games(self.publishers)

    def registry_games(self, publishers, match=None):
        expected = {normalized(name) for name in publishers}
        for program in self.inventory():
            if (normalized(program.name) in self.display_names
                    or not program.location or not program.location.is_dir()):
                continue
            if (match and match(program)) or normalized(program.publisher) in expected:
                path = game_directory(str(program.location))
                if path:
                    yield Game(f"{program.name.casefold()}|{path}", program.name, path)

    def check_auth(self):
        windows = self.ui.windows()
        if not windows:
            return "waiting"
        if challenge := self.ui.challenge(windows):
            return challenge
        needs_login = self.ui.needs_login(windows)
        if needs_login and self.ui.login_form(windows):
            return "login"
        if self.ui.external_sign_in(windows):
            return "external_login"
        if needs_login:
            return "manual_login"
        return "ready" if self.session_evidence(windows) else "unknown"

    def session_evidence(self, windows):
        return bool(self.ui.game_rows(windows, self.games))

    def login(self, credential_provider, revalidate):
        windows = self.ui.windows()
        form = self.ui.login_form(windows)
        if not form or self.ui.challenge(windows) or not self.owns_window(form[0], fresh=True):
            return False
        self.safety()
        credentials = credential_provider()
        try:
            return self.ui.enter_credentials(
                windows, credentials, self.safety, revalidate,
                lambda window: self.owns_window(window, fresh=True),
            )
        finally:
            del credentials

    def check_updates(self):
        windows = self.ui.windows()
        if challenge := self.ui.challenge(windows):
            return challenge, {}
        if self.ui.needs_login(windows):
            return "login", {}
        if self.ui.external_sign_in(windows):
            return "external_login", {}
        rows = self.ui.game_rows(windows, self.games)
        result = {}
        for game_id, (_, window, button, phase) in rows.items():
            label = normalized(button.window_text()) if button else ""
            if phase:
                result[game_id] = Observation("busy", Progress(phase), window, button)
            elif label in UPDATE_BUTTONS:
                result[game_id] = Observation("pending", window=window, button=button)
            elif label in PLAY_BUTTONS and button.is_enabled():
                result[game_id] = Observation("ready", window=window, button=button)
        queue_active = any(
            BUSY_TEXT.match(normalized(node.window_text()))
            for window in windows for node in self.ui.controls(window, "Text")
        )
        return ("active" if queue_active else "normal"), result

    def start_updates(self, game_id):
        state, updates = self.check_updates()
        item = updates.get(game_id)
        return (state in ("normal", "active") and item is not None and item.state == "pending"
                and self.ui.invoke_update(item.window, item.button, self.safety))

    def get_progress(self):
        return self.check_updates()

    def wait_for_completion(self, tick, emit, *, timeout=4 * 3600):
        started, active, updated, settled = set(), set(), set(), {}
        identified = set()
        last_activity = time.monotonic()
        deadline = last_activity + timeout
        quiet_observations = 0
        unresolved = True
        while time.monotonic() < deadline:
            tick(unresolved=unresolved)
            state, observations = self.get_progress()
            if state in ("challenge", "steam_guard"):
                return Result("confirmation_required", state, queue_safe=not unresolved)
            if state in ("login", "external_login"):
                reason = "external_login" if state == "external_login" else "login_required"
                return Result("login_required", reason, queue_safe=not started and not active)
            busy = {key for key, item in observations.items() if item.state == "busy"}
            pending = {key for key, item in observations.items() if item.state == "pending"}
            ready = {key for key, item in observations.items() if item.state in {"ready", "installed"}}
            active.update(busy)
            updated.update(key for key, item in observations.items() if item.state == "installed")
            for key in active - updated:
                settled[key] = settled.get(key, 0) + 1 if key in ready else 0
                if settled[key] >= 2:
                    updated.add(key)
            unresolved = bool(state == "active" or busy or (started | active) - updated)
            quiet_observations = 0 if unresolved or pending else quiet_observations + 1
            detected = (pending | busy | updated) - identified
            identified.update(detected)
            for game in self.games:
                if game.id in detected:
                    emit("detected", game.name, None, len(identified))
            for game in self.games:
                if game.id in busy:
                    emit("working", game.name, observations[game.id].progress, len(identified))
                    break
            if pending and not busy and state != "active" and not (started - updated):
                if not tick(new_work=True):
                    last_activity = time.monotonic()
                else:
                    for game in self.games:
                        if game.id not in pending - started:
                            continue
                        emit("update_available", game.name, None, len(identified))
                        if self.start_updates(game.id):
                            started.add(game.id)
                            unresolved = True
                            last_activity = time.monotonic()
                        break
            if quiet_observations >= 2 and self.games and len(ready) == len(self.games):
                return Result("updated" if updated else "current", updated=len(updated), current=len(ready - updated))
            if busy or state == "active":
                last_activity = time.monotonic()
            # Missing UI evidence is neither a successful update nor an idle queue.
            if time.monotonic() - last_activity > 90:
                return Result("unverified", "unknown_update", len(updated), len(ready - updated),
                              queue_safe=not unresolved)
            time.sleep(3)
        return Result("unverified", "timeout", len(updated), queue_safe=False)

    def close(self):
        if not self.spawned or self.spawned.poll() is not None:
            return
        state, observations = self.check_updates()
        if state != "normal" or not self.games or any(
            observations.get(game.id, Observation()).state not in {"ready", "installed"} for game in self.games
        ):
            return
        for window in self.ui.windows():
            if window.element_info.process_id == self.spawned.pid:
                self.ui.request_close(window, self.safety)
