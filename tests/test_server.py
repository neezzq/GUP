import re
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest

from server.database import Database
from server.main import COOKIE, create_app
from server.manage import copy_database, create_key, reencrypt
from server.security import PASSWORDS, Vault, digest


ADMIN = "operator"
ADMIN_PASSWORD = "test only: administrator password"
ACCOUNT_PASSWORD = "test only: game credential"
VERSION = "0.1.0"


def field(page, name):
    return re.search(f'name="{name}" value="([^"]+)"', page.text).group(1)


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "data.sqlite3"
    keys = tmp_path / "keys.json"
    create_key(keys, False)
    db = Database(path)
    with TestClient(create_app(path, keys), base_url="https://updates.example.test") as client:
        with db.transaction(write=True) as connection:
            connection.execute("INSERT INTO admin VALUES (1,?,?)", (ADMIN, PASSWORDS.hash(ADMIN_PASSWORD)))
        yield client, db, keys


def admin_login(client):
    response = client.get("/admin/login")
    token = field(response, "form_token")
    assert client.post("/admin/login", data={
        "name": ADMIN, "password": ADMIN_PASSWORD, "form_token": token,
    }, follow_redirects=False).status_code == 303
    return field(client.get("/admin/accounts"), "form_token")


def enroll(client, csrf, *launchers):
    result = client.post("/admin/devices/enrollment", data={"form_token": csrf, "launchers": launchers})
    assert result.status_code == 200
    return re.search(r'<input readonly value="([A-Za-z0-9_-]{43})">', result.text).group(1)


def register(client, key, name="PC-1"):
    return client.post("/api/v1/devices/register", json={"name": name, "key": key, "version": VERSION})


def account(client, csrf):
    return client.post("/admin/accounts/steam", data={
        "form_token": csrf, "action": "save", "username": "test@example.test", "password": ACCOUNT_PASSWORD,
    }, follow_redirects=False)


def device_headers(registration):
    return {"Authorization": "Bearer " + registration["token"]}


def api(client, endpoint, device, body=None):
    return client.post("/api/v1/" + endpoint, headers=device_headers(device), json=body)


def test_registration_is_one_use_hash_only_and_revocable(service):
    client, db, _ = service
    assert client.get("/api/v1/health").json() == {"status": "ok"}
    csrf = admin_login(client)
    key = enroll(client, csrf, "steam")
    response = register(client, key)
    assert response.status_code == 201
    device = response.json()
    assert register(client, key).status_code == 401
    assert api(client, "devices/heartbeat", device, {"version": VERSION}).status_code == 200
    with db.transaction() as connection:
        row = connection.execute("SELECT * FROM devices").fetchone()
        assert row["token"] == digest(device["token"])
        assert row["seen"] >= row["created"]
    # Verify that a valid API token alone cannot operate the panel.
    client.cookies.delete(COOKIE)
    assert client.post("/admin/accounts/steam", headers=device_headers(device), data={
        "action": "delete", "form_token": csrf,
    }).status_code == 403
    csrf = admin_login(client)
    response = client.post(f"/admin/devices/{device['device_id']}/revoke",
                           data={"form_token": csrf}, follow_redirects=False)
    assert response.status_code == 303
    assert api(client, "devices/heartbeat", device, {"version": VERSION}).status_code == 401
    assert api(client, "auth/lease", device, {"launcher": "steam"}).status_code == 401


def test_expiry_and_parallel_enrollment(service):
    client, db, _ = service
    csrf = admin_login(client)
    expired = enroll(client, csrf)
    with db.transaction(write=True) as connection:
        connection.execute("UPDATE enrollments SET expires=0 WHERE token=?", (digest(expired),))
    assert register(client, expired).status_code == 401
    key = enroll(client, csrf)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda name: register(client, key, name).status_code, ("one", "two")))
    assert sorted(results) == [201, 401]


