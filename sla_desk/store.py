"""SQLite 持久化层：工单登记、查看、合并与列出。

所有写操作都在单个显式事务内完成：业务校验不通过时抛异常并回滚，
数据库不会留下半条记录或被部分改写。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import sla

DB_FILENAME = "sla-desk.db"

STATUS_OPEN = "open"
STATUS_MERGED = "merged"
STATUS_PAUSED = "paused"

VALID_PRIORITIES = ("P1", "P2", "P3", "P4")


class StoreError(Exception):
    """所有业务错误的基类，消息可直接展示给用户。"""


class ValidationError(StoreError):
    """登记字段校验失败，errors 一次性列出全部无效字段。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class TicketNotFound(StoreError):
    """工单号在库中不存在。"""


class InvalidTicketId(StoreError):
    """工单号格式非法（非 T 后接正整数）。"""


class DuplicateConflict(StoreError):
    """同客户请求号重复登记但字段不一致。"""


class MergeConflict(StoreError):
    """合并前置条件不满足（不存在/非 open/同一单/已合并）。"""


class InvalidTimestamp(StoreError):
    """pause/resume 时刻无法解析为 ISO 8601。"""


class PauseResumeError(StoreError):
    """暂停/恢复前置条件不满足（状态不符或时刻乱序）。"""


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    request_no: str
    title: str
    email: str
    priority: str
    submitted_at: str
    status: str
    merged_into: str | None
    merged_at: str | None

    @property
    def is_open(self) -> bool:
        return self.status == STATUS_OPEN


