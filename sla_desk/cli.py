"""Command-line entry point."""

import argparse
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

from . import __version__

PRIORITIES = ("P1", "P2", "P3", "P4")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
STATE_ACCEPTED = "accepted"
STATE_PAUSED = "paused"
STATE_ESCALATED = "escalated"

# 超出 response_due_at 的整秒数超过该阈值时定级 L2，否则 L1
_L2_OVERDUE_THRESHOLDS = {"P1": 3600, "P2": 7200, "P3": 14400, "P4": 14400}

EXIT_INVALID = 2
EXIT_DUPLICATE = 3
EXIT_NOT_FOUND = 4
EXIT_CONFLICT = 5

_TICKET_COLUMNS = "id, priority, title, response_minutes, state, created_at, paused_seconds"


def _db_path() -> Path:
    override = os.environ.get("SLA_DESK_DB")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "sla_desk.db"


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_db_path())
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id TEXT PRIMARY KEY,
            priority TEXT NOT NULL,
            title TEXT NOT NULL,
            response_minutes INTEGER NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            paused_seconds INTEGER NOT NULL DEFAULT 0,
            paused_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS escalations (
            ticket_id TEXT NOT NULL,
            at TEXT NOT NULL,
            level TEXT NOT NULL
        )
        """
    )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(tickets)").fetchall()
    }
    if "paused_at" not in columns:
        connection.execute("ALTER TABLE tickets ADD COLUMN paused_at TEXT")
    return connection


def _non_empty(value: str) -> str:
    if value == "":
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def _positive_int(value: str) -> int:
    try:
        number = int(value, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid positive integer: {value!r}"
        ) from None
    if number <= 0:
        raise argparse.ArgumentTypeError(f"not a positive integer: {value!r}")
    return number


def _utc_time(value: str) -> datetime:
    try:
        moment = datetime.strptime(value, TIME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid UTC time: {value!r}") from None
    if moment.strftime(TIME_FORMAT) != value:
        raise argparse.ArgumentTypeError(f"invalid UTC time: {value!r}")
    return moment


def _ticket_json(row: tuple, escalations: list[dict] | None = None) -> dict:
    ticket_id, priority, title, response_minutes, state, created_at, paused_seconds = row
    response_due_at = None
    if created_at is not None and response_minutes is not None:
        start = datetime.strptime(created_at, TIME_FORMAT).replace(tzinfo=timezone.utc)
        due = start + timedelta(minutes=response_minutes, seconds=paused_seconds or 0)
        response_due_at = due.strftime(TIME_FORMAT)
    return {
        "id": ticket_id,
        "priority": priority,
        "title": title,
        "response_minutes": response_minutes,
        "state": state,
        "created_at": created_at,
        "response_due_at": response_due_at,
        "paused_seconds": paused_seconds,
        "escalations": escalations if escalations is not None else [],
    }


def _fetch_escalations(connection: sqlite3.Connection, ticket_id: str) -> list[dict]:
    rows = connection.execute(
        "SELECT at, level FROM escalations WHERE ticket_id = ? ORDER BY rowid ASC",
        (ticket_id,),
    ).fetchall()
    return [{"at": at, "level": level} for at, level in rows]


def _cmd_register(args: argparse.Namespace) -> int:
    created_at = datetime.now(timezone.utc).strftime(TIME_FORMAT)
    connection = _connect()
    try:
        with connection:
            connection.execute(
                "INSERT INTO tickets (id, priority, title, response_minutes, state,"
                " created_at, paused_seconds) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    args.id,
                    args.priority,
                    args.title,
                    args.response_minutes,
                    STATE_ACCEPTED,
                    created_at,
                    0,
                ),
            )
    except sqlite3.IntegrityError:
        print(f"error: 工单号已被占用，拒绝重复登记: {args.id}", file=sys.stderr)
        return EXIT_DUPLICATE
    finally:
        connection.close()
    print(f"registered {args.id} {STATE_ACCEPTED}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    connection = _connect()
    try:
        row = connection.execute(
            f"SELECT {_TICKET_COLUMNS} FROM tickets WHERE id = ?", (args.id,)
        ).fetchone()
        if row is None:
            print(f"error: 工单不存在: {args.id}", file=sys.stderr)
            return EXIT_NOT_FOUND
        escalations = _fetch_escalations(connection, args.id)
    finally:
        connection.close()
    print(json.dumps(_ticket_json(row, escalations), ensure_ascii=False))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    connection = _connect()
    try:
        rows = connection.execute(
            f"SELECT {_TICKET_COLUMNS} FROM tickets ORDER BY created_at ASC, rowid ASC"
        ).fetchall()
        escalations = [(_fetch_escalations(connection, row[0])) for row in rows]
    finally:
        connection.close()
    for row, ticket_escalations in zip(rows, escalations):
        print(json.dumps(_ticket_json(row, ticket_escalations), ensure_ascii=False))
    return 0


def _cmd_pause(args: argparse.Namespace) -> int:
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT state FROM tickets WHERE id = ?", (args.id,)
        ).fetchone()
        if row is None:
            print(f"error: 工单不存在: {args.id}", file=sys.stderr)
            return EXIT_NOT_FOUND
        if row[0] == STATE_PAUSED:
            print(f"error: 工单已处于暂停状态，拒绝重复暂停: {args.id}", file=sys.stderr)
            return EXIT_CONFLICT
        with connection:
            connection.execute(
                "UPDATE tickets SET state = ?, paused_at = ? WHERE id = ?",
                (STATE_PAUSED, args.at.strftime(TIME_FORMAT), args.id),
            )
    finally:
        connection.close()
    print(f"paused {args.id} {STATE_PAUSED}")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT state, created_at, paused_seconds, paused_at FROM tickets WHERE id = ?",
            (args.id,),
        ).fetchone()
        if row is None:
            print(f"error: 工单不存在: {args.id}", file=sys.stderr)
            return EXIT_NOT_FOUND
        state, created_at, paused_seconds, paused_at = row
        if state != STATE_PAUSED:
            print(f"error: 工单未处于暂停状态，拒绝恢复: {args.id}", file=sys.stderr)
            return EXIT_CONFLICT
        paused_start = datetime.strptime(paused_at, TIME_FORMAT).replace(
            tzinfo=timezone.utc
        )
        created = datetime.strptime(created_at, TIME_FORMAT).replace(tzinfo=timezone.utc)
        if args.at < paused_start or args.at < created:
            print(
                f"error: 恢复时刻早于最近一次暂停时刻或创建时刻: {args.id}",
                file=sys.stderr,
            )
            return EXIT_INVALID
        interval = int((args.at - paused_start).total_seconds())
        with connection:
            connection.execute(
                "UPDATE tickets SET state = ?, paused_seconds = ?, paused_at = NULL"
                " WHERE id = ?",
                (STATE_ACCEPTED, paused_seconds + interval, args.id),
            )
    finally:
        connection.close()
    print(f"resumed {args.id} {STATE_ACCEPTED}")
    return 0


def _escalation_level(priority: str, overdue_seconds: int) -> str:
    if overdue_seconds > _L2_OVERDUE_THRESHOLDS[priority]:
        return "L2"
    return "L1"


def _cmd_escalate(args: argparse.Namespace) -> int:
    at_text = args.at.strftime(TIME_FORMAT)
    connection = _connect()
    try:
        rows = connection.execute(
            f"SELECT {_TICKET_COLUMNS} FROM tickets ORDER BY created_at ASC, rowid ASC"
        ).fetchall()
        for row in rows:
            created = datetime.strptime(row[5], TIME_FORMAT).replace(tzinfo=timezone.utc)
            if args.at < created:
                print(
                    f"error: 观察时刻早于工单创建时刻: {row[0]}",
                    file=sys.stderr,
                )
                return EXIT_INVALID
        due_tickets = []
        for row in rows:
            ticket_id, priority, _, response_minutes, state, created_at, paused_seconds = row
            if state != STATE_ACCEPTED:
                continue
            created = datetime.strptime(created_at, TIME_FORMAT).replace(
                tzinfo=timezone.utc
            )
            due = created + timedelta(
                minutes=response_minutes, seconds=paused_seconds or 0
            )
            if args.at < due:
                continue
            overdue_seconds = int((args.at - due).total_seconds())
            due_tickets.append(
                (ticket_id, priority, _escalation_level(priority, overdue_seconds))
            )
        with connection:
            for ticket_id, _, level in due_tickets:
                connection.execute(
                    "UPDATE tickets SET state = ? WHERE id = ?",
                    (STATE_ESCALATED, ticket_id),
                )
                connection.execute(
                    "INSERT INTO escalations (ticket_id, at, level) VALUES (?, ?, ?)",
                    (ticket_id, at_text, level),
                )
    finally:
        connection.close()
    for ticket_id, priority, _ in due_tickets:
        print(
            json.dumps(
                {
                    "id": ticket_id,
                    "priority": priority,
                    "state": STATE_ESCALATED,
                    "escalated_at": at_text,
                },
                ensure_ascii=False,
            )
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sla-desk",
        description="Local 工单 SLA 与升级路由 ledger.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    register = subparsers.add_parser("register", help="登记新工单")
    register.add_argument("--id", required=True, type=_non_empty, help="工单号")
    register.add_argument("--priority", required=True, choices=PRIORITIES)
    register.add_argument("--title", required=True, type=_non_empty)
    register.add_argument(
        "--response-minutes",
        required=True,
        type=_positive_int,
        dest="response_minutes",
        help="响应时限（正整数分钟）",
    )
    register.set_defaults(handler=_cmd_register)

    status = subparsers.add_parser("status", help="查询单个工单")
    status.add_argument("--id", required=True, type=_non_empty, help="工单号")
    status.set_defaults(handler=_cmd_status)

    list_parser = subparsers.add_parser("list", help="按创建时间升序列出全部工单")
    list_parser.set_defaults(handler=_cmd_list)

    pause = subparsers.add_parser("pause", help="暂停工单 SLA 计时")
    pause.add_argument("--id", required=True, type=_non_empty, help="工单号")
    pause.add_argument(
        "--at",
        required=True,
        type=_utc_time,
        help="暂停时刻（UTC，YYYY-MM-DDThh:mm:ssZ）",
    )
    pause.set_defaults(handler=_cmd_pause)

    resume = subparsers.add_parser("resume", help="恢复工单 SLA 计时")
    resume.add_argument("--id", required=True, type=_non_empty, help="工单号")
    resume.add_argument(
        "--at",
        required=True,
        type=_utc_time,
        help="恢复时刻（UTC，YYYY-MM-DDThh:mm:ssZ）",
    )
    resume.set_defaults(handler=_cmd_resume)

    escalate = subparsers.add_parser("escalate", help="到期升级检查全部工单")
    escalate.add_argument(
        "--at",
        required=True,
        type=_utc_time,
        help="观察时刻（UTC，YYYY-MM-DDThh:mm:ssZ）",
    )
    escalate.set_defaults(handler=_cmd_escalate)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)
