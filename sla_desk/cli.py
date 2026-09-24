"""Command-line entry point."""

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__

ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = ROOT / "ticket_ledger.json"

_RESPONSE_SECONDS = {"P1": 3600, "P2": 14400, "P3": 28800, "P4": 57600}
_CREATED_AT_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
_RECORD_FIELDS = (
    "ticket_id",
    "priority",
    "created_at",
    "sla_due_at",
    "elapsed_seconds",
)


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _parse_created_at(value: str) -> datetime:
    if not _CREATED_AT_RE.fullmatch(value):
        raise ValueError("bad timestamp literal")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")


def _is_valid_ledger(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            return False
        if not all(field in value for field in _RECORD_FIELDS):
            return False
    return True


def _register(ticket_id: str, priority: str, created_at: str) -> int:
    if priority not in _RESPONSE_SECONDS:
        return _fail(
            f"非法优先级 {priority!r}：仅接受 P1、P2、P3、P4 四个大写字面值"
        )

    try:
        created = _parse_created_at(created_at)
        due = created + timedelta(seconds=_RESPONSE_SECONDS[priority])
        sla_due_at = due.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError):
        return _fail(
            f"非法创建时间 {created_at!r}：仅接受精确到秒、以 Z 结尾的 UTC "
            "ISO 8601 字面值，例如 2025-01-02T03:04:05Z"
        )

    if LEDGER_PATH.exists():
        try:
            raw = LEDGER_PATH.read_text(encoding="utf-8")
            ledger = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            return _fail(f"ticket_ledger.json 无法读取或不是合法 JSON：{exc}")
        if not _is_valid_ledger(ledger):
            return _fail("ticket_ledger.json 结构不符：根节点必须是工单号到记录对象的映射")
        if ticket_id in ledger:
            return _fail(f"工单号 {ticket_id} 已存在，不得重复登记")
    else:
        ledger = {}

    record = {
        "ticket_id": ticket_id,
        "priority": priority,
        "created_at": created_at,
        "sla_due_at": sla_due_at,
        "elapsed_seconds": 0,
    }
    ledger[ticket_id] = record

    payload = json.dumps(ledger, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=ROOT, prefix=".ticket_ledger.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, LEDGER_PATH)
    except OSError as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        return _fail(f"ticket_ledger.json 写入失败：{exc}")

    print(json.dumps(record, ensure_ascii=False))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sla-desk",
        description="Local 工单 SLA 与升级路由 ledger.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    register_parser = subparsers.add_parser("register", help="登记一张工单")
    register_parser.add_argument("ticket_id", help="工单号")
    register_parser.add_argument("priority", help="优先级：P1/P2/P3/P4")
    register_parser.add_argument(
        "created_at",
        help="创建时间（UTC），形如 2025-01-02T03:04:05Z",
    )

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "register":
        return _register(args.ticket_id, args.priority, args.created_at)
    return 1
