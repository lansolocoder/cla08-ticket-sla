"""Command-line entry point."""

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from . import __version__

SLA_MINUTES = {"urgent": 30, "high": 60, "medium": 240, "low": 480}

ESCALATION = {"high": "urgent", "medium": "high", "low": "medium"}

_HHMM = re.compile(r"^([0-2]\d):([0-5]\d)$")


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


def _parse_timestamp(value: str) -> datetime:
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
        type=_parse_timestamp,
        metavar="ISO8601",
        help="受理时刻（带时区偏移的 ISO8601）",
    )
    ticket_create.set_defaults(handler=_ticket_create)

    ticket_pause = ticket_commands.add_parser("pause", help="暂停工单 SLA 计时")
    ticket_pause.add_argument("--db", required=True, help="SQLite 台账路径")
    ticket_pause.add_argument("--id", required=True, help="工单 id")
    ticket_pause.add_argument(
        "--at",
        required=True,
        type=_parse_timestamp,
        metavar="ISO8601",
        help="暂停时刻（带时区偏移的 ISO8601）",
    )
    ticket_pause.set_defaults(handler=_ticket_pause)

    ticket_resume = ticket_commands.add_parser("resume", help="恢复工单 SLA 计时")
    ticket_resume.add_argument("--db", required=True, help="SQLite 台账路径")
    ticket_resume.add_argument("--id", required=True, help="工单 id")
    ticket_resume.add_argument(
        "--at",
        required=True,
        type=_parse_timestamp,
        metavar="ISO8601",
        help="恢复时刻（带时区偏移的 ISO8601）",
    )
    ticket_resume.set_defaults(handler=_ticket_resume)

    ticket_escalate = ticket_commands.add_parser(
        "escalate", help="对已到响应截止的工单执行一次升级"
    )
    ticket_escalate.add_argument("--db", required=True, help="SQLite 台账路径")
    ticket_escalate.add_argument(
        "--at",
        required=True,
        type=_parse_timestamp,
        metavar="ISO8601",
        help="升级判定/生效时刻（带时区偏移的 ISO8601）",
    )
    ticket_escalate.set_defaults(handler=_ticket_escalate)

    ticket_show = ticket_commands.add_parser("show", help="按 id 查询工单")
    ticket_show.add_argument("--db", required=True, help="SQLite 台账路径")
    ticket_show.add_argument("--id", required=True, help="工单 id")
    ticket_show.set_defaults(handler=_ticket_show)

    ticket_list = ticket_commands.add_parser("list", help="列出全部工单")
    ticket_list.add_argument("--db", required=True, help="SQLite 台账路径")
    ticket_list.set_defaults(handler=_ticket_list)

    return parser


_TICKET_EXTRA_COLUMNS = (
    ("paused_at", "TEXT"),
    ("resumed_at", "TEXT"),
    ("consumed_seconds", "INTEGER NOT NULL DEFAULT 0"),
    ("tz_offset_seconds", "INTEGER NOT NULL DEFAULT 0"),
    ("pause_high_water", "TEXT"),
    ("escalated_at", "TEXT"),
)


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
        "paused_at TEXT, "
        "resumed_at TEXT, "
        "consumed_seconds INTEGER NOT NULL DEFAULT 0, "
        "tz_offset_seconds INTEGER NOT NULL DEFAULT 0, "
        "pause_high_water TEXT, "
        "escalated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS escalations ("
        "ticket_id TEXT NOT NULL REFERENCES tickets(id), "
        "seq INTEGER NOT NULL, "
        "at TEXT NOT NULL, "
        "from_priority TEXT NOT NULL, "
        "to_priority TEXT NOT NULL, "
        "PRIMARY KEY (ticket_id, seq))"
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(tickets)")}
    for name, declaration in _TICKET_EXTRA_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE tickets ADD COLUMN {name} {declaration}")
    conn.commit()
    return conn


def _window_bounds(
    day, start_minute: int, end_minute: int, tz
) -> tuple[datetime, datetime]:
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


