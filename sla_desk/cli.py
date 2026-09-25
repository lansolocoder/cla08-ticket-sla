"""Command-line entry point."""

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from . import __version__

SLA_MINUTES = {"high": 60, "medium": 240, "low": 480}

_HHMM = re.compile(r"^([0-2]\d):([0-5]\d)$")

_TICKET_FIELDS = (
    "id, priority, window_id, created_at, deadline, state, paused_at, resumed_at"
)


def _parse_hhmm(value: str) -> int:
    match = _HHMM.match(value)
    if match is None:
        raise argparse.ArgumentTypeError(
            f"invalid time {value!r}; expected HH:MM"
        )
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23:
        raise argparse.ArgumentTypeError(
            f"invalid time {value!r}; hour must be within 00-23"
        )
    return hour * 60 + minute


def _parse_iso8601(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid timestamp {value!r}; expected ISO8601 with offset, "
            "e.g. 2026-03-02T09:15:00+08:00"
        ) from None
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(
            f"invalid timestamp {value!r}; timezone offset is required"
        )
    return parsed


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sla-desk",
        description="Local 工单 SLA 与升级路由 ledger.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    groups = parser.add_subparsers(dest="group")

    window_parser = groups.add_parser("window", help="管理服务时段")
    window_commands = window_parser.add_subparsers(dest="command", required=True)

    window_create = window_commands.add_parser("create", help="登记每日服务时段")
    window_create.add_argument("--db", required=True, help="SQLite 台账路径")
    window_create.add_argument("--id", required=True, help="时段 id")
    window_create.add_argument(
        "--start", required=True, type=_parse_hhmm, metavar="HH:MM", help="每日起点（本地时间）"
    )
    window_create.add_argument(
        "--end", required=True, type=_parse_hhmm, metavar="HH:MM", help="每日终点（本地时间）"
    )
    window_create.set_defaults(handler=_window_create)

    ticket_parser = groups.add_parser("ticket", help="管理工单")
    ticket_commands = ticket_parser.add_subparsers(dest="command", required=True)

    ticket_create = ticket_commands.add_parser("create", help="登记工单")
    ticket_create.add_argument("--db", required=True, help="SQLite 台账路径")
    ticket_create.add_argument("--id", required=True, help="工单 id")
    ticket_create.add_argument(
        "--priority",
        required=True,
        choices=("high", "medium", "low"),
        help="SLA 优先级（high|medium|low）",
    )
    ticket_create.add_argument(
        "--window-id", required=True, dest="window_id", help="引用的服务时段 id"
    )
    ticket_create.add_argument(
        "--created-at",
        required=True,
        dest="created_at",
        type=_parse_iso8601,
        metavar="ISO8601",
        help="受理时刻（带时区偏移的 ISO8601）",
    )
    ticket_create.set_defaults(handler=_ticket_create)

    ticket_show = ticket_commands.add_parser("show", help="按 id 查询工单")
    ticket_show.add_argument("--db", required=True, help="SQLite 台账路径")
    ticket_show.add_argument("--id", required=True, help="工单 id")
    ticket_show.set_defaults(handler=_ticket_show)

    ticket_list = ticket_commands.add_parser("list", help="列出全部工单")
    ticket_list.add_argument("--db", required=True, help="SQLite 台账路径")
    ticket_list.set_defaults(handler=_ticket_list)

    ticket_pause = ticket_commands.add_parser("pause", help="暂停工单 SLA 计时")
    ticket_pause.add_argument("--db", required=True, help="SQLite 台账路径")
    ticket_pause.add_argument("--id", required=True, help="工单 id")
    ticket_pause.add_argument(
        "--at",
        required=True,
        type=_parse_iso8601,
        metavar="ISO8601",
        help="暂停生效时刻（带时区偏移的 ISO8601）",
    )
    ticket_pause.set_defaults(handler=_ticket_pause)

    ticket_resume = ticket_commands.add_parser("resume", help="恢复工单 SLA 计时")
    ticket_resume.add_argument("--db", required=True, help="SQLite 台账路径")
    ticket_resume.add_argument("--id", required=True, help="工单 id")
    ticket_resume.add_argument(
        "--at",
        required=True,
        type=_parse_iso8601,
        metavar="ISO8601",
        help="恢复生效时刻（带时区偏移的 ISO8601）",
    )
    ticket_resume.set_defaults(handler=_ticket_resume)

    return parser


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS windows ("
        "id TEXT PRIMARY KEY, start_minute INTEGER NOT NULL, end_minute INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tickets ("
        "id TEXT PRIMARY KEY, "
        "priority TEXT NOT NULL, "
        "window_id TEXT NOT NULL REFERENCES windows(id), "
        "created_at TEXT NOT NULL, "
        "deadline TEXT NOT NULL, "
        "state TEXT NOT NULL DEFAULT 'running', "
        "paused_at TEXT, "
        "resumed_at TEXT, "
        "consumed_seconds INTEGER NOT NULL DEFAULT 0, "
        "segment_start TEXT, "
        "tz_offset_seconds INTEGER NOT NULL DEFAULT 0)"
    )
    # 旧版台账迁移：补齐暂停/恢复所需的列。
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tickets)")}
    migrations = [
        ("state", "ALTER TABLE tickets ADD COLUMN state TEXT NOT NULL DEFAULT 'running'"),
        ("paused_at", "ALTER TABLE tickets ADD COLUMN paused_at TEXT"),
        ("resumed_at", "ALTER TABLE tickets ADD COLUMN resumed_at TEXT"),
        ("consumed_seconds",
         "ALTER TABLE tickets ADD COLUMN consumed_seconds INTEGER NOT NULL DEFAULT 0"),
        ("segment_start", "ALTER TABLE tickets ADD COLUMN segment_start TEXT"),
        ("tz_offset_seconds",
         "ALTER TABLE tickets ADD COLUMN tz_offset_seconds INTEGER NOT NULL DEFAULT 0"),
    ]
    for name, statement in migrations:
        if name not in columns:
            conn.execute(statement)
    conn.execute(
        "UPDATE tickets SET segment_start = created_at WHERE segment_start IS NULL"
    )
    conn.commit()
    return conn


