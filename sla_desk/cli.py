"""Command-line entry point."""

import argparse
from collections.abc import Sequence
from pathlib import Path
import re
import sqlite3
import sys

from . import __version__

DB_PATH = Path(__file__).resolve().parents[1] / "ledger.db"

PRIORITIES = ("low", "medium", "high", "critical")

_RESPONSE_MINUTES_RE = re.compile(r"[0-9]+")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    ticket TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    priority TEXT NOT NULL,
    response_minutes INTEGER NOT NULL
)
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(_SCHEMA)
    return conn


def _register(args: argparse.Namespace) -> int:
    if args.priority not in PRIORITIES:
        print(f"invalid priority: {args.priority}", file=sys.stderr)
        return 2
    minutes = args.response_minutes
    if _RESPONSE_MINUTES_RE.fullmatch(minutes) is None or int(minutes) <= 0:
        print(f"invalid response-minutes: {minutes}", file=sys.stderr)
        return 2
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM tickets WHERE ticket = ?", (args.ticket,)
        ).fetchone()
        if row is not None:
            print(f"duplicate ticket: {args.ticket}", file=sys.stderr)
            return 3
        conn.execute(
            "INSERT INTO tickets (ticket, title, priority, response_minutes)"
            " VALUES (?, ?, ?, ?)",
            (args.ticket, args.title, args.priority, int(minutes)),
        )
    print(f"{args.ticket} registered")
    return 0


def _list(args: argparse.Namespace) -> int:
    if not DB_PATH.exists():
        return 0
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ticket, priority, response_minutes, title FROM tickets"
            " ORDER BY ticket"
        ).fetchall()
    for ticket, priority, minutes, title in rows:
        print(f"{ticket}|{priority}|{minutes}|{title}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sla-desk",
        description="Local 工单 SLA 与升级路由 ledger.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    register = subparsers.add_parser("register", help="登记一条工单.")
    register.add_argument("--ticket", required=True)
    register.add_argument("--title", required=True)
    register.add_argument("--priority", required=True)
    register.add_argument("--response-minutes", required=True)
    register.set_defaults(handler=_register)

    list_parser = subparsers.add_parser("list", help="列出全部工单.")
    list_parser.set_defaults(handler=_list)

    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)