class Store:
    """包裹一个 SQLite 连接的小仓储，数据库文件位于当前工作目录。"""

    def __init__(self, path: str | Path = DB_FILENAME):
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                seq          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id    TEXT NOT NULL UNIQUE,
                request_no   TEXT NOT NULL UNIQUE,
                title        TEXT NOT NULL,
                email        TEXT NOT NULL,
                priority     TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'open',
                merged_into  TEXT,
                merged_at    TEXT,
                FOREIGN KEY (merged_into) REFERENCES tickets(ticket_id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sla_events (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT NOT NULL,
                kind      TEXT NOT NULL,
                at_raw    TEXT NOT NULL,
                FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id)
            )
            """
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE 立即取写锁；块内自行 commit/rollback，异常必回滚。"""
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise

    # ------------------------------------------------------------------
    # 业务方法
    # ------------------------------------------------------------------

    def register(
        self,
        request_no: str,
        title: str,
        email: str,
        priority: str,
        submitted_at: str,
    ) -> tuple[Ticket, bool]:
        """登记工单。

        返回 ``(工单, 是否新建)``。同请求号且标题/邮箱/优先级/提交时间
        文本与首次完全一致时视为重试：不新建、不改写，返回原工单与
        ``False``。任一字段不同则抛 ``DuplicateConflict``。
        """
        errors = validate_fields(title, email, priority, submitted_at)
        if errors:
            raise ValidationError(errors)

        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tickets WHERE request_no = ?", (request_no,)
            ).fetchone()
            if row is not None:
                existing = _row_to_ticket(row)
                same = (
                    existing.title == title
                    and existing.email == email
                    and existing.priority == priority
                    and existing.submitted_at == submitted_at
                )
                if not same:
                    raise DuplicateConflict(
                        f"客户请求号 {request_no} 已登记为 {existing.ticket_id}，"
                        "本次提交字段与首次不一致，拒绝重复登记"
                    )
                # 幂等重试：主动结束事务，原记录保持不变。
                conn.execute("ROLLBACK")
                return existing, False

            ticket_id = next_ticket_id(conn)
            conn.execute(
                """
                INSERT INTO tickets (ticket_id, request_no, title, email,
                                     priority, submitted_at, status)
                VALUES (?, ?, ?, ?, ?, ?, 'open')
                """,
                (ticket_id, request_no, title, email, priority, submitted_at),
            )
            conn.execute("COMMIT")
            return (
                Ticket(
                    ticket_id=ticket_id,
                    request_no=request_no,
                    title=title,
                    email=email,
                    priority=priority,
                    submitted_at=submitted_at,
                    status=STATUS_OPEN,
                    merged_into=None,
                    merged_at=None,
                ),
                True,
            )

    def get(self, ticket_id: str) -> Ticket:
        key = normalize_ticket_id(ticket_id)
        row = self._conn.execute(
            "SELECT * FROM tickets WHERE ticket_id = ?", (key,)
        ).fetchone()
        if row is None:
            raise TicketNotFound(f"工单 {key} 不存在")
        return _row_to_ticket(row)

    def merge(self, source_id: str, target_id: str) -> Ticket:
        """把 ``source`` 工单并入 ``target`` 主工单，返回被并入的工单。

        前置条件：两单都存在、状态均为 open、不是同一单。任一不满足则
        抛 ``MergeConflict`` 且两单状态不变。
        """
        source_key = normalize_ticket_id(source_id)
        target_key = normalize_ticket_id(target_id)
        if source_key == target_key:
            raise MergeConflict("不能将工单并入自身")

        with self._transaction() as conn:
            source = _load(conn, source_key)
            target = _load(conn, target_key)
            if not source.is_open or not target.is_open:
                raise MergeConflict(
                    "只有 open 状态的工单可以合并："
                    f"{source_key} 状态为 {source.status}，"
                    f"{target_key} 状态为 {target.status}"
                )
            merged_at = datetime.now().astimezone().isoformat()
            conn.execute(
                """
                UPDATE tickets
                   SET status = 'merged', merged_into = ?, merged_at = ?
                 WHERE ticket_id = ?
                """,
                (target_key, merged_at, source_key),
            )
            conn.execute("COMMIT")
            return Ticket(
                ticket_id=source.ticket_id,
                request_no=source.request_no,
                title=source.title,
                email=source.email,
                priority=source.priority,
                submitted_at=source.submitted_at,
                status=STATUS_MERGED,
                merged_into=target_key,
                merged_at=merged_at,
            )

    def list_open(self) -> list[Ticket]:
        """只返回 open 工单，按工单号（seq）升序。"""
        rows = self._conn.execute(
            "SELECT * FROM tickets WHERE status = 'open' ORDER BY seq"
        ).fetchall()
        return [_row_to_ticket(row) for row in rows]

    def events(self, ticket_id: str) -> list[tuple[str, str]]:
        """返回工单的 pause/resume 事件（kind, 原始时刻文本），按发生先后排列。"""
        rows = self._conn.execute(
            "SELECT kind, at_raw FROM sla_events WHERE ticket_id = ? ORDER BY id",
            (ticket_id,),
        ).fetchall()
        return [(row["kind"], row["at_raw"]) for row in rows]

    def pause(self, ticket_id: str, at_raw: str) -> Ticket:
        """暂停工单：仅 open 工单可暂停，时刻不早于提交时刻且不早于上一事件。

        时刻文本原样保存。任一前置条件不满足则抛异常、回滚，库中不新增事件。
        """
        return self._add_event(ticket_id, at_raw, "pause")

    def resume(self, ticket_id: str, at_raw: str) -> Ticket:
        """恢复工单：仅 paused 工单可恢复，时刻不早于本次暂停起点。

        时刻文本原样保存。任一前置条件不满足则抛异常、回滚，库中不新增事件。
        """
        return self._add_event(ticket_id, at_raw, "resume")

    def _add_event(self, ticket_id: str, at_raw: str, kind: str) -> Ticket:
        key = normalize_ticket_id(ticket_id)
        try:
            at_abs = sla.parse_iso8601(at_raw).abs_dt
        except (ValueError, TypeError):
            raise InvalidTimestamp(
                f"时刻无法解析，应为 ISO 8601 格式（如 2026-09-24T10:00:00+08:00）：{at_raw!r}"
            )

        with self._transaction() as conn:
            ticket = _load(conn, key)
            if kind == "pause":
                if ticket.status != STATUS_OPEN:
                    raise PauseResumeError(
                        f"只有 open 状态的工单可以暂停：{key} 当前状态为 {ticket.status}"
                    )
                new_status = STATUS_PAUSED
            else:
                if ticket.status != STATUS_PAUSED:
                    raise PauseResumeError(
                        f"只有 paused 状态的工单可以恢复：{key} 当前状态为 {ticket.status}"
                    )
                new_status = STATUS_OPEN

            submitted_abs = sla.parse_iso8601(ticket.submitted_at).abs_dt
            if at_abs < submitted_abs:
                raise PauseResumeError(
                    f"时刻 {at_raw} 早于工单提交时刻 {ticket.submitted_at}，拒绝{kind}"
                )

            last_row = conn.execute(
                "SELECT at_raw FROM sla_events WHERE ticket_id = ? ORDER BY id DESC LIMIT 1",
                (key,),
            ).fetchone()
            if last_row is not None:
                last_abs = sla.parse_iso8601(last_row["at_raw"]).abs_dt
                if at_abs < last_abs:
                    raise PauseResumeError(
                        f"时刻 {at_raw} 早于上一操作时刻 {last_row['at_raw']}，"
                        f"拒绝乱序{kind}"
                    )

            conn.execute(
                "INSERT INTO sla_events (ticket_id, kind, at_raw) VALUES (?, ?, ?)",
                (key, kind, at_raw),
            )
            conn.execute(
                "UPDATE tickets SET status = ? WHERE ticket_id = ?",
                (new_status, key),
            )
            conn.execute("COMMIT")
            return Ticket(
                ticket_id=ticket.ticket_id,
                request_no=ticket.request_no,
                title=ticket.title,
                email=ticket.email,
                priority=ticket.priority,
                submitted_at=ticket.submitted_at,
                status=new_status,
                merged_into=ticket.merged_into,
                merged_at=ticket.merged_at,
            )


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------

