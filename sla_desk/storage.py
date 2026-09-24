"""SQLite 台账持久化。

台账包含两张表：

- ``windows``：每日服务时段（本地挂钟时间 ``HH:MM``）；
- ``tickets``：工单及其受理时刻与计算出的响应截止时刻。

工单写入在单个事务内完成，任何校验失败都不会留下半条记录。
"""

import datetime as _dt
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import sla

SCHEMA = """
CREATE TABLE IF NOT EXISTS windows (
    id      TEXT PRIMARY KEY,
    start   TEXT NOT NULL,
    "end"   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    id          TEXT PRIMARY KEY,
    priority    TEXT NOT NULL,
    window_id   TEXT NOT NULL REFERENCES windows(id),
    created_at  TEXT NOT NULL,
    deadline    TEXT NOT NULL
);
"""


class LedgerError(Exception):
    """台账操作失败（重复 id、外键缺失等）。"""


class NotFound(LedgerError):
    """查询的记录不存在。"""


class DuplicateRecord(LedgerError):
    """id 已存在；退出码按 2 处理。"""


@contextmanager
def connect(
    db_path: str | Path, *, create_ok: bool = True
) -> Iterator[sqlite3.Connection]:
    """打开台账，成功时提交，异常时回滚。

    文件不存在时默认引导建库（用于 window create）；查询类命令应传
    ``create_ok=False``，使失败不留下任何台账文件。
    """
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        raise LedgerError(f"台账目录不存在: {path.parent}")
    if not create_ok and not path.exists():
        raise LedgerError(f"台账文件不存在: {path}")
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_window(
    conn: sqlite3.Connection, window_id: str, start: _dt.time, end: _dt.time
) -> None:
    try:
        conn.execute(
            'INSERT INTO windows (id, start, "end") VALUES (?, ?, ?)',
            (window_id, start.isoformat(), end.isoformat()),
        )
    except sqlite3.IntegrityError as exc:
        raise DuplicateRecord(f"服务时段已存在: {window_id}") from exc


def get_window(
    conn: sqlite3.Connection, window_id: str
) -> tuple[_dt.time, _dt.time]:
    row = conn.execute(
        'SELECT start, "end" FROM windows WHERE id = ?', (window_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"服务时段不存在: {window_id}")
    return _dt.time.fromisoformat(row[0]), _dt.time.fromisoformat(row[1])


def create_ticket(
    conn: sqlite3.Connection,
    ticket_id: str,
    priority: str,
    window_id: str,
    created_at: _dt.datetime,
    deadline: _dt.datetime,
) -> None:
    try:
        conn.execute(
            "INSERT INTO tickets (id, priority, window_id, created_at, deadline)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                ticket_id,
                priority,
                window_id,
                sla.format_utc(created_at),
                sla.format_utc(deadline),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise DuplicateRecord(f"工单已存在: {ticket_id}") from exc


def _ticket_row_to_dict(row: sqlite3.Row) -> dict[str, str]:
    return {
        "id": row[0],
        "priority": row[1],
        "window_id": row[2],
        "created_at": row[3],
        "deadline": row[4],
    }


def get_ticket(conn: sqlite3.Connection, ticket_id: str) -> dict[str, str]:
    row = conn.execute(
        "SELECT id, priority, window_id, created_at, deadline"
        " FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"工单不存在: {ticket_id}")
    return _ticket_row_to_dict(row)


def list_tickets(conn: sqlite3.Connection) -> list[dict[str, str]]:
    rows = conn.execute(
        "SELECT id, priority, window_id, created_at, deadline"
        " FROM tickets ORDER BY created_at ASC, id ASC"
    ).fetchall()
    return [_ticket_row_to_dict(row) for row in rows]