def _compute_deadline(
    moment: datetime,
    start_minute: int,
    end_minute: int,
    quota_seconds: int,
    tz: timezone,
) -> datetime:
    """从 moment 起累计服务时段内时间，跨天时段逐日累加，返回响应截止时刻。"""
    moment = moment.astimezone(tz)

    def bounds(day) -> tuple[datetime, datetime]:
        start = datetime(
            day.year,
            day.month,
            day.day,
            start_minute // 60,
            start_minute % 60,
            tzinfo=tz,
        )
        end = datetime(
            day.year,
            day.month,
            day.day,
            end_minute // 60,
            end_minute % 60,
            tzinfo=tz,
        )
        return start, end

    day_start, day_end = bounds(moment.date())
    if day_start <= moment < day_end:
        cursor = moment
    elif moment < day_start:
        cursor = day_start
    else:
        cursor = bounds(moment.date() + timedelta(days=1))[0]

    remaining = timedelta(seconds=quota_seconds)
    while True:
        window_start, window_end = bounds(cursor.date())
        available = window_end - cursor
        if remaining <= available:
            return cursor + remaining
        remaining -= available
        cursor = bounds(cursor.date() + timedelta(days=1))[0]


def _service_seconds_between(
    start: datetime, end: datetime, start_minute: int, end_minute: int, tz: timezone
) -> int:
    """[start, end) 内落在每日服务时段中的秒数。"""
    start = start.astimezone(tz)
    end = end.astimezone(tz)
    if end <= start:
        return 0
    total = 0
    day = start.date()
    while True:
        window_start = datetime(
            day.year, day.month, day.day,
            start_minute // 60, start_minute % 60, tzinfo=tz,
        )
        window_end = datetime(
            day.year, day.month, day.day,
            end_minute // 60, end_minute % 60, tzinfo=tz,
        )
        low = max(start, window_start)
        high = min(end, window_end)
        if high > low:
            total += int((high - low).total_seconds())
        if end <= window_end:
            return total
        day += timedelta(days=1)


def _ticket_json(row: sqlite3.Row | tuple) -> dict:
    _id, priority, window_id, created_at, deadline, state, paused_at, resumed_at = row
    return {
        "id": _id,
        "priority": priority,
        "window_id": window_id,
        "created_at": created_at,
        "deadline": deadline,
        "state": state,
        "paused_at": paused_at,
        "resumed_at": resumed_at,
    }