def _advance_within_windows(
    anchor: datetime, start_minute: int, end_minute: int, remaining: timedelta
) -> datetime:
    """从 anchor 起累计服务时段内 remaining 时长，返回截止时刻。"""
    if remaining <= timedelta(0):
        return anchor
    tz = anchor.tzinfo
    day_start, day_end = _window_bounds(anchor.date(), start_minute, end_minute, tz)
    if day_start <= anchor < day_end:
        cursor = anchor
    elif anchor < day_start:
        cursor = day_start
    else:
        cursor = _window_bounds(
            anchor.date() + timedelta(days=1), start_minute, end_minute, tz
        )[0]

    while True:
        window_start, window_end = _window_bounds(
            cursor.date(), start_minute, end_minute, tz
        )
        available = window_end - cursor
        if remaining <= available:
            return cursor + remaining
        remaining -= available
        cursor = _window_bounds(
            cursor.date() + timedelta(days=1), start_minute, end_minute, tz
        )[0]


def _compute_deadline(
    created: datetime, start_minute: int, end_minute: int, quota_minutes: int
) -> datetime:
    """累计服务时段内时间，跨天时段逐日累加，返回响应截止时刻。"""
    return _advance_within_windows(
        created, start_minute, end_minute, timedelta(minutes=quota_minutes)
    )


def _window_time_between(
    start: datetime, end: datetime, start_minute: int, end_minute: int
) -> timedelta:
    """[start, end] 内落在每日服务时段中的累计时长（两者须同一时区）。"""
    if end <= start:
        return timedelta(0)
    total = timedelta(0)
    day = start.date()
    last = end.date()
    one_day = timedelta(days=1)
    while day <= last:
        window_start, window_end = _window_bounds(
            day, start_minute, end_minute, start.tzinfo
        )
        overlap = min(end, window_end) - max(start, window_start)
        if overlap > timedelta(0):
            total += overlap
        day += one_day
    return total


def _parse_utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


_TICKET_SELECT = (
    "SELECT t.id, t.priority, t.window_id, t.created_at, t.paused_at, t.resumed_at, "
    "t.consumed_seconds, t.tz_offset_seconds, t.pause_high_water, "
    "t.escalated_at, w.start_minute, w.end_minute "
    "FROM tickets t JOIN windows w ON w.id = t.window_id"
)


def _escalations_for(conn: sqlite3.Connection, ticket_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT at, from_priority, to_priority FROM escalations "
        "WHERE ticket_id = ? ORDER BY seq ASC",
        (ticket_id,),
    ).fetchall()
    return [
        {"at": at_text, "from": from_priority, "to": to_priority}
        for at_text, from_priority, to_priority in rows
    ]


