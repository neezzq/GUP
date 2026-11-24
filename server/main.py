from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hmac
import json
import os
from pathlib import Path
import re
import time
import uuid

from argon2.exceptions import VerificationError
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .account_links import (
    LINK_TTL, OAUTH_PROVIDERS, LinkError, configured_oauth, oauth_authorize,
    public_origin, steam_authorize, verified_identity, vk_authorize,
)
from .database import Database, event
from .models import (
    Heartbeat, LAUNCHER_NAMES, LAUNCHER_SIGN_INS, Launcher, LeaseAccess, LeaseRequest, Registration, Report,
)
from .security import PASSWORDS, Vault, accept_request, digest, random_token


HERE = Path(__file__).resolve().parent
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
LEASE_TTL = 180
ENROLL_TTL = 600
SESSION_TTL = 8 * 3600
ACTIONS = {
    "device_registered": "Компьютер подключён", "device_revoked": "Доступ отозван",
    "device_token_rotated": "Токен устройства заменён",
    "enrollment_created": "Создан ключ подключения", "account_save": "Аккаунт сохранён",
    "account_delete": "Аккаунт удалён", "account_local": "Выбрана сессия на компьютере",
    "account_linked": "Аккаунт подтверждён", "account_unlinked": "Привязка аккаунта удалена",
    "authorization_requested": "Запрошен вход",
    "started": "Начало проверки", "updated": "Установка завершена", "current": "Игры актуальны",
    "error": "Ошибка", "login_required": "Требуется вход",
    "confirmation_required": "Требуется подтверждение", "busy": "Аккаунт занят",
    "skipped": "Пропущено", "unverified": "Результат не подтверждён",
}
COOKIE = "__Host-gup_admin"
LOGIN_COOKIE = "__Host-gup_login"


