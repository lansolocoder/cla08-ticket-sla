"""Command-line entry point."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import datetime
from functools import partial
from pathlib import Path
import re
import sqlite3
import sys

from . import __version__

DB_PATH = Path(__file__).resolve().parents[1] / "sla.db"

PRIORITIES = ("P1", "P2", "P3", "P4")
STATE_OPEN = "open"
STATE_ACKNOWLEDGED = "acknowledged"
STATE_RESOLVED = "resolved"

EXIT_DUPLICATE = 2
EXIT_ILLEGAL_TRANSITION = 3
EXIT_NOT_FOUND = 4
EXIT_INVALID_FIELD = 5

_CREATED_PATTERN = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\Z")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    priority TEXT NOT NULL,
    created TEXT NOT NULL,
    state TEXT NOT NULL
)
"""


def _fail(message: str, code: int) -> int:
    print(f"sla-desk: error: {message}", file=sys.stderr)
    return code


def _validate_ticket_id(ticket_id: str) -> str | None:
    if not ticket_id or any(char.isspace() for char in ticket_id):
        return "工单号不能为空或包含空白字符"
    return None


def _validate_priority(priority: str) -> str | None:
    if priority not in PRIORITIES:
        return "priority 必须是 P1、P2、P3 或 P4"
    return None


def _validate_created(created: str) -> str | None:
    if not _CREATED_PATTERN.match(created):
        return "created 必须是 YYYY-MM-DDTHH:MM:SS 格式的 UTC 时间戳"
    try:
        datetime.strptime(created, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return "created 必须是真实存在的 UTC 时间"
    return None


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.execute(_SCHEMA)
    return connection


def _fetch(connection: sqlite3.Connection, ticket_id: str) -> tuple | None:
    return connection.execute(
        "SELECT id, priority, created, state FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()


def _cmd_open(args: argparse.Namespace) -> int:
    problem = (
        _validate_ticket_id(args.id)
        or _validate_priority(args.priority)
        or _validate_created(args.created)
    )
    if problem:
        return _fail(problem, EXIT_INVALID_FIELD)
    with _connect() as connection:
        if _fetch(connection, args.id) is not None:
            return _fail(f"工单 {args.id} 已存在", EXIT_DUPLICATE)
        connection.execute(
            "INSERT INTO tickets (id, priority, created, state) VALUES (?, ?, ?, ?)",
            (args.id, args.priority, args.created, STATE_OPEN),
        )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    problem = _validate_ticket_id(args.id)
    if problem:
        return _fail(problem, EXIT_INVALID_FIELD)
    with _connect() as connection:
        row = _fetch(connection, args.id)
    if row is None:
        return _fail(f"工单 {args.id} 不存在", EXIT_NOT_FOUND)
    ticket_id, priority, created, state = row
    print(f"id={ticket_id}")
    print(f"priority={priority}")
    print(f"created={created}")
    print(f"state={state}")
    return 0


def _cmd_transition(
    args: argparse.Namespace, expected: str, target: str
) -> int:
    problem = _validate_ticket_id(args.id)
    if problem:
        return _fail(problem, EXIT_INVALID_FIELD)
    with _connect() as connection:
        row = _fetch(connection, args.id)
        if row is None:
            return _fail(f"工单 {args.id} 不存在", EXIT_NOT_FOUND)
        current = row[3]
        if current != expected:
            return _fail(
                f"不允许从 {current} 流转到 {target}", EXIT_ILLEGAL_TRANSITION
            )
        connection.execute(
            "UPDATE tickets SET state = ? WHERE id = ?", (target, args.id)
        )
    print(f"id={args.id} state={target}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sla-desk",
        description="Local 工单 SLA 与升级路由 ledger.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    ticket = subparsers.add_parser("ticket", help="工单登记与状态流转")
    ticket_subparsers = ticket.add_subparsers(dest="ticket_command")

    open_parser = ticket_subparsers.add_parser("open", help="登记新工单")
    open_parser.add_argument("--id", required=True, help="唯一工单号")
    open_parser.add_argument("--priority", required=True, help="P1|P2|P3|P4")
    open_parser.add_argument(
        "--created", required=True, help="UTC 时间戳 YYYY-MM-DDTHH:MM:SS"
    )
    open_parser.set_defaults(handler=_cmd_open)

    show_parser = ticket_subparsers.add_parser("show", help="查看工单")
    show_parser.add_argument("--id", required=True, help="工单号")
    show_parser.set_defaults(handler=_cmd_show)

    ack_parser = ticket_subparsers.add_parser(
        "ack", help="确认工单 (open -> acknowledged)"
    )
    ack_parser.add_argument("--id", required=True, help="工单号")
    ack_parser.set_defaults(
        handler=partial(_cmd_transition, expected=STATE_OPEN, target=STATE_ACKNOWLEDGED)
    )

    resolve_parser = ticket_subparsers.add_parser(
        "resolve", help="办结工单 (acknowledged -> resolved)"
    )
    resolve_parser.add_argument("--id", required=True, help="工单号")
    resolve_parser.set_defaults(
        handler=partial(
            _cmd_transition, expected=STATE_ACKNOWLEDGED, target=STATE_RESOLVED
        )
    )

    reopen_parser = ticket_subparsers.add_parser(
        "reopen", help="重开工单 (resolved -> open)"
    )
    reopen_parser.add_argument("--id", required=True, help="工单号")
    reopen_parser.set_defaults(
        handler=partial(_cmd_transition, expected=STATE_RESOLVED, target=STATE_OPEN)
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] | None = getattr(
        args, "handler", None
    )
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)
