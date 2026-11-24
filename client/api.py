from contextlib import contextmanager
from dataclasses import dataclass, field
import re
import time

import httpx


VERSION = "0.1.3"
PREFIX = "/api/v1/"
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


class ServerUnavailable(Exception):
    pass


class ServerRejected(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(f"HTTP {status}")


@dataclass(frozen=True)
class LauncherConfig:
    passwords: frozenset[str] = frozenset()
    local_sessions: dict[str, str] = field(default_factory=dict)


def normalize_server(value):
    try:
        url = httpx.URL(value.strip())
    except (httpx.InvalidURL, ValueError):
        raise ValueError("Invalid HTTPS address") from None
    if (
        url.scheme != "https" or not url.host or url.userinfo
        or url.path not in ("/", "") or url.query or url.fragment
    ):
        raise ValueError("Enter an HTTPS server address without a path")
    return str(url).rstrip("/")


class API:
    def __init__(self, server, token=None, transport=None):
        self.http = httpx.Client(
            base_url=normalize_server(server), headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=httpx.Timeout(8, connect=4), follow_redirects=False, trust_env=False, transport=transport,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.http.close()

    def request(self, method, path, body=None):
        try:
            response = self.http.request(method, PREFIX + path, json=body)
        except httpx.TransportError as exc:
            # Certificate errors must not be treated as ordinary offline operation.
            import ssl
            cause = exc
            while cause is not None:
                if isinstance(cause, ssl.SSLError):
                    raise ServerRejected(495) from None
                cause = cause.__cause__
            raise ServerUnavailable() from None
        if not response.is_success:
            raise ServerRejected(response.status_code)
        try:
            result = response.json()
            if isinstance(result, dict):
                return result
        except ValueError:
            pass
        raise ServerRejected(502)

    def register(self, name, key):
        result = self.request("POST", "devices/register", {"name": name, "key": key, "version": VERSION})
        if not isinstance(result.get("token"), str) or not TOKEN.fullmatch(result["token"]):
            raise ServerRejected(502)
        if not isinstance(result.get("device_id"), str):
            raise ServerRejected(502)
        return result

    def heartbeat(self):
        return self.request("POST", "devices/heartbeat", {"version": VERSION})

    def configured(self):
        result = self.request("GET", "launchers/config")
        passwords = result.get("launchers")
        local = result.get("local_sessions", {})
        if not isinstance(passwords, list) or not all(isinstance(item, str) for item in passwords):
            raise ServerRejected(502)
        if not isinstance(local, dict) or any(provider not in ("official", "steam") for provider in local.values()):
            raise ServerRejected(502)
        return LauncherConfig(frozenset(passwords).difference(local), local)

    def report(self, launcher, status, updated=0, current=0):
        self.request("POST", "updates/report", {
            "launcher": launcher, "status": status, "updated": updated, "current": current,
        })

    @contextmanager
    def lease(self, launcher):
        result = self.request("POST", "auth/lease", {"launcher": launcher})
        lease = Lease(self, launcher, result)
        try:
            yield lease
        finally:
            if not lease.defer_release:
                try:
                    self.request("POST", "auth/release", lease.body)
                except (ServerRejected, ServerUnavailable):
                    pass


class Lease:
    def __init__(self, api, launcher, result):
        self.api = api
        self.defer_release = False
        token = result.get("lease")
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise ServerRejected(502)
        self.body = {"launcher": launcher, "lease": token}
        self.schedule(result)

    def schedule(self, result):
        lifetime = result.get("expires_in")
        if not isinstance(lifetime, int) or lifetime < 10:
            raise ServerRejected(502)
        self.renew_at = time.monotonic() + min(60, lifetime / 3)

    def confirm(self):
        self.schedule(self.api.request("POST", "auth/renew", self.body))

    def renew_if_due(self):
        if time.monotonic() >= self.renew_at:
            self.confirm()

    def credentials(self):
        result = self.api.request("POST", "auth/credentials", self.body)
        username, password = result.get("username"), result.get("password")
        if not all(isinstance(item, str) and item for item in (username, password)):
            raise ServerRejected(502)
        return username, password
