"""SQLite-backed persistence for 工单 (tickets).

Every public mutating function runs inside a single ``BEGIN IMMEDIATE``
transaction so that a failing command never leaves a partial record behind.
The database file lives in the current working directory (``sla-desk.db``).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

DB_FILENAME = "sla-desk.db"

VALID_PRIORITIES = ("P1", "P2", "P3", "P4")
STATUS_OPEN = "open"
STATUS_MERGED = "merged"


class TicketError(Exception):
    """A domain-level error reported to the user with a non-zero exit."""


class Ticket(NamedTuple):
    ticket_no: str
    customer_ref: str
    title: str
    reporter_email: str
    priority: str
    submitted_at: str
    status: str
    merged_into: str | None
    merged_at: str | None


class RegisterResult(NamedTuple):
    ticket: Ticket
    duplicate: bool


SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_no TEXT NOT NULL UNIQUE,
    customer_ref TEXT NOT NULL,
    title TEXT NOT NULL,
    reporter_email TEXT NOT NULL,
    priority TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    status TEXT NOT NULL,
    merged_into TEXT,
    merged_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_customer_ref
    ON tickets(customer_ref);
"""


def default_db_path() -> Path:
    return Path.cwd() / DB_FILENAME


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path if db_path is not None else default_db_path()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def parse_submitted_at(value: str) -> datetime:
    """Parse an ISO 8601 timestamp, accepting a trailing ``Z`` and offsets."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00", 1))
    except (ValueError, AttributeError):
        raise TicketError(f"提交时间无法解析: {value!r}")


def validate_ticket_fields(
    title: str, reporter_email: str, priority: str, submitted_at: str
) -> list[str]:
    """Return human-readable complaints, one per invalid field; empty = valid."""
    problems: list[str] = []
    if not title.strip():
        problems.append("标题不能为空")
    if "@" not in reporter_email:
        problems.append(f"报障人邮箱必须包含 @: {reporter_email!r}")
    if priority not in VALID_PRIORITIES:
        problems.append(
            f"优先级必须是 {'/'.join(VALID_PRIORITIES)} 之一: {priority!r}"
        )
    try:
        parse_submitted_at(submitted_at)
    except TicketError as exc:
        problems.append(str(exc))
    return problems


def _row_to_ticket(row: sqlite3.Row) -> Ticket:
    return Ticket(
        ticket_no=row["ticket_no"],
        customer_ref=row["customer_ref"],
        title=row["title"],
        reporter_email=row["reporter_email"],
        priority=row["priority"],
        submitted_at=row["submitted_at"],
        status=row["status"],
        merged_into=row["merged_into"],
        merged_at=row["merged_at"],
    )


def get_ticket(conn: sqlite3.Connection, ticket_no: str) -> Ticket | None:
    row = conn.execute(
        "SELECT * FROM tickets WHERE ticket_no = ?", (ticket_no,)
    ).fetchone()
    return _row_to_ticket(row) if row is not None else None


def get_by_customer_ref(conn: sqlite3.Connection, customer_ref: str) -> Ticket | None:
    row = conn.execute(
        "SELECT * FROM tickets WHERE customer_ref = ?", (customer_ref,)
    ).fetchone()
    return _row_to_ticket(row) if row is not None else None


def register_ticket(
    conn: sqlite3.Connection,
    *,
    customer_ref: str,
    title: str,
    reporter_email: str,
    priority: str,
    submitted_at: str,
) -> RegisterResult:
    """Create a ticket, or recognise an identical retry of the same request.

    Validation runs before any write.  When the customer request number was
    registered before with exactly the same title / email / priority /
    submitted-time text, the original ticket is returned untouched
    (``duplicate=True``); any differing field is an error and writes nothing.
    """
    problems = validate_ticket_fields(title, reporter_email, priority, submitted_at)
    if problems:
        raise TicketError("; ".join(problems))

    # The timestamp text is stored verbatim once it parses.
    parse_submitted_at(submitted_at)

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = get_by_customer_ref(conn, customer_ref)
        if existing is not None:
            if existing.status == STATUS_MERGED:
                raise TicketError(
                    f"客户请求号 {customer_ref!r} 所属工单 {existing.ticket_no} 已合并，"
                    "不能再用于新建工单"
                )
            same = (
                existing.title == title
                and existing.reporter_email == reporter_email
                and existing.priority == priority
                and existing.submitted_at == submitted_at
            )
            if not same:
                raise TicketError(
                    f"客户请求号 {customer_ref!r} 已登记为工单 {existing.ticket_no}，"
                    "且字段与本次提交不一致"
                )
            conn.execute("COMMIT")
            return RegisterResult(ticket=existing, duplicate=True)

        # BEGIN IMMEDIATE already holds the write lock, so MAX(id) is stable.
        next_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM tickets"
        ).fetchone()[0]
        ticket_no = f"T{next_id}"
        conn.execute(
            """
            INSERT INTO tickets (
                ticket_no, customer_ref, title, reporter_email, priority,
                submitted_at, status, merged_into, merged_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                ticket_no,
                customer_ref,
                title,
                reporter_email,
                priority,
                submitted_at,
                STATUS_OPEN,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    ticket = get_ticket(conn, ticket_no)
    assert ticket is not None
    return RegisterResult(ticket=ticket, duplicate=False)


def merge_tickets(
    conn: sqlite3.Connection, *, merged_no: str, master_no: str, merged_at: str
) -> Ticket:
    """Merge ``merged_no`` into ``master_no``.

    Both tickets must exist, be open, and be distinct.  Already-merged
    tickets can participate on neither side.  The master ticket's fields
    are left unchanged; the merged ticket records the master and timestamp.
    """
    if merged_no == master_no:
        raise TicketError("不能将工单合并到自身")
    parse_submitted_at(merged_at)

    conn.execute("BEGIN IMMEDIATE")
    try:
        merged = get_ticket(conn, merged_no)
        if merged is None:
            raise TicketError(f"工单不存在: {merged_no}")
        master = get_ticket(conn, master_no)
        if master is None:
            raise TicketError(f"工单不存在: {master_no}")
        if merged.status != STATUS_OPEN:
            raise TicketError(f"工单 {merged_no} 状态为 {merged.status}，无法被合并")
        if master.status != STATUS_OPEN:
            raise TicketError(
                f"工单 {master_no} 状态为 {master.status}，无法作为主工单"
            )

        conn.execute(
            """
            UPDATE tickets
               SET status = ?, merged_into = ?, merged_at = ?
             WHERE ticket_no = ?
            """,
            (STATUS_MERGED, master_no, merged_at, merged_no),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    ticket = get_ticket(conn, merged_no)
    assert ticket is not None
    return ticket


def list_open_tickets(conn: sqlite3.Connection) -> list[Ticket]:
    rows = conn.execute(
        """
        SELECT * FROM tickets
         WHERE status = ?
         ORDER BY CAST(substr(ticket_no, 2) AS INTEGER) ASC
        """,
        (STATUS_OPEN,),
    ).fetchall()
    return [_row_to_ticket(row) for row in rows]
