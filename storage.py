# -*- coding: utf-8 -*-
"""Слой хранения данных (SQLite). Программа и тренировки хранятся как JSON —
этого достаточно для MVP и легко расширяется позже без миграций схемы."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = "trainer_bot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    created_at TEXT NOT NULL,
    last_seen_version TEXT
);

CREATE TABLE IF NOT EXISTS programs (
    user_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    data TEXT NOT NULL,       -- JSON: {"name":..., "days":[...]}
    raw_text TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    day_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    data TEXT,                -- JSON: список выполненных упражнений с подходами
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS user_exercises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, name),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    user_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL,      -- 'idle' | 'awaiting_program' | 'choosing_day' | 'logging'
    data TEXT NOT NULL,       -- JSON произвольные данные текущей сессии
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn):
        """Добавляем колонки, которых нет в уже существующей базе, — чтобы
        обновление бота не требовало сносить данные пользователей."""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "last_seen_version" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN last_seen_version TEXT")

    # ---- users ----
    def ensure_user(self, user_id: int, username: str | None) -> bool:
        """Возвращает True, если пользователь появился впервые — новичку
        не нужно показывать список прошлых обновлений."""
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            conn.execute(
                "INSERT INTO users (user_id, username, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
                (user_id, username, now_iso()),
            )
        return existing is None

    def get_seen_version(self, user_id: int) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT last_seen_version FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return row["last_seen_version"] if row else None

    def set_seen_version(self, user_id: int, version: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET last_seen_version = ? WHERE user_id = ?",
                (version, user_id),
            )

    # ---- programs ----
    def save_program(self, user_id: int, program: dict, raw_text: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO programs (user_id, name, data, raw_text, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "name=excluded.name, data=excluded.data, raw_text=excluded.raw_text, "
                "updated_at=excluded.updated_at",
                (user_id, program["name"], json.dumps(program, ensure_ascii=False), raw_text, now_iso()),
            )

    def get_program(self, user_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data FROM programs WHERE user_id = ?", (user_id,)
            ).fetchone()
        return json.loads(row["data"]) if row else None

    # ---- workouts ----
    def start_workout(self, user_id: int, day_name: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO workouts (user_id, day_name, started_at, data) VALUES (?, ?, ?, ?)",
                (user_id, day_name, now_iso(), json.dumps([])),
            )
            return cur.lastrowid

    def finish_workout(self, workout_id: int, exercises_log: list):
        with self._conn() as conn:
            conn.execute(
                "UPDATE workouts SET finished_at = ?, data = ? WHERE id = ?",
                (now_iso(), json.dumps(exercises_log, ensure_ascii=False), workout_id),
            )

    def get_workout(self, workout_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM workouts WHERE id = ?", (workout_id,)).fetchone()
        return dict(row) if row else None

    def get_history(self, user_id: int, limit: int = 10) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM workouts WHERE user_id = ? AND finished_at IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- личный список упражнений пользователя ----
    def add_user_exercise(self, user_id: int, name: str) -> bool:
        """Возвращает False, если такое упражнение уже есть в списке."""
        name = name.strip()
        if not name:
            return False
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO user_exercises (user_id, name, created_at) "
                "VALUES (?, ?, ?)",
                (user_id, name, now_iso()),
            )
            return cur.rowcount > 0

    def list_user_exercises(self, user_id: int) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT name FROM user_exercises WHERE user_id = ? ORDER BY id",
                (user_id,),
            ).fetchall()
        return [r["name"] for r in rows]

    def delete_user_exercise(self, user_id: int, name: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM user_exercises WHERE user_id = ? AND name = ?",
                (user_id, name),
            )
            return cur.rowcount > 0

    def has_user_exercise(self, user_id: int, name: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM user_exercises WHERE user_id = ? AND name = ?",
                (user_id, name.strip()),
            ).fetchone()
        return row is not None

    # ---- sessions (текущее состояние диалога с пользователем) ----
    def get_session(self, user_id: int) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT state, data FROM sessions WHERE user_id = ?", (user_id,)
            ).fetchone()
        if not row:
            return {"state": "idle", "data": {}}
        return {"state": row["state"], "data": json.loads(row["data"])}

    def set_session(self, user_id: int, state: str, data: dict):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sessions (user_id, state, data) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, data=excluded.data",
                (user_id, state, json.dumps(data, ensure_ascii=False)),
            )

    def clear_session(self, user_id: int):
        self.set_session(user_id, "idle", {})
