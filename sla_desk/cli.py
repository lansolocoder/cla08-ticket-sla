"""Command-line entry point."""

import argparse
import sys
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime

from . import __version__
from . import storage
from .storage import TicketError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sla-desk",
        description="Local 工单 SLA 与升级路由 ledger.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    register = subparsers.add_parser("register", help="登记一个新工单")
    register.add_argument("--customer-ref", required=True, help="客户请求号")
    register.add_argument("--title", required=True, help="工单标题")
    register.add_argument("--email", required=True, help="报障人邮箱")
    register.add_argument("--priority", required=True, help="优先级 P1/P2/P3/P4")
    register.add_argument(
        "--submitted-at", required=True, help="提交时间（ISO 8601，可带时区偏移）"
    )

    show = subparsers.add_parser("show", help="按工单号查看工单")
    show.add_argument("ticket_no", help="工单号，例如 T1")

    merge = subparsers.add_parser(
        "merge", help="将一个工单并入另一个工单（不可逆）"
    )
    merge.add_argument("ticket_no", help="被并入的工单号，例如 T2")
    merge.add_argument("master_no", help="保留的主工单号，例如 T1")

    subparsers.add_parser("list", help="列出所有 open 工单（按工单号升序）")
    return parser


def _cmd_register(args: argparse.Namespace, out) -> int:
    # Validation (including priority) happens in storage so that every invalid
    # field can be reported in one error instead of argparse stopping at the first.
    with closing(storage.connect()) as conn:
        result = storage.register_ticket(
            conn,
            customer_ref=args.customer_ref,
            title=args.title,
            reporter_email=args.email,
            priority=args.priority,
            submitted_at=args.submitted_at,
        )
    ticket = result.ticket
    print(f"工单号: {ticket.ticket_no}", file=out)
    print(f"状态: {ticket.status}", file=out)
    print(f"提交时间: {ticket.submitted_at}", file=out)
    return 0


def _cmd_show(args: argparse.Namespace, out) -> int:
    with closing(storage.connect()) as conn:
        ticket = storage.get_ticket(conn, args.ticket_no)
    if ticket is None:
        raise TicketError(f"工单不存在: {args.ticket_no}")
    print(f"工单号: {ticket.ticket_no}", file=out)
    print(f"客户请求号: {ticket.customer_ref}", file=out)
    print(f"标题: {ticket.title}", file=out)
    print(f"报障人: {ticket.reporter_email}", file=out)
    print(f"优先级: {ticket.priority}", file=out)
    print(f"提交时间: {ticket.submitted_at}", file=out)
    print(f"状态: {ticket.status}", file=out)
    if ticket.status == storage.STATUS_MERGED:
        print(f"主工单号: {ticket.merged_into}", file=out)
        print(f"合并时间: {ticket.merged_at}", file=out)
    return 0


def _cmd_merge(args: argparse.Namespace, out) -> int:
    merged_at = datetime.now().astimezone().isoformat(timespec="seconds")
    with closing(storage.connect()) as conn:
        ticket = storage.merge_tickets(
            conn,
            merged_no=args.ticket_no,
            master_no=args.master_no,
            merged_at=merged_at,
        )
    print(f"工单 {ticket.ticket_no} 已并入主工单 {ticket.merged_into}", file=out)
    print(f"状态: {ticket.status}", file=out)
    print(f"合并时间: {ticket.merged_at}", file=out)
    return 0


def _cmd_list(args: argparse.Namespace, out) -> int:
    with closing(storage.connect()) as conn:
        tickets = storage.list_open_tickets(conn)
    for ticket in tickets:
        print(
            "\t".join(
                (ticket.ticket_no, ticket.priority, title_for(ticket), ticket.status)
            ),
            file=out,
        )
    return 0


def title_for(ticket: storage.Ticket) -> str:
    """Keep list output one record per line even if a title contains a tab/newline."""
    return ticket.title.replace("\t", " ").replace("\n", " ")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    handlers = {
        "register": _cmd_register,
        "show": _cmd_show,
        "merge": _cmd_merge,
        "list": _cmd_list,
    }
    try:
        return handlers[args.command](args, sys.stdout)
    except TicketError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