def validate_fields(
    title: str, email: str, priority: str, submitted_at: str
) -> list[str]:
    """返回全部无效字段的说明列表；为空表示全部通过。"""
    errors: list[str] = []
    if not title.strip():
        errors.append("标题不能为空")
    if "@" not in email:
        errors.append("报障人邮箱必须包含 @")
    if priority not in VALID_PRIORITIES:
        errors.append("优先级必须是 P1/P2/P3/P4 之一")
    try:
        datetime.fromisoformat(submitted_at)
    except (ValueError, TypeError):
        errors.append(
            "提交时间无法解析，应为 ISO 8601 格式"
            "（如 2026-09-24T10:00:00+08:00）"
        )
    return errors


def normalize_ticket_id(value: str) -> str:
    """把用户输入规整为 T<n>；非法时抛 InvalidTicketId。"""
    text = (value or "").strip().upper()
    if len(text) < 2 or text[0] != "T" or not text[1:].isdigit():
        raise InvalidTicketId(
            f"工单号格式非法：{value!r}，应为 T1、T2 这样的形式"
        )
    number = int(text[1:])
    if number <= 0:
        raise InvalidTicketId(
            f"工单号格式非法：{value!r}，应为 T1、T2 这样的形式"
        )
    return f"T{number}"


def next_ticket_id(conn: sqlite3.Connection) -> str:
    """生成全局递增工单号 T1、T2……（基于自增序列，删除也不回退）。"""
    row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'tickets'"
    ).fetchone()
    return f"T{(row['seq'] if row else 0) + 1}"


def _load(conn: sqlite3.Connection, ticket_id: str) -> Ticket:
    row = conn.execute(
        "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    if row is None:
        raise TicketNotFound(f"工单 {ticket_id} 不存在")
    return _row_to_ticket(row)


def _row_to_ticket(row: sqlite3.Row) -> Ticket:
    return Ticket(
        ticket_id=row["ticket_id"],
        request_no=row["request_no"],
        title=row["title"],
        email=row["email"],
        priority=row["priority"],
        submitted_at=row["submitted_at"],
        status=row["status"],
        merged_into=row["merged_into"],
        merged_at=row["merged_at"],
    )
