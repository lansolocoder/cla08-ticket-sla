"""Command-line entry point."""

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__

PRIORITIES = ("low", "medium", "high", "critical")

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "ledger.db"


def _parse_minutes(raw: str) -> int | None:
    """Return a positive decimal integer, or None for anything else."""
    if not raw or not all("0" <= ch <= "9" for ch in raw):
        return None
    value = int(raw)
    return value if value > 0 else None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sla-desk",
        description="Local 工单 SLA 与升级路由 ledger.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    register = subparsers.add_parser("register", help="登记一条工单")
    register.add_argument("--ticket", required=True)
    register.add_argument("--title", required=True)
    register.add_argument("--priority", required=True)
    register.add_argument("--response-minutes", dest="response_minutes", required=True)

    subparsers.add_parser("list", help="列出全部工单")

    return parser


def _register(ticket: str, title: str, priority: str, minutes: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tickets ("
            "ticket TEXT PRIMARY KEY, "
            "title TEXT NOT NULL, "
            "priority TEXT NOT NULL, "
            "response_minutes INTEGER NOT NULL)"
        )
        try:
            conn.execute(
                "INSERT INTO tickets (ticket, title, priority, response_minutes) "
                "VALUES (?, ?, ?, ?)",
                (ticket, title, priority, minutes),
            )
        except sqlite3.IntegrityError:
            print(f"duplicate ticket: {ticket}", file=sys.stderr)
            return 3
        conn.commit()
    finally:
        conn.close()

    print(f"{ticket} registered")
    return 0


def _list() -> int:
    if not DB_PATH.exists():
        return 0

    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT ticket, priority, response_minutes, title FROM tickets"
        ).fetchall()
    finally:
        conn.close()

    rows.sort(key=lambda row: row[0])
    sys.stdout.write("".join(f"{ticket}|{priority}|{minutes}|{title}\n"
                              for ticket, priority, minutes, title in rows))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "register":
        if args.priority not in PRIORITIES:
            print(f"invalid priority: {args.priority}", file=sys.stderr)
            return 2
        minutes = _parse_minutes(args.response_minutes)
        if minutes is None:
            print(f"invalid response-minutes: {args.response_minutes}",
                  file=sys.stderr)
            return 2
        return _register(args.ticket, args.title, args.priority, minutes)

    if args.command == "list":
        return _list()

    return 0