def create_app(database_path=None, key_path=None, *, public_url=None):
    database = Database(Path(database_path or os.environ.get("GUP_DATABASE", "/data/gup.sqlite3")))
    key_path = Path(key_path or os.environ.get("GUP_KEYS_FILE", "/run/secrets/gup_keys"))
    origin = public_origin(public_url if public_url is not None else os.environ.get("GUP_PUBLIC_ORIGIN"))
    oauth = configured_oauth()
    linkable = ({Launcher.STEAM} | oauth.keys()) if origin else set()
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["utc"] = lambda value: (
        datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if value else "—"
    )

    @asynccontextmanager
    async def lifespan(app):
        database.initialize()
        app.state.vault = Vault.load(key_path)
        with database.transaction() as connection:
            used_keys = {row["key_id"] for row in connection.execute("SELECT DISTINCT key_id FROM accounts")}
        if used_keys - app.state.vault.keys.keys():
            raise RuntimeError("Vault keyring cannot read all accounts")
        app.state.dummy_hash = PASSWORDS.hash(random_token())
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        form_targets = "'self'"
        if request.url.path == "/admin/accounts":
            form_targets += " https://steamcommunity.com https://www.epicgames.com https://auth.riotgames.com"
            form_targets += " https://oauth.battle.net https://account.battle.net https://id.vk.com"
        response.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                f"default-src 'none'; style-src 'self'; form-action {form_targets}; "
                "frame-ancestors 'none'; base-uri 'none'"
            ),
        })
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse({"detail": "Invalid request"}, status_code=422)

    def guard(request, scope, limit, ttl=60):
        now = int(time.time())
        subject = request.client.host if request.client else "unknown"
        if not accept_request(database, scope, subject, now, limit, ttl):
            raise HTTPException(429, "Retry later")
        return now

    def device(connection, authorization):
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not TOKEN_PATTERN.fullmatch(token):
            raise HTTPException(401, "Device authorization required")
        row = connection.execute(
            "SELECT * FROM devices WHERE token=? AND revoked IS NULL", (digest(token),)
        ).fetchone()
        if row is None:
            raise HTTPException(401, "Device authorization required")
        return row

    def allowed(row, launcher):
        if launcher not in json.loads(row["allowed"]):
            raise HTTPException(403, "Launcher not permitted")

    def lease_row(connection, row, lease, now):
        allowed(row, lease.launcher)
        result = connection.execute(
            "SELECT * FROM leases WHERE launcher=? AND device=? AND token=? AND expires>?",
            (lease.launcher, row["id"], digest(lease.lease), now),
        ).fetchone()
        if result is None:
            raise HTTPException(409, "Lease unavailable")
        return result

    @app.get("/api/v1/health")
    def health():
        with database.transaction() as connection:
            connection.execute("SELECT 1 FROM accounts LIMIT 1")
        return {"status": "ok"}

    @app.post("/api/v1/devices/register", status_code=201)
    def register(body: Registration, request: Request):
        now = guard(request, "registration", 30)
        device_id, token = str(uuid.uuid4()), random_token()
        with database.transaction(write=True) as connection:
            enrollment = connection.execute(
                "DELETE FROM enrollments WHERE token=? AND expires>? RETURNING allowed",
                (digest(body.key), now),
            ).fetchone()
            if enrollment is None:
                raise HTTPException(401, "Invalid enrollment key")
            connection.execute(
                "INSERT INTO devices VALUES (?,?,?,?,?,?,?,NULL)",
                (device_id, body.name, digest(token), enrollment["allowed"], now, now, body.version),
            )
            event(connection, now, "device_registered", device=device_id)
        return {"device_id": device_id, "token": token}

    @app.post("/api/v1/devices/heartbeat")
    def heartbeat(body: Heartbeat, request: Request, authorization: str | None = Header(None)):
        now = guard(request, "devices", 600)
        with database.transaction(write=True) as connection:
            row = device(connection, authorization)
            connection.execute(
                "UPDATE devices SET seen=?, version=? WHERE id=?", (now, body.version, row["id"])
            )
        return {"status": "ok"}

    @app.post("/api/v1/devices/token/rotate")
    def rotate_token(request: Request, authorization: str | None = Header(None)):
        now = guard(request, "rotation", 30)
        token = random_token()
        with database.transaction(write=True) as connection:
            row = device(connection, authorization)
            connection.execute("UPDATE devices SET token=?,seen=? WHERE id=?", (digest(token), now, row["id"]))
            connection.execute("DELETE FROM leases WHERE device=?", (row["id"],))
            event(connection, now, "device_token_rotated", device=row["id"])
        return {"device_id": row["id"], "token": token}

    @app.get("/api/v1/launchers/config")
    def config(request: Request, authorization: str | None = Header(None)):
        guard(request, "devices", 600)
        with database.transaction() as connection:
            row = device(connection, authorization)
            permitted = set(json.loads(row["allowed"]))
            configured = {account["launcher"] for account in connection.execute("SELECT launcher FROM accounts")}
            local = {item["launcher"]: item["provider"] for item in connection.execute(
                "SELECT launcher,provider FROM local_sessions"
            ) if item["launcher"] in permitted}
        return {"launchers": [launcher for launcher in Launcher if launcher in permitted & configured],
                "local_sessions": local}

    @app.post("/api/v1/auth/lease")
    def acquire(body: LeaseRequest, request: Request, authorization: str | None = Header(None)):
        now = guard(request, "devices", 600)
        token = random_token()
        with database.transaction(write=True) as connection:
            row = device(connection, authorization)
            allowed(row, body.launcher)
            if connection.execute("SELECT 1 FROM accounts WHERE launcher=?", (body.launcher,)).fetchone() is None:
                raise HTTPException(404, "Account unavailable")
            connection.execute("DELETE FROM leases WHERE launcher=? AND expires<=?", (body.launcher, now))
            result = connection.execute(
                "INSERT OR IGNORE INTO leases (launcher,device,token,expires) VALUES (?,?,?,?)",
                (body.launcher, row["id"], digest(token), now + LEASE_TTL),
            )
            if not result.rowcount:
                raise HTTPException(409, "Account busy", headers={"Retry-After": "5"})
            event(connection, now, "authorization_requested", device=row["id"], launcher=body.launcher)
        return {"lease": token, "expires_in": LEASE_TTL}

    @app.post("/api/v1/auth/renew")
    def renew(body: LeaseAccess, request: Request, authorization: str | None = Header(None)):
        now = guard(request, "devices", 600)
        with database.transaction(write=True) as connection:
            row = device(connection, authorization)
            lease_row(connection, row, body, now)
            connection.execute("UPDATE leases SET expires=? WHERE launcher=?", (now + LEASE_TTL, body.launcher))
        return {"expires_in": LEASE_TTL}

    @app.post("/api/v1/auth/credentials")
    def credentials(body: LeaseAccess, request: Request, authorization: str | None = Header(None)):
        now = guard(request, "devices", 600)
        with database.transaction(write=True) as connection:
            row = device(connection, authorization)
            lease = lease_row(connection, row, body, now)
            if lease["delivered"]:
                raise HTTPException(409, "Credentials already delivered")
            account = connection.execute("SELECT * FROM accounts WHERE launcher=?", (body.launcher,)).fetchone()
            if account is None:
                raise HTTPException(404, "Account unavailable")
            result = app.state.vault.decrypt(account)
            connection.execute("UPDATE leases SET delivered=1 WHERE launcher=?", (body.launcher,))
        return result

    @app.post("/api/v1/auth/release")
    def release(body: LeaseAccess, request: Request, authorization: str | None = Header(None)):
        now = guard(request, "devices", 600)
        with database.transaction(write=True) as connection:
            row = device(connection, authorization)
            lease_row(connection, row, body, now)
            connection.execute("DELETE FROM leases WHERE launcher=?", (body.launcher,))
        return {"status": "ok"}

    @app.post("/api/v1/updates/report")
    def report(body: Report, request: Request, authorization: str | None = Header(None)):
        now = guard(request, "devices", 600)
        with database.transaction(write=True) as connection:
            row = device(connection, authorization)
            event(connection, now, body.status, device=row["id"], launcher=body.launcher,
                  updated=body.updated, current=body.current)
        return {"status": "ok"}

    def admin(connection, request, now):
        cookie = request.cookies.get(COOKIE, "")
        if TOKEN_PATTERN.fullmatch(cookie):
            row = connection.execute(
                "SELECT * FROM sessions WHERE token=? AND expires>?", (digest(cookie), now),
            ).fetchone()
            if row:
                return row
        raise HTTPException(403, "Administrator session required")

    def csrf(expected, actual):
        if not (TOKEN_PATTERN.fullmatch(expected) and TOKEN_PATTERN.fullmatch(actual)
                and hmac.compare_digest(expected, actual)):
            raise HTTPException(403, "Invalid form token")

    def render(request, view, **context):
        return templates.TemplateResponse(request=request, name=view, context=context)

    def panel(connection, request, section, session, enrollment=None):
        accounts, devices, events = {}, [], []
        local_sessions = {}
        linked = {}
        if section == "accounts":
            linked = {row["launcher"]: row["subject"] for row in connection.execute("SELECT * FROM linked_accounts")}
            local_sessions = {row["launcher"]: row for row in connection.execute("SELECT * FROM local_sessions")}
            for row in connection.execute("SELECT * FROM accounts"):
                accounts[row["launcher"]] = {
                    "changed": row["changed"],
                    "username": app.state.vault.decrypt(row)["username"],
                }
        elif section == "devices":
            devices = connection.execute(
                "SELECT id,name,created,seen,version,revoked FROM devices ORDER BY created DESC"
            ).fetchall()
        elif section == "journal":
            events = connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT 200").fetchall()
        return render(request, "panel.html", section=section, csrf=session["csrf"], names=LAUNCHER_NAMES,
                      accounts=accounts, local_sessions=local_sessions, devices=devices, events=events,
                      enrollment=enrollment, actions=ACTIONS, sign_ins=LAUNCHER_SIGN_INS,
                      linked=linked, linkable=linkable, oauth_providers=OAUTH_PROVIDERS.keys() | {Launcher.VKPLAY})

    @app.get("/")
    def index():
        return RedirectResponse("/admin/accounts", status_code=303)

    @app.get("/admin/login")
    def login_form(request: Request):
        guard(request, "login_form", 60)
        token = random_token()
        response = render(request, "login.html", csrf=token, failed=False)
        response.set_cookie(LOGIN_COOKIE, token, max_age=600, secure=True, httponly=True, samesite="strict")
        return response

    @app.post("/admin/login")
    def login(request: Request, name: str = Form(max_length=80), password: str = Form(max_length=512),
              form_token: str = Form(max_length=100)):
        now = guard(request, "login", 8, 300)
        csrf(request.cookies.get(LOGIN_COOKIE, ""), form_token)
        if not accept_request(database, "administrator", "all", now, 30, 300):
            raise HTTPException(429, "Retry later")
        with database.transaction() as connection:
            row = connection.execute("SELECT * FROM admin WHERE id=1").fetchone()
        valid = False
        try:
            valid = PASSWORDS.verify(row["password_hash"] if row else app.state.dummy_hash, password)
        except VerificationError:
            pass
        if not valid or row is None or row["name"] != name:
            response = render(request, "login.html", csrf=form_token, failed=True)
            response.status_code = 401
            return response
        token = random_token()
        with database.transaction(write=True) as connection:
            current = connection.execute("SELECT password_hash FROM admin WHERE id=1").fetchone()
            if current is None or current["password_hash"] != row["password_hash"]:
                raise HTTPException(403, "Sign in again")
            connection.execute("DELETE FROM sessions WHERE expires<=? OR token=?",
                               (now, digest(request.cookies.get(COOKIE, ""))))
            connection.execute("INSERT INTO sessions VALUES (?,?,?)", (digest(token), random_token(), now + SESSION_TTL))
        response = RedirectResponse("/admin/accounts", status_code=303)
        response.set_cookie(COOKIE, token, max_age=SESSION_TTL, secure=True, httponly=True, samesite="strict")
        response.delete_cookie(LOGIN_COOKIE, secure=True, httponly=True, samesite="strict")
        return response

    @app.post("/admin/logout")
    def logout(request: Request, form_token: str = Form(max_length=100)):
        now = guard(request, "panel", 120)
        with database.transaction(write=True) as connection:
            session = admin(connection, request, now)
            csrf(session["csrf"], form_token)
            connection.execute("DELETE FROM sessions WHERE token=?", (session["token"],))
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict")
        return response

    @app.get("/admin/{section}")
    def admin_panel(section: str, request: Request):
        if section not in ("accounts", "devices", "journal"):
            raise HTTPException(404)
        now = guard(request, "panel", 120)
        with database.transaction() as connection:
            try:
                session = admin(connection, request, now)
            except HTTPException:
                return RedirectResponse("/admin/login", status_code=303)
            return panel(connection, request, section, session)

    @app.post("/admin/accounts/{launcher}/link")
    def link_account(launcher: Launcher, request: Request, form_token: str = Form(max_length=100)):
        now = guard(request, "link_start", 30)
        state = random_token()
        verifier = random_token() if launcher == Launcher.VKPLAY else ""
        with database.transaction(write=True) as connection:
            session = admin(connection, request, now)
            csrf(session["csrf"], form_token)
            if launcher not in linkable:
                raise HTTPException(404, "Account linking is unavailable")
            connection.execute(
                "DELETE FROM account_link_requests WHERE expires<=? OR (launcher=? AND session=?)",
                (now, launcher, session["token"]),
            )
            connection.execute(
                "INSERT INTO account_link_requests (token,launcher,expires,session,verifier) VALUES (?,?,?,?,?)",
                (digest(state), launcher, now + LINK_TTL, session["token"], verifier),
            )
        callback_url = f"{origin}/admin/accounts/{launcher}/callback"
        if launcher == Launcher.STEAM:
            target = steam_authorize(origin, callback_url, state)
        elif launcher == Launcher.VKPLAY:
            target = vk_authorize(oauth[launcher][0], callback_url, state, verifier)
        else:
            target = oauth_authorize(launcher, oauth[launcher][0], callback_url, state)
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            f"__Host-gup_link_{launcher}", state, max_age=LINK_TTL,
            secure=True, httponly=True, samesite="lax",
        )
        return response

    @app.get("/admin/accounts/{launcher}/callback")
    def account_callback(launcher: Launcher, request: Request):
        now = guard(request, "link_callback", 30)
        cookie_name = f"__Host-gup_link_{launcher}"
        query = dict(request.query_params)
        state = query.get("state", "")
        subject = None
        code = 400
        browser_state = request.cookies.get(cookie_name, "")
        matched = (TOKEN_PATTERN.fullmatch(state) and TOKEN_PATTERN.fullmatch(browser_state)
                   and hmac.compare_digest(browser_state, state))
        if (launcher in linkable and matched
                and len(request.scope["query_string"]) <= 8192
                and len(query) == len(request.query_params.multi_items())):
            with database.transaction(write=True) as connection:
                pending = connection.execute(
                    """
                    UPDATE account_link_requests SET used=1
                    WHERE token=? AND launcher=? AND expires>? AND used=0
                    AND EXISTS (SELECT 1 FROM sessions
                                WHERE sessions.token=account_link_requests.session AND sessions.expires>?)
                    RETURNING session, verifier
                    """, (digest(state), launcher, now, now),
                ).fetchone()
            if pending:
                try:
                    candidate = verified_identity(
                        launcher, oauth.get(launcher), query, f"{origin}/admin/accounts/{launcher}/callback",
                        state, now, pending["verifier"],
                    )
                except LinkError:
                    pass
                else:
                    with database.transaction(write=True) as connection:
                        finished = int(time.time())
                        confirmed = connection.execute(
                            """
                            DELETE FROM account_link_requests WHERE token=? AND used=1 AND expires>?
                            AND EXISTS (SELECT 1 FROM sessions
                                        WHERE sessions.token=account_link_requests.session AND sessions.expires>?)
                            """, (digest(state), finished, finished),
                        ).rowcount
                        if confirmed:
                            connection.execute(
                                """
                                INSERT INTO linked_accounts VALUES (?,?,?) ON CONFLICT(launcher) DO UPDATE SET
                                subject=excluded.subject, changed=excluded.changed
                                """, (launcher, candidate, now),
                            )
                            event(connection, now, "account_linked", launcher=launcher)
                            subject, code = candidate, 200
        provider_name = "VK ID" if launcher == Launcher.VKPLAY else LAUNCHER_NAMES[launcher]
        response = render(request, "account_link.html", name=provider_name, subject=subject)
        response.status_code = code
        if matched:
            response.delete_cookie(cookie_name, secure=True, httponly=True, samesite="lax")
        return response

    @app.post("/admin/accounts/{launcher}/unlink")
    def unlink_account(launcher: Launcher, request: Request, form_token: str = Form(max_length=100)):
        now = guard(request, "panel", 120)
        with database.transaction(write=True) as connection:
            session = admin(connection, request, now)
            csrf(session["csrf"], form_token)
            connection.execute("DELETE FROM linked_accounts WHERE launcher=?", (launcher,))
            connection.execute("DELETE FROM account_link_requests WHERE launcher=?", (launcher,))
            event(connection, now, "account_unlinked", launcher=launcher)
        return RedirectResponse("/admin/accounts", status_code=303)

    @app.post("/admin/accounts/{launcher}")
    def account(launcher: Launcher, request: Request, form_token: str = Form(max_length=100),
                action: str = Form(max_length=10), username: str = Form("", max_length=320),
                password: str = Form("", max_length=1024), label: str = Form("", max_length=80),
                provider: str = Form("official", max_length=10), remove_password: bool = Form(False)):
        now = guard(request, "panel", 120)
        with database.transaction(write=True) as connection:
            session = admin(connection, request, now)
            csrf(session["csrf"], form_token)
            if action == "delete":
                connection.execute("DELETE FROM accounts WHERE launcher=?", (launcher,))
                connection.execute("DELETE FROM local_sessions WHERE launcher=?", (launcher,))
            elif action == "local" and label.strip() and (
                provider == "official" or (launcher == Launcher.EPIC and provider == "steam")
            ):
                has_password = connection.execute(
                    "SELECT 1 FROM accounts WHERE launcher=?", (launcher,),
                ).fetchone()
                if has_password and not remove_password:
                    raise HTTPException(409, "Confirm removal of the stored credentials")
                connection.execute("DELETE FROM accounts WHERE launcher=?", (launcher,))
                connection.execute(
                    """
                    INSERT INTO local_sessions VALUES (?,?,?,?) ON CONFLICT(launcher) DO UPDATE SET
                    label=excluded.label, provider=excluded.provider, changed=excluded.changed
                    """, (launcher, label.strip(), provider, now),
                )
            elif action == "save" and username.strip() and password:
                key_id, nonce, ciphertext = app.state.vault.encrypt(launcher, username.strip(), password)
                connection.execute(
                    """
                    INSERT INTO accounts VALUES (?,?,?,?,?) ON CONFLICT(launcher) DO UPDATE SET
                    key_id=excluded.key_id, nonce=excluded.nonce, ciphertext=excluded.ciphertext, changed=excluded.changed
                    """,
                    (launcher, key_id, nonce, ciphertext, now),
                )
                connection.execute("DELETE FROM leases WHERE launcher=?", (launcher,))
                connection.execute("DELETE FROM local_sessions WHERE launcher=?", (launcher,))
            else:
                raise HTTPException(422, "Invalid account operation")
            event(connection, now, f"account_{action}", launcher=launcher)
        return RedirectResponse("/admin/accounts", status_code=303)

    @app.post("/admin/devices/enrollment")
    def enrollment_key(request: Request, form_token: str = Form(max_length=100),
                       launchers: list[Launcher] = Form(default=[])):
        now = guard(request, "panel", 120)
        token = random_token()
        with database.transaction(write=True) as connection:
            session = admin(connection, request, now)
            csrf(session["csrf"], form_token)
            connection.execute("DELETE FROM enrollments WHERE expires<=?", (now,))
            connection.execute(
                "INSERT INTO enrollments VALUES (?,?,?)",
                (digest(token), json.dumps(sorted(set(launchers))), now + ENROLL_TTL),
            )
            event(connection, now, "enrollment_created")
            return panel(connection, request, "devices", session, enrollment=token)

    @app.post("/admin/devices/{device_id}/revoke")
    def revoke(device_id: uuid.UUID, request: Request, form_token: str = Form(max_length=100)):
        now = guard(request, "panel", 120)
        identifier = str(device_id)
        with database.transaction(write=True) as connection:
            session = admin(connection, request, now)
            csrf(session["csrf"], form_token)
            if not connection.execute("UPDATE devices SET revoked=? WHERE id=? AND revoked IS NULL",
                                      (now, identifier)).rowcount:
                raise HTTPException(404, "Active device not found")
            connection.execute("DELETE FROM leases WHERE device=?", (identifier,))
            event(connection, now, "device_revoked", device=identifier)
        return RedirectResponse("/admin/devices", status_code=303)

    return app


app = create_app()
