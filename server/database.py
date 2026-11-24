from contextlib import contextmanager
from pathlib import Path
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS admin (
    id INTEGER PRIMARY KEY CHECK (id = 1), name TEXT NOT NULL, password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    launcher TEXT PRIMARY KEY, key_id TEXT NOT NULL, nonce BLOB NOT NULL,
    ciphertext BLOB NOT NULL, changed INTEGER NOT NULL, UNIQUE(key_id, nonce)
);
CREATE TABLE IF NOT EXISTS local_sessions (
    launcher TEXT PRIMARY KEY, label TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider IN ('official', 'steam')), changed INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS linked_accounts (
    launcher TEXT PRIMARY KEY, subject TEXT NOT NULL, changed INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS account_link_requests (
    token TEXT PRIMARY KEY, launcher TEXT NOT NULL, expires INTEGER NOT NULL,
    session TEXT NOT NULL REFERENCES sessions(token) ON DELETE CASCADE,
    verifier TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
    allowed TEXT NOT NULL, created INTEGER NOT NULL, seen INTEGER NOT NULL,
    version TEXT NOT NULL, revoked INTEGER
);
CREATE TABLE IF NOT EXISTS enrollments (
    token TEXT PRIMARY KEY, allowed TEXT NOT NULL, expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS leases (
    launcher TEXT PRIMARY KEY REFERENCES accounts(launcher) ON DELETE CASCADE,
    device TEXT NOT NULL REFERENCES devices(id), token TEXT NOT NULL,
    expires INTEGER NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, at INTEGER NOT NULL, action TEXT NOT NULL,
    device TEXT, launcher TEXT, updated INTEGER NOT NULL DEFAULT 0,
    current INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rate (
    scope TEXT NOT NULL, subject TEXT NOT NULL, expires INTEGER NOT NULL,
    hits INTEGER NOT NULL, PRIMARY KEY(scope, subject)
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def initialize(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, write=False):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def event(connection, now, action, *, device=None, launcher=None, updated=0, current=0):
    connection.execute(
        "INSERT INTO events (at,action,device,launcher,updated,current) VALUES (?,?,?,?,?,?)",
        (now, action, device, launcher, updated, current),
    )
