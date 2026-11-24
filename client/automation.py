from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
import re
import sys

import psutil


@dataclass(frozen=True)
class InstalledProgram:
    name: str
    publisher: str
    location: Path | None
    uninstall: str


def registry_values(hive, key, names):
    import winreg

    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as entry:
                values = {}
                for name in names:
                    try:
                        values[name] = winreg.QueryValueEx(entry, name)[0]
                    except FileNotFoundError:
                        pass
                yield values
        except (FileNotFoundError, PermissionError):
            continue


def installed_programs() -> Iterator[InstalledProgram]:
    if sys.platform != "win32":
        return
    import os
    import winreg

    key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    fields = ("DisplayName", "Publisher", "InstallLocation", "UninstallString")
    seen = set()
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as root:
                    names = [winreg.EnumKey(root, i) for i in range(winreg.QueryInfoKey(root)[0])]
            except OSError:
                continue
            for name in names:
                for entry in registry_values(hive, f"{key}\\{name}", fields):
                    label = entry.get("DisplayName")
                    if not isinstance(label, str) or not label:
                        continue
                    location = str(entry.get("InstallLocation", "")).strip().strip('"')
                    path = Path(os.path.expandvars(location)) if location else None
                    program = InstalledProgram(
                        label, str(entry.get("Publisher", "")),
                        path if path and path.is_absolute() else None,
                        str(entry.get("UninstallString", "")),
                    )
                    if program not in seen:
                        seen.add(program)
                        yield program


def running_programs():
    for process in psutil.process_iter(("pid", "exe", "username")):
        exe = process.info["exe"]
        if exe:
            yield process, Path(exe).resolve()


def active_game(games, programs):
    roots = {game.path.resolve() for game in games}
    return any(any(exe.is_relative_to(path) for path in roots) for _, exe in programs)


def normalized(text):
    return " ".join((text or "").casefold().split())


BUSY_TEXT = re.compile(
    r"^(downloading|installing|verifying|patching|unpacking|updating|загрузка|скачивание|"
    r"установка|проверка файлов|распаковка|обновление)(?:[ …:.]|$)"
)
CHALLENGE = re.compile(
    r"\b(captcha|steam guard|two.factor|verification code|security code|verify your identity|check your email|confirm sign.in|"
    r"подтверждение входа|код подтверждения|код безопасности|двухфактор\w*|restart required|"
    r"требуется перезагрузка|new device|новое устройство|проверьте почту|подтвердите вход)\b"
)
STEAM_GUARD = re.compile(r"\bsteam guard\b|\bподтвердите вход в приложении steam\b")
EXTERNAL_SIGN_IN = re.compile(
    r"^(?:(?:sign in|log in|continue) with|(?:войти|продолжить) через) "
    r"(?:steam|google|apple|xbox|playstation|nintendo|facebook|lego)(?: account| аккаунт)?$"
)
UPDATE_BUTTONS = {"update", "update now", "обновить", "обновить сейчас"}
PLAY_BUTTONS = {"play", "запустить", "играть"}
SIGN_IN_BUTTONS = {"sign in", "log in", "войти"}
USER_FIELDS = {
    "email", "email address", "email or phone", "username", "account name",
    "электронная почта", "адрес электронной почты", "имя пользователя", "имя аккаунта", "логин",
}


def visible(control):
    try:
        return control.is_visible()
    except Exception:
        return False


@lru_cache(maxsize=1)
def desktop_class():
    import comtypes.client
    import win32api

    # An explicit system path avoids resolving a registered typelib resource as a file.
    get_module = comtypes.client.GetModule
    uia_path = str(Path(win32api.GetSystemDirectory()) / "UIAutomationCore.dll")

    def load_module(library):
        if isinstance(library, str) and library.casefold() == "uiautomationcore.dll":
            library = uia_path
        return get_module(library)

    comtypes.client.GetModule = load_module
    try:
        from pywinauto import Desktop
    finally:
        comtypes.client.GetModule = get_module
    return Desktop


