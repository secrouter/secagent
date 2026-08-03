"""Database access layer for the API service, backed by SQLite."""

import sqlite3

from common.models import User

DB_PATH = "users.db"


def _conn():
    return sqlite3.connect(DB_PATH)


def get_user(uid: int) -> User:
    cur = _conn().execute("SELECT id, name, email FROM users WHERE id=?", (uid,))
    row = cur.fetchone()
    return User(*row)


def save_user(user: User) -> None:
    _conn().execute(
        "INSERT INTO users VALUES (?,?,?)", (user.id, user.name, user.email)
    )