def _window_create(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.end <= args.start:
        parser.error("--end 必须晚于 --start，且服务时段不得跨零点")

    try:
        conn = _connect(args.db)
    except sqlite3.Error as exc:
        print(f"sla-desk: error: 无法打开台账 {args.db!r}: {exc}", file=sys.stderr)
        return 1
    try:
        with conn:
            conn.execute(
                "INSERT INTO windows (id, start_minute, end_minute) VALUES (?, ?, ?)",
                (args.id, args.start, args.end),
            )
    except sqlite3.IntegrityError:
        print(f"sla-desk: error: 窗口 id {args.id!r} 已存在", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"sla-desk: error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def _ticket_create(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        conn = _connect(args.db)
    except sqlite3.Error as exc:
        print(f"sla-desk: error: 无法打开台账 {args.db!r}: {exc}", file=sys.stderr)
        return 1
    try:
        row = conn.execute(
            "SELECT start_minute, end_minute FROM windows WHERE id = ?",
            (args.window_id,),
        ).fetchone()
        if row is None:
            print(
                f"sla-desk: error: 服务时段 {args.window_id!r} 不存在",
                file=sys.stderr,
            )
            return 1

        tz = args.created_at.tzinfo
        deadline = _compute_deadline(
            args.created_at, row[0], row[1], SLA_MINUTES[args.priority] * 60, tz
        )
        created_at_text = _format_utc(args.created_at)
        deadline_text = _format_utc(deadline)
        offset = args.created_at.utcoffset()
        tz_offset_seconds = int(offset.total_seconds()) if offset is not None else 0

        try:
            with conn:
                conn.execute(
                    "INSERT INTO tickets (id, priority, window_id, created_at, deadline, "
                    "segment_start, tz_offset_seconds) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        args.id,
                        args.priority,
                        args.window_id,
                        created_at_text,
                        deadline_text,
                        created_at_text,
                        tz_offset_seconds,
                    ),
                )
        except sqlite3.IntegrityError:
            print(f"sla-desk: error: 工单 id {args.id!r} 已存在", file=sys.stderr)
            return 2
    except sqlite3.Error as exc:
        print(f"sla-desk: error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(
        json.dumps(
            {
                "id": args.id,
                "priority": args.priority,
                "window_id": args.window_id,
                "created_at": created_at_text,
                "deadline": deadline_text,
            },
            separators=(",", ":"),
        )
    )
    return 0


def _fetch_ticket(conn: sqlite3.Connection, ticket_id: str):
    return conn.execute(
        "SELECT priority, window_id, created_at, deadline, state, paused_at, "
        "resumed_at, consumed_seconds, segment_start, tz_offset_seconds "
        "FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()


def _fetch_window(conn: sqlite3.Connection, window_id: str):
    return conn.execute(
        "SELECT start_minute, end_minute FROM windows WHERE id = ?",
        (window_id,),
    ).fetchone()


def _ticket_pause(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        conn = _connect(args.db)
    except sqlite3.Error as exc:
        print(f"sla-desk: error: 无法打开台账 {args.db!r}: {exc}", file=sys.stderr)
        return 1
    try:
        row = _fetch_ticket(conn, args.id)
        if row is None:
            print(f"sla-desk: error: 工单 {args.id!r} 不存在", file=sys.stderr)
            return 1
        (priority, window_id, created_at, deadline, state, _paused_at,
         resumed_at, consumed, segment_start, tz_offset) = row
        if state == "paused":
            print(
                f"sla-desk: error: 工单 {args.id!r} 已处于暂停状态",
                file=sys.stderr,
            )
            return 2

        created = datetime.fromisoformat(created_at)
        current_deadline = datetime.fromisoformat(deadline)
        at = args.at.astimezone(timezone.utc)
        if at < created:
            print(
                f"sla-desk: error: --at 早于受理时刻 {created_at}",
                file=sys.stderr,
            )
            return 2
        if at > current_deadline:
            print(
                f"sla-desk: error: --at 晚于当前 deadline {deadline}",
                file=sys.stderr,
            )
            return 2
        segment_start_dt = datetime.fromisoformat(segment_start)
        if at < segment_start_dt:
            print(
                f"sla-desk: error: --at 早于当前计时段起点 {segment_start}",
                file=sys.stderr,
            )
            return 2

        window = _fetch_window(conn, window_id)
        if window is None:
            print(
                f"sla-desk: error: 服务时段 {window_id!r} 不存在",
                file=sys.stderr,
            )
            return 1
        tz = timezone(timedelta(seconds=tz_offset))
        consumed += _service_seconds_between(
            segment_start_dt, at, window[0], window[1], tz
        )

        at_text = _format_utc(at)
        with conn:
            conn.execute(
                "UPDATE tickets SET state = 'paused', paused_at = ?, "
                "consumed_seconds = ? WHERE id = ?",
                (at_text, consumed, args.id),
            )
    except sqlite3.Error as exc:
        print(f"sla-desk: error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(
        json.dumps(
            {
                "id": args.id,
                "priority": priority,
                "window_id": window_id,
                "created_at": created_at,
                "deadline": deadline,
                "state": "paused",
                "paused_at": at_text,
                "resumed_at": resumed_at,
            },
            separators=(",", ":"),
        )
    )
    return 0


def _ticket_resume(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        conn = _connect(args.db)
    except sqlite3.Error as exc:
        print(f"sla-desk: error: 无法打开台账 {args.db!r}: {exc}", file=sys.stderr)
        return 1
    try:
        row = _fetch_ticket(conn, args.id)
        if row is None:
            print(f"sla-desk: error: 工单 {args.id!r} 不存在", file=sys.stderr)
            return 1
        (priority, window_id, created_at, _deadline, state, paused_at,
         _resumed_at, consumed, _segment_start, tz_offset) = row
        if state != "paused":
            print(
                f"sla-desk: error: 工单 {args.id!r} 未处于暂停状态",
                file=sys.stderr,
            )
            return 2

        paused = datetime.fromisoformat(paused_at)
        at = args.at.astimezone(timezone.utc)
        if at < paused:
            print(
                f"sla-desk: error: --at 早于暂停时刻 {paused_at}",
                file=sys.stderr,
            )
            return 2

        window = _fetch_window(conn, window_id)
        if window is None:
            print(
                f"sla-desk: error: 服务时段 {window_id!r} 不存在",
                file=sys.stderr,
            )
            return 1
        tz = timezone(timedelta(seconds=tz_offset))
        remaining = SLA_MINUTES[priority] * 60 - consumed
        new_deadline = _compute_deadline(at, window[0], window[1], remaining, tz)

        at_text = _format_utc(at)
        deadline_text = _format_utc(new_deadline)
        with conn:
            conn.execute(
                "UPDATE tickets SET state = 'running', paused_at = NULL, "
                "resumed_at = ?, deadline = ?, segment_start = ? WHERE id = ?",
                (at_text, deadline_text, at_text, args.id),
            )
    except sqlite3.Error as exc:
        print(f"sla-desk: error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(
        json.dumps(
            {
                "id": args.id,
                "priority": priority,
                "window_id": window_id,
                "created_at": created_at,
                "deadline": deadline_text,
                "state": "running",
                "paused_at": None,
                "resumed_at": at_text,
            },
            separators=(",", ":"),
        )
    )
    return 0


def _ticket_show(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        conn = _connect(args.db)
    except sqlite3.Error as exc:
        print(f"sla-desk: error: 无法打开台账 {args.db!r}: {exc}", file=sys.stderr)
        return 1
    try:
        row = conn.execute(
            f"SELECT {_TICKET_FIELDS} FROM tickets WHERE id = ?",
            (args.id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        print(f"sla-desk: error: 工单 {args.id!r} 不存在", file=sys.stderr)
        return 1
    print(json.dumps(_ticket_json(row), separators=(",", ":")))
    return 0


def _ticket_list(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        conn = _connect(args.db)
    except sqlite3.Error as exc:
        print(f"sla-desk: error: 无法打开台账 {args.db!r}: {exc}", file=sys.stderr)
        return 1
    try:
        rows = conn.execute(
            f"SELECT {_TICKET_FIELDS} "
            "FROM tickets ORDER BY created_at ASC, id ASC"
        ).fetchall()
    finally:
        conn.close()

    print(json.dumps([_ticket_json(row) for row in rows], separators=(",", ":")))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) == 0:
        build_parser().print_help()
        return 0

    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.error("缺少子命令（window create / ticket create|show|list|pause|resume）")
    return args.handler(args, parser)