class LauncherUI:
    def __init__(self, owns_window, login_hosts=()):
        self.owns_window = owns_window
        self.login_hosts = set(login_hosts)

    def windows(self):
        desktop = desktop_class()(backend="uia")
        return [window for window in desktop.windows(visible_only=True)
                if self.owns_window(window)]

    def controls(self, window, kind):
        return [node for node in window.descendants(control_type=kind) if visible(node)]

    def challenge(self, windows):
        texts = [normalized(node.window_text()) for window in windows
                 for node in [window, *self.controls(window, "Text")]]
        if any(STEAM_GUARD.search(text) for text in texts):
            return "steam_guard"
        return "challenge" if any(CHALLENGE.search(text) for text in texts) else None

    def external_sign_in(self, windows):
        return any(
            EXTERNAL_SIGN_IN.fullmatch(normalized(button.window_text()))
            for window in windows for button in self.controls(window, "Button")
        )

    def trusted_login_scope(self, scope, window):
        while scope is not None:
            if scope.element_info.control_type == "Document":
                try:
                    url = urlsplit(scope.iface_value.CurrentValue)
                    return (url.scheme == "https" and url.hostname in self.login_hosts
                            and url.port in (None, 443) and url.username is None)
                except Exception:
                    return False
            if scope == window:
                return not self.controls(window, "Document")
            scope = scope.parent()
        return False

    def login_form(self, windows):
        forms = []
        for window in windows:
            edits = self.controls(window, "Edit")
            users, passwords = [], []
            for node in edits:
                info = node.element_info
                if info.element.CurrentIsPassword:
                    passwords.append(node)
                elif normalized(info.name) in USER_FIELDS:
                    users.append(node)
            buttons = [node for node in self.controls(window, "Button")
                       if normalized(node.window_text()) in SIGN_IN_BUTTONS]
            if len(users) == len(passwords) == len(buttons) == 1 and len(edits) == 2:
                matched = [*users, *passwords, *buttons]
                if all(self.trusted_login_scope(node, window) for node in matched):
                    forms.append((window, *matched))
        return forms[0] if len(forms) == 1 else None

    def needs_login(self, windows):
        return any(
            node.element_info.element.CurrentIsPassword
            for window in windows for node in self.controls(window, "Edit")
        ) or any(
            normalized(node.window_text()) in SIGN_IN_BUTTONS
            for window in windows for node in [window, *self.controls(window, "Button")]
        )

    def enter_credentials(self, windows, credentials, safety, revalidate, verify_owner):
        form = self.login_form(windows)
        if form is None or self.challenge(windows):
            return False
        window, username, password, submit = form
        entered = False
        try:
            safety()
            if not verify_owner(window):
                return False
            revalidate()
            if not all(self.trusted_login_scope(node, window) for node in (username, password, submit)):
                return False
            username.iface_value.SetValue(credentials[0])
            safety()
            if (not self.owns_window(window) or not password.element_info.element.CurrentIsPassword
                    or not self.trusted_login_scope(password, window) or self.challenge(self.windows())):
                return False
            entered = True
            password.iface_value.SetValue(credentials[1])
            safety()
            if (not self.owns_window(window) or not self.trusted_login_scope(submit, window)
                    or self.challenge(self.windows())):
                return False
            submit.iface_invoke.Invoke()
            return True
        finally:
            # A client may destroy the form on submit. If any field survives, clear it.
            try:
                if entered and self.owns_window(window) and password.element_info.element.CurrentIsPassword:
                    password.iface_value.SetValue("")
            except Exception:
                pass

    def game_rows(self, windows, games):
        by_title = {}
        for game in games:
            title = normalized(game.name)
            by_title[title] = None if title in by_title else game
        rows, ambiguous = {}, set()
        for window in windows:
            for title_node in self.controls(window, "Text"):
                game = by_title.get(normalized(title_node.window_text()))
                if not game or game.id in ambiguous:
                    continue
                row = title_node.parent()
                for _ in range(3):
                    if row == window or row is None:
                        break
                    titles = self.controls(row, "Text")
                    if any(by_title.get(normalized(node.window_text())) not in (None, game) for node in titles):
                        break
                    buttons = [node for node in self.controls(row, "Button") if
                               normalized(node.window_text()) in UPDATE_BUTTONS | PLAY_BUTTONS]
                    use = list({tuple(node.element_info.runtime_id or ()): node for node in buttons}.values())
                    labels = [normalized(node.window_text()) for node in titles]
                    busy = next((next_busy for text in labels if (next_busy := BUSY_TEXT.match(text))), None)
                    if len(use) == 1 or (not use and busy):
                        button = use[0] if use else None
                        key = (window.handle, tuple(row.element_info.runtime_id or ()))
                        if game.id in rows and rows[game.id][0] != key:
                            ambiguous.add(game.id)
                            del rows[game.id]
                        else:
                            rows[game.id] = (key, window, button, busy.group(1) if busy else None)
                        break
                    row = row.parent()
        return rows

    def invoke_update(self, window, button, safety):
        safety()
        if (button is None or not self.owns_window(window)
                or not button.is_enabled() or normalized(button.window_text()) not in UPDATE_BUTTONS
                or self.challenge(self.windows())):
            return False
        button.iface_invoke.Invoke()
        return True

    def request_close(self, window, safety):
        safety()
        if not self.owns_window(window):
            return
        import win32con
        import win32gui

        win32gui.PostMessage(window.handle, win32con.WM_CLOSE, 0, 0)
