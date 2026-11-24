import argparse
import base64
import getpass
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import time

from .database import Database
from .security import PASSWORDS, Vault


def create_key(path, rotate):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if rotate:
        Vault.load(path)
        content = json.loads(path.read_text())
    else:
        content = {"keys": {}}
    key_id = secrets.token_hex(8)
    content["active"] = key_id
    content["keys"][key_id] = base64.b64encode(secrets.token_bytes(32)).decode()
    if not rotate:
        with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
            json.dump(content, output)
        return
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        json.dump(content, output)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def set_admin(database, name):
    name = name.strip()
    if not 1 <= len(name) <= 80:
        raise SystemExit("Administrator name must have 1–80 characters.")
    password = getpass.getpass("Administrator password: ")
    if not 14 <= len(password) <= 512 or password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords must match and have 14–512 characters.")
    password_hash = PASSWORDS.hash(password)
    with database.transaction(write=True) as connection:
        connection.execute("INSERT OR REPLACE INTO admin VALUES (1,?,?)", (name, password_hash))
        connection.execute("DELETE FROM sessions")


def reencrypt(database, vault):
    with database.transaction(write=True) as connection:
        for row in connection.execute("SELECT * FROM accounts WHERE key_id<>?", (vault.active,)).fetchall():
            credentials = vault.decrypt(row)
            key_id, nonce, ciphertext = vault.encrypt(row["launcher"], **credentials)
            connection.execute(
                "UPDATE accounts SET key_id=?,nonce=?,ciphertext=? WHERE launcher=?",
                (key_id, nonce, ciphertext, row["launcher"]),
            )


def check_copy(connection, vault):
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("SQLite integrity check failed.")
    for row in connection.execute("SELECT * FROM accounts"):
        vault.decrypt(row)


def copy_database(source, target, vault, restore=False):
    if not source.is_file() or source.resolve() == target.resolve():
        raise SystemExit("Source must exist and differ from target.")
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=".gup-copy-")
    os.close(fd)
    temporary = Path(name)
    try:
        source_connection = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        destination = sqlite3.connect(temporary)
        destination.row_factory = sqlite3.Row
        try:
            source_connection.backup(destination)
            check_copy(destination, vault)
            if restore:
                with destination:
                    for table in ("sessions", "enrollments", "leases", "rate"):
                        destination.execute(f"DELETE FROM {table}")
                    destination.execute("UPDATE devices SET revoked=? WHERE revoked IS NULL", (int(time.time()),))
        finally:
            source_connection.close()
            destination.close()
        if restore:
            if any(Path(str(target) + sidecar).exists() for sidecar in ("-wal", "-shm")):
                raise SystemExit("Active SQLite sidecars found. Stop the service and checkpoint before restore.")
            temporary.replace(target)
        else:
            os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=os.environ.get("GUP_DATABASE", "/data/gup.sqlite3"))
    parser.add_argument("--keys", type=Path, default=os.environ.get("GUP_KEYS_FILE", "/run/secrets/gup_keys"))
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("init-keys", "new-key", "reencrypt"):
        commands.add_parser(command)
    commands.add_parser("admin").add_argument("name")
    commands.add_parser("backup").add_argument("file", type=Path)
    commands.add_parser("restore").add_argument("file", type=Path)
    args = parser.parse_args()
    if args.command in ("init-keys", "new-key"):
        create_key(args.keys, args.command == "new-key")
        return
    database = Database(args.database)
    if args.command == "admin":
        database.initialize()
        set_admin(database, args.name)
    else:
        vault = Vault.load(args.keys)
        if args.command == "reencrypt":
            reencrypt(database, vault)
        elif args.command == "backup":
            copy_database(args.database, args.file, vault)
        else:
            copy_database(args.file, args.database, vault, restore=True)


if __name__ == "__main__":
    main()
