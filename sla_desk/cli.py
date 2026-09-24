"""Command-line entry point."""

import argparse
from collections.abc import Sequence
from datetime import datetime
import os
from pathlib import Path
import sqlite3
import sys

from . import __version__

PRIORITIES = ("P1", "P2", "P3", "P4")
CREATED_FORMAT = "%Y-%m-%dT%H:%M:%S"

# 每个状态流转子命令对应的 (原状态, 新状态)。
TRANSITIONS = {
    "ack": ("open", "acknowledged"),
    "resolve": ("acknowledged", "resolved"),
    "reopen": ("resolved", "open"),
}

EXIT_DUPLICATE = 2
EXIT_BAD_TRANSITION = 3
EXIT_NOT_FOUND = 4
EXIT_INVALID = 5


def _db_path() -> Path:
    override = os.environ.get("SLA_DESK_DB")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "sla.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tickets ("
        "id TEXT PRIMARY KEY, "
        "priority TEXT NOT NULL, "
        "created TEXT NOT NULL, "
        "state TEXT NOT NULL)"
    )
    return conn


def _fail(message: str, code: int) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def _valid_id(ticket_id: str) -> bool:
    return bool(ticket_id) and not any(ch.isspace() for ch in ticket_id)


def _valid_created(value: str) -> bool:
    try:
        parsed = datetime.strptime(value, CREATED_FORMAT)
    except ValueError:
        return False
    return parsed.strftime(CREATED_FORMAT) == value


def _fetch(conn: sqlite3.Connection, ticket_id: str) -> tuple | None:
    return conn.execute(
        "SELECT id, priority, created, state FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()


def _cmd_open(args: argparse.Namespace) -> int:
    if not _valid_id(args.id):
        return _fail(f"invalid --id {args.id!r}", EXIT_INVALID)
    if args.priority not in PRIORITIES:
        return _fail(
            f"invalid --priority {args.priority!r} (expected one of {', '.join(PRIORITIES)})",
            EXIT_INVALID,
        )
    if not _valid_created(args.created):
        return _fail(
            f"invalid --created {args.created!r} (expected YYYY-MM-DDTHH:MM:SS UTC)",
            EXIT_INVALID,
        )
    with _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO tickets (id, priority, created, state) VALUES (?, ?, ?, 'open')",
                (args.id, args.priority, args.created),
            )
        except sqlite3.IntegrityError:
            return _fail(f"ticket {args.id!r} already exists", EXIT_DUPLICATE)
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    if not _valid_id(args.id):
        return _fail(f"invalid --id {args.id!r}", EXIT_INVALID)
    with _connect() as conn:
        row = _fetch(conn, args.id)
    if row is None:
        return _fail(f"ticket {args.id!r} not found", EXIT_NOT_FOUND)
    ticket_id, priority, created, state = row
    print(f"id={ticket_id}")
    print(f"priority={priority}")
    print(f"created={created}")
    print(f"state={state}")
    return 0


def _cmd_transition(args: argparse.Namespace, action: str) -> int:
    if not _valid_id(args.id):
        return _fail(f"invalid --id {args.id!r}", EXIT_INVALID)
    expected_from, new_state = TRANSITIONS[action]
    with _connect() as conn:
        row = _fetch(conn, args.id)
        if row is None:
            return _fail(f"ticket {args.id!r} not found", EXIT_NOT_FOUND)
        state = row[3]
        if state != expected_from:
            return _fail(
                f"cannot {action} ticket {args.id!r} in state {state!r}",
                EXIT_BAD_TRANSITION,
            )
        conn.execute("UPDATE tickets SET state = ? WHERE id = ?", (new_state, args.id))
    print(f"id={args.id} state={new_state}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sla-desk",
        description="Local 工单 SLA 与升级路由 ledger.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")
    ticket_parser = subparsers.add_parser("ticket", help="工单登记与状态流转")
    ticket_subparsers = ticket_parser.add_subparsers(dest="action")

    open_parser = ticket_subparsers.add_parser("open", help="登记新工单")
    open_parser.add_argument("--id", required=True, help="唯一工单号")
    open_parser.add_argument("--priority", required=True, help="P1|P2|P3|P4")
    open_parser.add_argument("--created", required=True, help="UTC 时间戳 YYYY-MM-DDTHH:MM:SS")

    for action in ("show", "ack", "resolve", "reopen"):
        action_parser = ticket_subparsers.add_parser(action, help=f"{action} 工单")
        action_parser.add_argument("--id", required=True, help="工单号")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.action is None:
        ticket_parser.print_help()
        return 0
    if args.action == "open":
        return _cmd_open(args)
    if args.action == "show":
        return _cmd_show(args)
    return _cmd_transition(args, args.action)
