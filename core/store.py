"""
SQLite-хранилище. Четыре таблицы:
  chat_map     — внешний chat_id ↔ адаптер + peer
  seen_msg     — дедупликация входящих
  kv           — OAuth-токены Битрикса и прочие пары ключ/значение
  adapter_state — закешированное состояние каждого адаптера (для UI)
"""

import sqlite3
import threading
import time
from pathlib import Path
import os

_DB_PATH = Path(os.environ.get("DB_PATH", "/data/bridge.sqlite3"))
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with _lock, _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS chat_map (
                external_chat_id TEXT PRIMARY KEY,
                adapter          TEXT NOT NULL,
                peer_id          TEXT NOT NULL,
                peer_phone       TEXT,
                peer_name        TEXT,
                created_at       INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seen_msg (
                adapter     TEXT NOT NULL,
                msg_id      TEXT NOT NULL,
                created_at  INTEGER NOT NULL,
                PRIMARY KEY (adapter, msg_id)
            );
            CREATE TABLE IF NOT EXISTS kv (
                k TEXT PRIMARY KEY,
                v TEXT
            );
            CREATE TABLE IF NOT EXISTS adapter_state (
                adapter    TEXT PRIMARY KEY,
                state      TEXT NOT NULL DEFAULT 'unknown',
                updated_at INTEGER NOT NULL
            );
        """)


def remember_chat(adapter: str, peer_id: str,
                  peer_phone: str | None, peer_name: str | None) -> str:
    external = f"{adapter}:{peer_id}"
    with _lock, _conn() as c:
        c.execute(
            """INSERT INTO chat_map
                 (external_chat_id, adapter, peer_id, peer_phone, peer_name, created_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(external_chat_id) DO UPDATE SET
                 peer_phone = COALESCE(excluded.peer_phone, chat_map.peer_phone),
                 peer_name  = COALESCE(excluded.peer_name,  chat_map.peer_name)""",
            (external, adapter, str(peer_id), peer_phone, peer_name, int(time.time())),
        )
    return external


def resolve_chat(external_chat_id: str) -> dict | None:
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM chat_map WHERE external_chat_id = ?", (external_chat_id,)
        ).fetchone()
    return dict(row) if row else None


def already_seen(adapter: str, msg_id: str) -> bool:
    with _lock, _conn() as c:
        try:
            c.execute(
                "INSERT INTO seen_msg (adapter, msg_id, created_at) VALUES (?,?,?)",
                (adapter, str(msg_id), int(time.time())),
            )
            return False
        except sqlite3.IntegrityError:
            return True


def kv_set(k: str, v: str) -> None:
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, v),
        )


def kv_get(k: str) -> str | None:
    with _lock, _conn() as c:
        row = c.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
    return row["v"] if row else None


def set_adapter_state(adapter: str, state: str) -> None:
    with _lock, _conn() as c:
        c.execute(
            """INSERT INTO adapter_state (adapter, state, updated_at)
               VALUES (?,?,?)
               ON CONFLICT(adapter) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at""",
            (adapter, state, int(time.time())),
        )


def get_adapter_states() -> dict[str, str]:
    with _lock, _conn() as c:
        rows = c.execute("SELECT adapter, state FROM adapter_state").fetchall()
    return {r["adapter"]: r["state"] for r in rows}
