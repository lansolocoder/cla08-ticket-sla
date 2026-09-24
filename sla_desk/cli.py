"""Command-line entry point."""

import argparse
import datetime as _dt
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__, sla, storage

PROG = "sla-desk"

# 退出码约定：用法/校验错误与重复 id 为 2，记录不存在为 3，其余异常为 1。
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_FAILURE = 1


class CliError(Exception):
    """业务校验失败，以非零退出码结束。"""

    def __init__(self, message: str, exit_code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _emit_json(payload: object) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    # --db 同时挂在顶层与各叶子命令上，位置均可；子解析器用 SUPPRESS
    # 避免缺省值覆盖顶层已解析到的 --db。
    db_parent = argparse.ArgumentParser(add_help=False)
    db_parent.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="SQLite 台账路径（除 help/version 外必填）",
    )

    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Local 工单 SLA 与升级路由 ledger.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--db", default=None, help=argparse.SUPPRESS)

    resource = parser.add_subparsers(dest="resource")

    # window create
    window_parser = resource.add_parser("window", help="管理每日服务时段")
    window_action = window_parser.add_subparsers(dest="action")
    window_create = window_action.add_parser(
        "create", parents=[db_parent], help="登记一个每日服务时段"
    )
    window_create.add_argument("--id", required=True, help="时段 id")
    window_create.add_argument(
        "--start", required=True, help="本地开始时间 HH:MM"
    )
    window_create.add_argument("--end", required=True, help="本地结束时间 HH:MM")

    # ticket create / show / list
    ticket_parser = resource.add_parser("ticket", help="管理工单")
    ticket_action = ticket_parser.add_subparsers(dest="action")

    ticket_create = ticket_action.add_parser(
        "create", parents=[db_parent], help="登记工单并计算响应截止时间"
    )
    ticket_create.add_argument("--id", required=True, help="工单 id")
    ticket_create.add_argument(
        "--priority",
        required=True,
        choices=("high", "medium", "low"),
        help="优先级（high|medium|low，精确小写）",
    )
    ticket_create.add_argument(
        "--window-id", required=True, help="引用的服务时段 id"
    )
    ticket_create.add_argument(
        "--created-at",
        required=True,
        help="带时区偏移的 ISO8601 时刻，如 2026-03-02T09:15:00+08:00",
    )

    ticket_show = ticket_action.add_parser(
        "show", parents=[db_parent], help="按 id 查询工单"
    )
    ticket_show.add_argument("--id", required=True, help="工单 id")

    ticket_action.add_parser(
        "list", parents=[db_parent], help="按受理时间升序列出全部工单"
    )

    return parser


def _require_db(ns: argparse.Namespace) -> str:
    db_path = getattr(ns, "db", None)
    if not db_path:
        raise CliError("除 --help/--version 外，必须通过 --db 指定 SQLite 台账路径")
    return db_path


def _handle_window_create(ns: argparse.Namespace) -> int:
    # 先做纯参数校验，失败时不触碰台账文件。
    try:
        start = sla.parse_hhmm(ns.start)
        end = sla.parse_hhmm(ns.end)
        sla.validate_window(start, end)
    except sla.WindowError as exc:
        raise CliError(str(exc)) from exc

    db_path = _require_db(ns)
    try:
        with storage.connect(db_path) as conn:
            storage.create_window(conn, ns.id, start, end)
    except storage.DuplicateRecord as exc:
        raise CliError(str(exc), exit_code=2) from exc
    except storage.LedgerError as exc:
        raise CliError(str(exc), exit_code=EXIT_FAILURE) from exc
    return 0


def _ticket_json(
    ticket_id: str,
    priority: str,
    window_id: str,
    created_at: _dt.datetime,
    deadline: _dt.datetime,
) -> dict[str, str]:
    return {
        "id": ticket_id,
        "priority": priority,
        "window_id": window_id,
        "created_at": sla.format_utc(created_at),
        "deadline": sla.format_utc(deadline),
    }


def _handle_ticket_create(ns: argparse.Namespace) -> int:
    # argparse choices 已兜底，这里保留显式校验以给出清晰的中文报错。
    if ns.priority not in sla.SLA_MINUTES:
        raise CliError("--priority 只接受精确小写的 high|medium|low")
    try:
        created_at = sla.parse_created_at(ns.created_at)
    except sla.DateTimeError as exc:
        raise CliError(str(exc)) from exc

    db_path = _require_db(ns)
    tz = sla.local_timezone()
    try:
        # 窗口必须先经 window create 登记，因此工单创建不引导建库，
        # 失败时文件系统也保持原样。
        with storage.connect(db_path, create_ok=False) as conn:
            try:
                start, end = storage.get_window(conn, ns.window_id)
            except storage.NotFound as exc:
                raise CliError(str(exc), exit_code=EXIT_NOT_FOUND) from exc
            deadline = sla.calculate_deadline(
                created_at, start, end, sla.SLA_MINUTES[ns.priority], tz
            )
            try:
                storage.create_ticket(
                    conn, ns.id, ns.priority, ns.window_id, created_at, deadline
                )
            except storage.DuplicateRecord as exc:
                raise CliError(str(exc), exit_code=2) from exc
            payload = _ticket_json(
                ns.id, ns.priority, ns.window_id, created_at, deadline
            )
    except storage.LedgerError as exc:
        raise CliError(str(exc), exit_code=EXIT_FAILURE) from exc

    _emit_json(payload)
    return 0


def _handle_ticket_show(ns: argparse.Namespace) -> int:
    db_path = _require_db(ns)
    try:
        with storage.connect(db_path, create_ok=False) as conn:
            try:
                payload = storage.get_ticket(conn, ns.id)
            except storage.NotFound as exc:
                raise CliError(str(exc), exit_code=EXIT_NOT_FOUND) from exc
    except storage.LedgerError as exc:
        raise CliError(str(exc), exit_code=EXIT_FAILURE) from exc
    _emit_json(payload)
    return 0


def _handle_ticket_list(ns: argparse.Namespace) -> int:
    db_path = _require_db(ns)
    try:
        with storage.connect(db_path, create_ok=False) as conn:
            payload = storage.list_tickets(conn)
    except storage.LedgerError as exc:
        raise CliError(str(exc), exit_code=EXIT_FAILURE) from exc
    _emit_json(payload)
    return 0


_HANDLERS = {
    ("window", "create"): _handle_window_create,
    ("ticket", "create"): _handle_ticket_create,
    ("ticket", "show"): _handle_ticket_show,
    ("ticket", "list"): _handle_ticket_list,
}


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # 保持既有行为：无参数显示帮助，退出码 0。
    if not argv:
        build_parser().print_help()
        return 0

    parser = build_parser()
    ns = parser.parse_args(list(argv))

    if ns.resource is None:
        parser.error("缺少子命令（window|ticket）")
    if getattr(ns, "action", None) is None:
        parser.error(f"{ns.resource} 缺少子命令")

    handler = _HANDLERS.get((ns.resource, ns.action))
    if handler is None:  # pragma: no cover - argparse 已拦截未知子命令
        parser.error(f"未知子命令: {ns.resource} {ns.action}")

    try:
        return handler(ns)
    except CliError as exc:
        print(f"{PROG}: error: {exc}", file=sys.stderr)
        return exc.exit_code
