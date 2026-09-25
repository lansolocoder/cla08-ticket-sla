"""Command-line entry point."""

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime

from . import __version__, sla
from .store import DB_FILENAME, Store, StoreError, ValidationError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sla-desk",
        description="Local 工单 SLA 与升级路由 ledger.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    register = subparsers.add_parser(
        "register", help="登记一张新工单"
    )
    register.add_argument("--request-no", required=True, help="客户请求号")
    register.add_argument("--title", required=True, help="工单标题")
    register.add_argument("--email", required=True, help="报障人邮箱")
    register.add_argument(
        "--priority", required=True,
        help="优先级 P1/P2/P3/P4（其余值视为无效字段）",
    )
    register.add_argument(
        "--submitted-at", required=True,
        help="提交时间（ISO 8601，可带时区偏移，如 2026-09-24T10:00:00+08:00）",
    )

    show = subparsers.add_parser("show", help="按工单号查看工单详情")
    show.add_argument("ticket_id", metavar="T<n>", help="工单号，如 T1")

    merge = subparsers.add_parser(
        "merge", help="把一张工单并入另一张工单（被并工单状态变为 merged）"
    )
    merge.add_argument("ticket_id", metavar="被并工单", help="要被并入的工单号，如 T2")
    merge.add_argument("master_id", metavar="主工单", help="保留的主工单号，如 T1")

    pause = subparsers.add_parser("pause", help="暂停工单计时（仅 open 工单）")
    pause.add_argument("ticket_id", metavar="T<n>", help="工单号，如 T1")
    pause.add_argument(
        "at", metavar="时刻",
        help="暂停时刻（ISO 8601，可带偏移，如 2026-09-24T10:05:00+08:00）",
    )

    resume = subparsers.add_parser("resume", help="恢复工单计时（仅 paused 工单）")
    resume.add_argument("ticket_id", metavar="T<n>", help="工单号，如 T1")
    resume.add_argument(
        "at", metavar="时刻",
        help="恢复时刻（ISO 8601，可带偏移，如 2026-09-24T11:30:00+08:00）",
    )

    subparsers.add_parser("list", help="列出所有 open 状态的工单（按工单号升序）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    try:
        with Store(DB_FILENAME) as store:
            if args.command == "register":
                return _cmd_register(store, args)
            if args.command == "show":
                return _cmd_show(store, args)
            if args.command == "merge":
                return _cmd_merge(store, args)
            if args.command == "pause":
                return _cmd_pause(store, args)
            if args.command == "resume":
                return _cmd_resume(store, args)
            if args.command == "list":
                return _cmd_list(store)
    except ValidationError as exc:
        print("登记失败，以下字段无效：", file=sys.stderr)
        for error in exc.errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    except StoreError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_register(store: Store, args: argparse.Namespace) -> int:
    ticket, created = store.register(
        request_no=args.request_no,
        title=args.title,
        email=args.email,
        priority=args.priority,
        submitted_at=args.submitted_at,
    )
    if not created:
        print("检测到重复请求的重试，返回原工单：")
    print(f"工单号: {ticket.ticket_id}")
    print(f"状态: {ticket.status}")
    print(f"提交时间: {ticket.submitted_at}")
    return 0


def _cmd_show(store: Store, args: argparse.Namespace) -> int:
    ticket = store.get(args.ticket_id)
    print(f"工单号: {ticket.ticket_id}")
    print(f"客户请求号: {ticket.request_no}")
    print(f"标题: {ticket.title}")
    print(f"报障人: {ticket.email}")
    print(f"优先级: {ticket.priority}")
    print(f"提交时间: {ticket.submitted_at}")
    print(f"状态: {ticket.status}")
    if ticket.merged_into is not None:
        print(f"主工单号: {ticket.merged_into}")
        if ticket.merged_at:
            print(f"合并时间: {ticket.merged_at}")
    view = sla.compute_sla(
        ticket.priority,
        ticket.submitted_at,
        store.events(ticket.ticket_id),
        paused=ticket.status == "paused",
        now=datetime.now().astimezone(),
    )
    print(f"响应截止: {view.deadline_text}")
    print(f"已耗时: {view.elapsed_minutes} 分钟")
    print(f"剩余: {view.remaining_minutes} 分钟")
    return 0


def _cmd_pause(store: Store, args: argparse.Namespace) -> int:
    ticket = store.pause(args.ticket_id, args.at)
    print(f"工单号: {ticket.ticket_id}")
    print(f"状态: {ticket.status}")
    print(f"暂停时刻: {args.at}")
    return 0


def _cmd_resume(store: Store, args: argparse.Namespace) -> int:
    ticket = store.resume(args.ticket_id, args.at)
    print(f"工单号: {ticket.ticket_id}")
    print(f"状态: {ticket.status}")
    print(f"恢复时刻: {args.at}")
    return 0


def _cmd_merge(store: Store, args: argparse.Namespace) -> int:
    merged = store.merge(args.ticket_id, args.master_id)
    print(f"工单号: {merged.ticket_id}")
    print(f"状态: {merged.status}")
    print(f"主工单号: {merged.merged_into}")
    print(f"合并时间: {merged.merged_at}")
    return 0


def _cmd_list(store: Store) -> int:
    for ticket in store.list_open():
        print("\t".join(
            (ticket.ticket_id, ticket.priority, ticket.title, ticket.status)
        ))
    return 0
