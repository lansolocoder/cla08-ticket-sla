"""Command-line entry point."""

import argparse
import json
import re
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__

LEDGER_PATH = Path(__file__).resolve().parent.parent / "ticket_ledger.json"

PRIORITY_TARGET_SECONDS = {
    "P1": 3600,
    "P2": 14400,
    "P3": 28800,
    "P4": 57600,
}

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def _parse_created_at(value: str) -> datetime | None:
    if not TIMESTAMP_PATTERN.match(value):
        return None
    try:
        parsed = datetime.strptime(value, TIMESTAMP_FORMAT)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _load_ledger() -> dict | None:
    """Return the ledger mapping, or None if the file is missing/unusable."""
    if not LEDGER_PATH.exists():
        return {}
    try:
        data = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if any(not isinstance(record, dict) for record in data.values()):
        return None
    return data


def register_ticket(ticket_id: str, priority: str, created_at: str) -> int:
    if priority not in PRIORITY_TARGET_SECONDS:
        return _fail(
            f"invalid priority {priority!r}; expected one of P1, P2, P3, P4"
        )
    created = _parse_created_at(created_at)
    if created is None:
        return _fail(
            f"invalid created_at {created_at!r}; expected ISO 8601 UTC "
            "timestamp like 2025-01-02T03:04:05Z"
        )
    try:
        due = created + timedelta(seconds=PRIORITY_TARGET_SECONDS[priority])
    except OverflowError:
        return _fail(f"created_at {created_at!r} is too large to compute sla_due_at")
    record = {
        "ticket_id": ticket_id,
        "priority": priority,
        "created_at": created_at,
        "sla_due_at": (
            f"{due.year:04d}-{due.month:02d}-{due.day:02d}"
            f"T{due.hour:02d}:{due.minute:02d}:{due.second:02d}Z"
        ),
        "elapsed_seconds": 0,
    }
    ledger = _load_ledger()
    if ledger is None:
        return _fail(f"ledger file {LEDGER_PATH} is not a valid ticket ledger")
    if ticket_id in ledger:
        return _fail(f"ticket {ticket_id} is already registered")
    ledger[ticket_id] = record
    LEDGER_PATH.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(record, ensure_ascii=False))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sla-desk",
        description="Local 工单 SLA 与升级路由 ledger.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    register = subparsers.add_parser("register", help="登记新工单")
    register.add_argument("ticket_id", help="工单号")
    register.add_argument("priority", help="优先级, 仅接受 P1/P2/P3/P4")
    register.add_argument(
        "created_at", help="UTC 创建时间, 形如 2025-01-02T03:04:05Z"
    )
    args = parser.parse_args(argv)
    if args.command == "register":
        return register_ticket(args.ticket_id, args.priority, args.created_at)
    parser.print_help()
    return 0