def _ticket_payload(row: sqlite3.Row | tuple, escalations: list[dict]) -> dict:
    (
        _id,
        priority,
        window_id,
        created_at,
        paused_at,
        resumed_at,
        consumed_seconds,
        tz_offset_seconds,
        _pause_high_water,
        escalated_at,
        start_minute,
        end_minute,
    ) = row
    tz = timezone(timedelta(seconds=tz_offset_seconds))
    remaining = timedelta(minutes=SLA_MINUTES[priority]) - timedelta(
        seconds=consumed_seconds
    )
    if paused_at is not None:
        state = "paused"
        # 暂停期间升级时以升级时刻为冻结锚点；升级后再次暂停则以新暂停时刻为准
        anchor_text = max(
            text for text in (paused_at, escalated_at) if text is not None
        )
        anchor = _parse_utc(anchor_text).astimezone(tz)
    else:
        state = "running"
        # 运行中升级以升级时刻重新起算；升级后又经历 pause/resume 则以 resumed_at 为准
        anchor_text = max(
            text
            for text in (created_at, resumed_at, escalated_at)
            if text is not None
        )
        anchor = _parse_utc(anchor_text).astimezone(tz)
    deadline = _advance_within_windows(anchor, start_minute, end_minute, remaining)
    return {
        "id": _id,
        "priority": priority,
        "window_id": window_id,
        "created_at": created_at,
        "deadline": _format_utc(deadline),
        "state": state,
        "paused_at": paused_at,
        "resumed_at": resumed_at,
        "escalations": escalations,
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

        deadline = _compute_deadline(
            args.created_at, row[0], row[1], SLA_MINUTES[args.priority]
        )
        created_at_text = _format_utc(args.created_at)
        deadline_text = _format_utc(deadline)
        tz_offset_seconds = int(args.created_at.utcoffset().total_seconds())

        try:
            with conn:
                conn.execute(
                    "INSERT INTO tickets "
                    "(id, priority, window_id, created_at, deadline, tz_offset_seconds) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        args.id,
                        args.priority,
                        args.window_id,
                        created_at_text,
                        deadline_text,
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
        _TICKET_SELECT + " WHERE t.id = ?", (ticket_id,)
    ).fetchone()


def _ticket_show(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
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
        payload = _ticket_payload(row, _escalations_for(conn, args.id))
    finally:
        conn.close()

    print(json.dumps(payload, separators=(",", ":")))
    return 0


def _ticket_list(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        conn = _connect(args.db)
    except sqlite3.Error as exc:
        print(f"sla-desk: error: 无法打开台账 {args.db!r}: {exc}", file=sys.stderr)
        return 1
    try:
        rows = conn.execute(
            _TICKET_SELECT + " ORDER BY t.created_at ASC, t.id ASC"
        ).fetchall()
        payloads = [
            _ticket_payload(row, _escalations_for(conn, row[0])) for row in rows
        ]
    finally:
        conn.close()

    print(json.dumps(payloads, separators=(",", ":")))
    return 0


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

        payload = _ticket_payload(row, _escalations_for(conn, args.id))
        if payload["state"] == "paused":
            print(
                f"sla-desk: error: 工单 {args.id!r} 已处于暂停状态",
                file=sys.stderr,
            )
            return 2

        at_utc = args.at.astimezone(timezone.utc)
        if at_utc < _parse_utc(payload["created_at"]):
            print(
                f"sla-desk: error: --at 早于工单 {args.id!r} 的受理时刻",
                file=sys.stderr,
            )
            return 2
        if at_utc > _parse_utc(payload["deadline"]):
            print(
                f"sla-desk: error: --at 晚于工单 {args.id!r} 的当前响应截止",
                file=sys.stderr,
            )
            return 2

        (
            _id,
            priority,
            _window_id,
            created_at,
            _paused_at,
            resumed_at,
            consumed_seconds,
            tz_offset_seconds,
            pause_high_water,
            escalated_at,
            start_minute,
            end_minute,
        ) = row
        tz = timezone(timedelta(seconds=tz_offset_seconds))
        anchor_text = max(
            text
            for text in (created_at, resumed_at, escalated_at)
            if text is not None
        )
        anchor = _parse_utc(anchor_text).astimezone(tz)
        consumed = consumed_seconds + int(
            _window_time_between(
                anchor, at_utc.astimezone(tz), start_minute, end_minute
            ).total_seconds()
        )
        at_text = _format_utc(at_utc)
        if pause_high_water is None or at_text > pause_high_water:
            pause_high_water = at_text
        remaining = timedelta(minutes=SLA_MINUTES[priority]) - timedelta(
            seconds=consumed
        )
        frozen = _advance_within_windows(
            at_utc.astimezone(tz), start_minute, end_minute, remaining
        )

        with conn:
            conn.execute(
                "UPDATE tickets SET paused_at = ?, consumed_seconds = ?, "
                "pause_high_water = ?, deadline = ?, escalated_at = NULL "
                "WHERE id = ?",
                (at_text, consumed, pause_high_water, _format_utc(frozen), args.id),
            )
        payload = _ticket_payload(
            _fetch_ticket(conn, args.id), _escalations_for(conn, args.id)
        )
    except sqlite3.Error as exc:
        print(f"sla-desk: error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(json.dumps(payload, separators=(",", ":")))
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

        payload = _ticket_payload(row, _escalations_for(conn, args.id))
        if payload["state"] != "paused":
            print(
                f"sla-desk: error: 工单 {args.id!r} 未处于暂停状态",
                file=sys.stderr,
            )
            return 2

        at_utc = args.at.astimezone(timezone.utc)
        pause_high_water = row[8]
        if at_utc < _parse_utc(pause_high_water):
            print(
                f"sla-desk: error: --at 早于工单 {args.id!r} 的暂停时刻",
                file=sys.stderr,
            )
            return 2

        (
            _id,
            priority,
            _window_id,
            _created_at,
            _paused_at,
            _resumed_at,
            consumed_seconds,
            tz_offset_seconds,
            _high_water,
            _escalated_at,
            start_minute,
            end_minute,
        ) = row
        tz = timezone(timedelta(seconds=tz_offset_seconds))
        remaining = timedelta(minutes=SLA_MINUTES[priority]) - timedelta(
            seconds=consumed_seconds
        )
        deadline = _advance_within_windows(
            at_utc.astimezone(tz), start_minute, end_minute, remaining
        )
        at_text = _format_utc(at_utc)

        with conn:
            conn.execute(
                "UPDATE tickets SET paused_at = NULL, resumed_at = ?, deadline = ?, "
                "escalated_at = NULL WHERE id = ?",
                (at_text, _format_utc(deadline), args.id),
            )
        payload = _ticket_payload(
            _fetch_ticket(conn, args.id), _escalations_for(conn, args.id)
        )
    except sqlite3.Error as exc:
        print(f"sla-desk: error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(json.dumps(payload, separators=(",", ":")))
    return 0


def _ticket_escalate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        conn = _connect(args.db)
    except sqlite3.Error as exc:
        print(f"sla-desk: error: 无法打开台账 {args.db!r}: {exc}", file=sys.stderr)
        return 1
    try:
        rows = conn.execute(_TICKET_SELECT + " ORDER BY t.id ASC").fetchall()
        at_utc = args.at.astimezone(timezone.utc)
        at_text = _format_utc(at_utc)

        candidates = []
        for row in rows:
            (
                _id,
                priority,
                _window_id,
                created_at,
                paused_at,
                _resumed_at,
                _consumed_seconds,
                tz_offset_seconds,
                pause_high_water,
                _escalated_at,
                start_minute,
                end_minute,
            ) = row
            # urgent 为顶级，不再升级
            if priority not in ESCALATION:
                continue
            # --at 早于受理时刻的工单跳过
            if at_utc < _parse_utc(created_at):
                continue
            payload = _ticket_payload(row, _escalations_for(conn, _id))
            # 到期判定按当前记录重算的有效截止，与状态无关
            if at_utc < _parse_utc(payload["deadline"]):
                continue
            candidates.append(
                (
                    _id,
                    priority,
                    paused_at,
                    tz_offset_seconds,
                    pause_high_water,
                    start_minute,
                    end_minute,
                )
            )

        results = []
        with conn:
            for (
                ticket_id,
                old_priority,
                paused_at,
                tz_offset_seconds,
                pause_high_water,
                start_minute,
                end_minute,
            ) in candidates:
                new_priority = ESCALATION[old_priority]
                quota = timedelta(minutes=SLA_MINUTES[new_priority])
                tz = timezone(timedelta(seconds=tz_offset_seconds))
                new_deadline = _advance_within_windows(
                    at_utc.astimezone(tz), start_minute, end_minute, quota
                )
                new_deadline_text = _format_utc(new_deadline)
                high_water = pause_high_water
                if high_water is None or at_text > high_water:
                    high_water = at_text

                seq_row = conn.execute(
                    "SELECT COUNT(*) FROM escalations WHERE ticket_id = ?",
                    (ticket_id,),
                ).fetchone()
                conn.execute(
                    "INSERT INTO escalations "
                    "(ticket_id, seq, at, from_priority, to_priority) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (ticket_id, seq_row[0] + 1, at_text, old_priority, new_priority),
                )
                if paused_at is not None:
                    # 暂停中：paused_at/resumed_at 不变，以升级时刻冻结新截止
                    conn.execute(
                        "UPDATE tickets SET priority = ?, consumed_seconds = 0, "
                        "deadline = ?, escalated_at = ?, pause_high_water = ? "
                        "WHERE id = ?",
                        (
                            new_priority,
                            new_deadline_text,
                            at_text,
                            high_water,
                            ticket_id,
                        ),
                    )
                else:
                    # 运行中：从 --at 起按新额度满额重算
                    conn.execute(
                        "UPDATE tickets SET priority = ?, consumed_seconds = 0, "
                        "deadline = ?, escalated_at = ? WHERE id = ?",
                        (new_priority, new_deadline_text, at_text, ticket_id),
                    )
                results.append(
                    {
                        "id": ticket_id,
                        "escalations": [
                            {"at": at_text, "from": old_priority, "to": new_priority}
                        ],
                    }
                )
    except sqlite3.Error as exc:
        print(f"sla-desk: error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    results.sort(key=lambda item: item["id"])
    print(json.dumps(results, separators=(",", ":")))
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
        parser.error("缺少子命令（window create / ticket create|pause|resume|escalate|show|list）")
    return args.handler(args, parser)
