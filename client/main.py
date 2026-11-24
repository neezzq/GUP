import json
import sys

from client.launchers import LAUNCHERS
from client.security import data_directory, enter_instance


def settings_at(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    order = value.get("order")
    enabled = value.get("enabled")
    paths = value.get("paths")
    value["order"] = list(dict.fromkeys(
        [key for key in order if isinstance(key, str) and key in LAUNCHERS] + list(LAUNCHERS)
    )) if isinstance(order, list) else list(LAUNCHERS)
    value["enabled"] = [key for key in enabled if isinstance(key, str) and key in LAUNCHERS] if isinstance(enabled, list) else list(LAUNCHERS)
    value["paths"] = {key: item for key, item in paths.items() if key in LAUNCHERS and isinstance(item, str)} if isinstance(paths, dict) else {}
    for name in ("device_id", "server", "name", "language"):
        if name in value and not isinstance(value[name], str):
            del value[name]
    for flag, default in (("maintenance", False), ("auto_start", True), ("auto_close", True)):
        if not isinstance(value.get(flag), bool):
            value[flag] = default
    return value


def main():
    if sys.platform != "win32":
        raise SystemExit("Game Updater requires Windows 10/11 x64.")
    # Pywinauto shares its automation object across sequential queue threads.
    sys.coinit_flags = 0
    import pythoncom

    if "--smoke-test" in sys.argv[1:]:
        from client.launchers import adapters
        from client.security import credential_store
        from client.ui import Window

        from pathlib import Path
        from tempfile import TemporaryDirectory
        import win32cred
        import win32ts

        if "--verify-uia" in sys.argv[1:]:
            from client.automation import desktop_class

            desktop_class()(backend="uia").windows(visible_only=True)
        credential_store()
        assert len(adapters()) == 7
        assert win32cred.CredRead and win32ts.WTSEnumerateSessions
        with TemporaryDirectory(prefix="gup-smoke-") as temporary:
            window = Window(Path(temporary), {})
            window.withdraw()
            window.update_idletasks()
            assert len(window.rows) == 7
            window.close_logging()
            window.destroy()
        return
    mutex = enter_instance()
    if not mutex:
        return
    try:
        from client.ui import Window

        directory = data_directory()
        Window(directory, settings_at(directory / "settings.json")).mainloop()
    finally:
        mutex.Close()


if __name__ == "__main__":
    main()
