"""Command-line entry point."""

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__

DB_PATH = Path(__file__).resolve().parents[1] / "sla_desk.db"

PRIORITIES = ("P1", "P2", "P3", "P4")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    priority TEXT NOT NULL,
    title TEXT NOT NULL,
    response_minutes INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    paused_seconds INTEGER NOT NULL DEFAULT 0
)
"""


def _positive_int(value: str) -> int:
    try:
        number = int(value, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a positive integer: {value!r}")
    if number <= 0:
        raise argparse.ArgumentTypeError(f"not a positive integer: {value!r}")
    return number


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute(_SCHEMA)
    return connection


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime(TIME_FORMAT)


def _to_record(row: sqlite3.Row) -> dict:
    created_at = row["created_at"]
    response_due_at = None
    if created_at:
        due = datetime.strptime(created_at, TIME_FORMAT) + timedelta(
            minutes=row["response_minutes"]
        )
        response_due_at = due.strftime(TIME_FORMAT)
    return {
        "id": row["id"],
        "priority": row["priority"],
        "title": row["title"],
        "response_minutes": row["response_minutes"],
        "state": row["state"],
        "created_at": created_at,
        "response_due_at": response_due_at,
        "paused_seconds": row["paused_seconds"],
    }


def _cmd_register(args: argparse.Namespace, db_path: Path) -> int:
    with _connect(db_path) as connection:
        existing = connection.execute(
            "SELECT 1 FROM tickets WHERE id = ?", (args.id,)
        ).fetchone()
        if existing is not None:
            print(f"error: duplicate ticket id: {args.id!r}", file=sys.stderr)
            return 3
        connection.execute(
            "INSERT INTO tickets"
            " (id, priority, title, response_minutes, state, created_at, paused_seconds)"
            " VALUES (?, ?, ?, ?, ?, ?, 0)",
            (args.id, args.priority, args.title, args.response_minutes, "accepted", _now_utc()),
        )
    print(f"registered {args.id} accepted")
    return 0


def _cmd_status(args: argparse.Namespace, db_path: Path) -> int:
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM tickets WHERE id = ?", (args.id,)
        ).fetchone()
    if row is None:
        print(f"error: ticket not found: {args.id!r}", file=sys.stderr)
        return 4
    print(json.dumps(_to_record(row), ensure_ascii=False))
    return 0


def _cmd_list(args: argparse.Namespace, db_path: Path) -> int:
    with _connect(db_path) as connection:
        if args.id is not None:
            row = connection.execute(
                "SELECT * FROM tickets WHERE id = ?", (args.id,)
            ).fetchone()
            if row is None:
                print(f"error: ticket not found: {args.id!r}", file=sys.stderr)
                return 4
            rows = [row]
        else:
            rows = connection.execute(
                "SELECT * FROM tickets ORDER BY created_at ASC, rowid ASC"
            ).fetchall()
    for row in rows:
        print(json.dumps(_to_record(row), ensure_ascii=False))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sla-desk",
        description="Local 工单 SLA 与升级路由 ledger.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    register = subparsers.add_parser("register", help="登记新工单")
    register.add_argument("--id", required=True, help="工单号（大小写敏感，原样保存）")
    register.add_argument("--priority", required=True, choices=PRIORITIES)
    register.add_argument("--title", required=True)
    register.add_argument("--response-minutes", required=True, type=_positive_int)

    status = subparsers.add_parser("status", help="查询单个工单")
    status.add_argument("--id", required=True)

    listing = subparsers.add_parser("list", help="按创建时间升序列出工单")
    listing.add_argument("--id", default=None, help="可选，仅查询指定工单")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "register":
        if args.id == "":
            parser.error("ticket id must not be empty")
        if args.title == "":
            parser.error("ticket title must not be empty")
        return _cmd_register(args, DB_PATH)
    if args.command == "status":
        return _cmd_status(args, DB_PATH)
    if args.command == "list":
        return _cmd_list(args, DB_PATH)
    parser.error(f"unknown command: {args.command}")
    return 2