def test_vault_and_lease_permissions(service, caplog):
    client, db, keys = service
    csrf = admin_login(client)
    assert account(client, csrf).status_code == 303
    page = client.get("/admin/accounts")
    assert "test@example.test" in page.text
    assert ACCOUNT_PASSWORD not in page.text
    a = register(client, enroll(client, csrf, "steam")).json()
    b = register(client, enroll(client, csrf, "steam")).json()
    c = register(client, enroll(client, csrf, "epic")).json()
    assert client.get("/api/v1/launchers/config", headers=device_headers(a)).json() == {"launchers": ["steam"]}
    assert client.get("/api/v1/launchers/config").status_code == 401
    assert client.get("/api/v1/launchers/config", headers=device_headers(c)).json() == {"launchers": []}
    assert api(client, "auth/lease", c, {"launcher": "steam"}).status_code == 403
    response = api(client, "auth/lease", a, {"launcher": "steam"})
    assert response.status_code == 200
    access = {"launcher": "steam", "lease": response.json()["lease"]}
    assert api(client, "auth/lease", b, {"launcher": "steam"}).status_code == 409
    assert api(client, "auth/credentials", b, access).status_code == 409
    assert api(client, "auth/credentials", a, access).json() == {
        "username": "test@example.test", "password": ACCOUNT_PASSWORD,
    }
    assert api(client, "auth/credentials", a, access).status_code == 409
    assert api(client, "auth/renew", a, access).status_code == 200
    assert api(client, "auth/release", a, access).status_code == 200
    assert api(client, "auth/lease", b, {"launcher": "steam"}).status_code == 200
    with db.transaction(write=True) as connection:
        row = connection.execute("SELECT * FROM accounts").fetchone()
        assert ACCOUNT_PASSWORD.encode() not in row["ciphertext"]
        assert Vault.load(keys).decrypt(row)["password"] == ACCOUNT_PASSWORD
        connection.execute("UPDATE leases SET expires=0")
    assert api(client, "auth/lease", a, {"launcher": "steam"}).status_code == 200
    journal = client.get("/admin/journal")
    for value in (ACCOUNT_PASSWORD, a["token"], b["token"], access["lease"]):
        assert value not in journal.text and value not in caplog.text


def test_csrf_secure_cookies_and_redaction(service):
    client, _, _ = service
    result = client.post("/admin/login", data={"name": ADMIN, "password": ADMIN_PASSWORD, "form_token": "x" * 43})
    assert result.status_code == 403
    csrf = admin_login(client)
    result = client.post("/admin/accounts/steam", data={
        "action": "save", "username": "test", "password": ACCOUNT_PASSWORD, "form_token": "x" * len(csrf),
    })
    assert result.status_code == 403
    result = client.post("/api/v1/devices/register", json={"key": ACCOUNT_PASSWORD})
    assert result.status_code == 422 and ACCOUNT_PASSWORD not in result.text
    result = client.post("/admin/logout", data={"form_token": csrf}, follow_redirects=False)
    assert result.status_code == 303
    assert "Secure" in result.headers["Set-Cookie"]
    assert client.get("/admin/journal", follow_redirects=False).status_code == 303


def test_rotate_and_restore_disables_credentials(service, tmp_path):
    client, db, keys = service
    csrf = admin_login(client)
    assert account(client, csrf).status_code == 303
    device = register(client, enroll(client, csrf, "steam")).json()
    old_id = Vault.load(keys).active
    create_key(keys, True)
    vault = Vault.load(keys)
    assert vault.active != old_id
    reencrypt(db, vault)
    with db.transaction() as connection:
        row = connection.execute("SELECT * FROM accounts").fetchone()
        assert row["key_id"] == vault.active
        assert vault.decrypt(row)["password"] == ACCOUNT_PASSWORD
    backup, restored = tmp_path / "backup.sqlite", tmp_path / "restored.sqlite"
    copy_database(db.path, backup, vault)
    copy_database(backup, restored, vault, restore=True)
    with TestClient(create_app(restored, keys), base_url="https://updates.example.test") as reader:
        assert api(reader, "devices/heartbeat", device, {"version": VERSION}).status_code == 401
        assert reader.get("/admin/accounts", follow_redirects=False).status_code == 303
        admin_login(reader)
        assert "test@example.test" in reader.get("/admin/accounts").text


def test_rotate_device_token_replaces_active_token_and_lease(service):
    client, _, _ = service
    csrf = admin_login(client)
    assert account(client, csrf).status_code == 303
    old = register(client, enroll(client, csrf, "steam")).json()
    assert api(client, "auth/lease", old, {"launcher": "steam"}).status_code == 200
    response = api(client, "devices/token/rotate", old)
    assert response.status_code == 200
    new = response.json()
    assert new["device_id"] == old["device_id"] and new["token"] != old["token"]
    assert api(client, "devices/heartbeat", old, {"version": VERSION}).status_code == 401
    assert api(client, "devices/heartbeat", new, {"version": VERSION}).status_code == 200
    assert api(client, "auth/lease", new, {"launcher": "steam"}).status_code == 200
