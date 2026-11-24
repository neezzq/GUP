from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import time
from threading import Event

from .api import ServerRejected, ServerUnavailable
from .launchers import LAUNCHERS, adapters, scan_launchers
from .launchers.base import Result, UnsafeDesktop


@dataclass(frozen=True)
class Notice:
    status: str
    launcher: str = ""
    game: str = ""
    progress: object = None
    pending: int | None = None
    result: Result | None = None
    position: int = 0
    total: int = 0


class Cancelled(Exception):
    pass


class AccountBusy(Exception):
    pass


class LeaseServerOffline(Exception):
    pass


class Updater:
    def __init__(self, settings, api, emit, launcher_types=None, icon_callback=None):
        self.settings, self.api, self.emit = settings, api, emit
        self.launcher_types = adapters() if launcher_types is None else launcher_types
        self.stopped, self.paused = Event(), Event()
        self.position = self.total = 0
        self.icon_callback = icon_callback

    def publish(self, status, launcher="", game="", progress=None, pending=None, result=None):
        self.emit(Notice(status, launcher, game, progress, pending, result, self.position, self.total))

    def delay(self, seconds):
        if self.stopped.wait(seconds):
            raise Cancelled

    def boundary(self):
        while self.paused.is_set():
            self.delay(.2)
        self.delay(0)

    def connect(self):
        if self.api is None:
            return None
        for attempt in range(4):
            self.boundary()
            try:
                self.api.heartbeat()
                return self.api.configured()
            except ServerRejected as exc:
                if exc.status not in (502, 503, 504):
                    raise
            except ServerUnavailable:
                pass
            if attempt < 3:
                self.delay(1.5 * (attempt + 1))
        return None

    def report(self, launcher, status, result=None):
        if self.api is None:
            return
        try:
            self.api.report(launcher, status, result.updated if result else 0, result.current if result else 0)
        except (ServerRejected, ServerUnavailable):
            pass

    @contextmanager
    def reserve(self, launcher, before_wait):
        deadline = time.monotonic() + 10 * 60
        while True:
            self.boundary()
            lease_manager = self.api.lease(launcher)
            try:
                lease = lease_manager.__enter__()
            except ServerUnavailable:
                raise LeaseServerOffline from None
            except ServerRejected as exc:
                if exc.status in (502, 503, 504):
                    raise LeaseServerOffline from None
                if exc.status != 409:
                    raise
                if time.monotonic() >= deadline:
                    raise AccountBusy from None
                before_wait()
                self.delay(5)
                continue
            try:
                yield lease
            finally:
                lease_manager.__exit__(None, None, None)
            return

    def auth_state(self, launcher, reserve, *, wait=35, can_navigate=False):
        deadline = time.monotonic() + wait
        navigated = False
        while time.monotonic() < deadline:
            self.delay(0)
            launcher.safety()
            if reserve:
                reserve.renew_if_due()
            state = launcher.check_auth()
            if state in ("ready", "login", "challenge", "steam_guard", "manual_login", "external_login"):
                return state
            if can_navigate and state == "unknown" and not navigated and hasattr(launcher, "open_downloads"):
                launcher.open_downloads()
                navigated = True
            self.delay(1)
        return state

    def process(self, launcher, configured, provider=None):
        name = launcher.key

        def before_wait():
            launcher.safety()
            self.publish("busy", name)

        manager = self.reserve(name, before_wait) if configured else nullcontext()
        with manager as lease:
            try:
                result = self.process_reserved(launcher, lease, provider)
            except (Cancelled, MonitoringStopped, ServerRejected, ServerUnavailable):
                if lease:
                    lease.defer_release = True
                raise
            if lease and not result.queue_safe:
                lease.defer_release = True
            return result

    def process_reserved(self, launcher, lease, provider):
        name = launcher.key
        self.publish("launching", name)
        for attempt in range(4):
            self.boundary()
            try:
                launcher.launch()
                break
            except OSError:
                if attempt == 3:
                    raise
                self.delay(2 ** attempt)

        self.publish("checking", name)
        state = self.auth_state(launcher, lease, can_navigate=True)
        manual_reason = {
            "steam": "epic_steam_session",
            "official": "local_session_required",
        }.get(provider)
        if state in ("challenge", "steam_guard"):
            return Result("confirmation_required", state)
        if state == "external_login":
            return Result("login_required", manual_reason or state)
        if state in ("waiting", "unknown"):
            queue, observations = launcher.check_updates()
            if queue in ("challenge", "steam_guard"):
                return Result("confirmation_required", queue)
            if queue == "external_login":
                return Result("login_required", manual_reason or queue)
            active = queue == "active" or any(item.state == "busy" for item in observations.values())
            return Result("unverified", "unknown_session", queue_safe=not active)
        if state == "manual_login" or (state == "login" and lease is None):
            return Result("login_required", manual_reason or "login_required")
        self.boundary()
        if state == "login":
            self.publish("authorizing", name)
            submitted = launcher.login(lease.credentials, lease.confirm)
            state = self.auth_state(launcher, lease, wait=45) if submitted else launcher.check_auth()
            if state in ("challenge", "steam_guard"):
                return Result("confirmation_required", state)
            if state != "ready":
                reason = "external_login" if state == "external_login" else "login_required"
                return Result("login_required", reason)

        self.publish("checking", name)

        def tick(*, unresolved=False, new_work=False):
            self.delay(0)
            try:
                launcher.safety()
                if lease:
                    lease.renew_if_due()
            except (UnsafeDesktop, ServerUnavailable, ServerRejected) as exc:
                reason = exc.args[0] if isinstance(exc, UnsafeDesktop) else "lease_lost"
                raise MonitoringStopped(reason, unresolved) from None
            return not self.paused.is_set() if new_work else True

        def progress(status, game, observation, pending):
            self.publish(status, name, game, observation, pending)

        result = launcher.wait_for_completion(tick, progress)
        if result.status in ("current", "updated") and self.settings.get("auto_close", True):
            try:
                launcher.close()
            except (OSError, UnsafeDesktop):
                pass
        return result

    def run(self):
        if not self.settings.get("maintenance"):
            self.publish("finished", result=Result("skipped", "maintenance"))
            return
        try:
            configured = self.connect()
        except ServerRejected:
            self.publish("rejected")
            self.publish("finished", result=Result("error", "device_rejected"))
            return
        except Cancelled:
            self.publish("finished", result=Result("skipped", "stopped"))
            return

        self.publish("offline" if configured is None else "online")
        order = self.settings.get("order", list(LAUNCHERS))
        enabled = self.settings.get("enabled", list(LAUNCHERS))
        selected = list(dict.fromkeys(key for key in order if key in enabled and key in self.launcher_types))
        self.total = len(selected)
        if not selected:
            self.publish("finished", result=Result("finished"))
            return
        paths = self.settings.get("paths", {})
        launchers, initial = {}, {}
        for key, launcher, detection in scan_launchers(paths, self.launcher_types):
            try:
                self.delay(0)
                if launcher is not None:
                    launcher.games = list({game.id: game for game in launcher.discover_games()}.values())
                    launchers[key] = launcher
                    if key in selected and self.icon_callback:
                        self.icon_callback(key, launcher.exe)
                else:
                    initial[key] = Result("skipped" if detection == "not_installed" else "error", detection)
            except Cancelled:
                self.publish("finished", result=Result("skipped", "stopped"))
                return
            except Exception:
                initial[key] = Result("error", "detect_failed")
        protected = [game for launcher in launchers.values() for game in launcher.games]
        for launcher in launchers.values():
            launcher.protected_games = protected

        totals = {"updated": 0, "current": 0}
        can_continue = True
        for self.position, key in enumerate(selected, 1):
            try:
                self.boundary()
                if not can_continue:
                    result = Result("skipped", "previous_active")
                elif key in initial:
                    result = initial[key]
                else:
                    if configured is not None:
                        # Recheck revocation before opening another client.
                        try:
                            self.api.heartbeat()
                        except ServerUnavailable:
                            configured = None
                            self.publish("offline")
                        except ServerRejected as exc:
                            if exc.status not in (502, 503, 504):
                                self.publish("rejected")
                                can_continue = False
                                raise
                            configured = None
                            self.publish("offline")
                    if configured is not None:
                        self.report(key, "started")
                    try:
                        result = self.process(
                            launchers[key], configured is not None and key in configured.passwords,
                            configured.local_sessions.get(key) if configured is not None else None,
                        )
                    except LeaseServerOffline:
                        configured = None
                        self.publish("offline")
                        result = self.process(launchers[key], False)
            except Cancelled:
                self.publish("finished", result=Result("skipped", "stopped", **totals))
                return
            except AccountBusy:
                result = Result("busy", "busy")
            except UnsafeDesktop as exc:
                result = Result("skipped", exc.args[0])
            except MonitoringStopped as exc:
                result = Result("unverified", exc.reason, queue_safe=not exc.unresolved)
            except (ServerUnavailable, ServerRejected):
                result = Result("error", "server_error")
            except Exception:
                # External accessibility errors can include control contents.
                result = Result("error", "automation_error", queue_safe=False)
            can_continue = can_continue and result.queue_safe
            totals["updated"] += result.updated
            totals["current"] += result.current
            if configured is not None:
                self.report(key, result.status, result)
            self.publish(result.status, key, result=result)
        self.publish("finished", result=Result("finished", **totals))


class MonitoringStopped(Exception):
    def __init__(self, reason, unresolved):
        self.reason = reason
        self.unresolved = unresolved
