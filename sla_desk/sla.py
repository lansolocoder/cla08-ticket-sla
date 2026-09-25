"""SLA 响应计时：按优先级目标时长与服务时段累计。

- 优先级响应目标：P1=15 分钟、P2=30 分钟、P3=4 小时、P4=1 个工作日（=8 服务小时）。
- 服务时段：周一至周五 09:00–18:00；落在服务时段外（夜晚、周末）的时长不计入，
  P1–P4 的目标时长都只在服务时段内消耗。
- 时间解释：每个时刻按其自带的时区偏移解释；无偏移按本地时间。服务窗口按
  *提交时刻所在偏移帧* 的墙钟挂到时间轴上（例如提交带 ``+08:00``，则服务窗口
  是工作日北京时间 09:00–18:00 对应的绝对时段）。
- 暂停区间不消耗计时；恢复后从已消耗时长起继续在服务时段内累计，截止时间相应顺延。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

# 优先级 -> 响应目标时长
TARGETS: dict[str, timedelta] = {
    "P1": timedelta(minutes=15),
    "P2": timedelta(minutes=30),
    "P3": timedelta(hours=4),
    "P4": timedelta(hours=8),  # 1 个工作日 = 8 小时服务时段
}

SERVICE_START = time(9, 0)
SERVICE_END = time(18, 0)
OPEN_DELTA = timedelta(hours=18 - 9)  # 每个工作日 8 小时服务时段


@dataclass(frozen=True)
class ParsedTime:
    """解析后的时刻：``abs_dt`` 为 UTC 绝对时刻，``offset`` 为其自带偏移。"""

    abs_dt: datetime
    offset: timedelta

    @property
    def tz(self) -> timezone:
        return timezone(self.offset)


def parse_iso8601(text: str) -> ParsedTime:
    """解析 ISO 8601 文本。

    可带时区偏移（``+08:00``、``Z`` 等）；无偏移视为本地时间。返回 UTC 绝对时刻
    与原偏移（朴素时间取本地偏移）。无法解析时抛 ``ValueError``。
    """
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        local = dt.astimezone()  # 朴素时间按本地时区解释
        return ParsedTime(local.astimezone(timezone.utc), local.utcoffset() or timedelta())
    return ParsedTime(dt.astimezone(timezone.utc), dt.utcoffset() or timedelta())


def _iter_business_days(start_date, end_date):
    """枚举帧墙钟日期范围（含端点），调用方自行跳过周末。"""
    day = start_date
    while day <= end_date:
        if day.weekday() < 5:  # 周一=0 … 周五=4
            yield day
        day += timedelta(days=1)


def _window(day, tz: timezone) -> tuple[datetime, datetime]:
    """某帧墙钟日期的服务窗口 [09:00, 18:00) 对应的绝对时刻。"""
    start = datetime.combine(day, SERVICE_START, tzinfo=tz)
    return start, start + OPEN_DELTA


def service_seconds_between(start: datetime, end: datetime, offset: timedelta) -> float:
    """[start, end] 与提交偏移帧内服务窗口的重叠秒数。start/end 为 UTC 绝对时刻。"""
    if end <= start:
        return 0.0
    tz = timezone(offset)
    d0 = start.astimezone(tz).date()
    d1 = end.astimezone(tz).date()
    seconds = 0.0
    for day in _iter_business_days(d0, d1):
        win_start, win_end = _window(day, tz)
        lo = max(start, win_start)
        hi = min(end, win_end)
        if hi > lo:
            seconds += (hi - lo).total_seconds()
    return seconds


def advance_in_service(start: datetime, seconds: float, offset: timedelta) -> datetime:
    """从绝对时刻 ``start`` 起，在服务时段内累计 ``seconds`` 秒后到达的绝对时刻。

    ``start`` 本身可落在服务时段外（夜晚/周末），这种时长不计入。
    """
    if seconds <= 0:
        return start
    tz = timezone(offset)
    day = start.astimezone(tz).date()
    # 跨度上限保护：8 小时目标至多跨越若干个工作日，这里给到足够宽的天数。
    for _ in range(366 * 10):
        if day.weekday() < 5:
            win_start, win_end = _window(day, tz)
            seg_start = max(start, win_start)
            if seg_start < win_end:
                avail = (win_end - seg_start).total_seconds()
                if seconds <= avail:
                    return seg_start + timedelta(seconds=seconds)
                seconds -= avail
        day += timedelta(days=1)
    raise RuntimeError("advance_in_service 超出合理时间范围")  # pragma: no cover


@dataclass(frozen=True)
class SlaView:
    """某张工单在某观察时刻的 SLA 计时结果。"""

    deadline: datetime          # 响应截止（UTC 绝对时刻）
    offset: timedelta           # 提交时刻所在偏移帧
    elapsed_seconds: float      # 已消耗服务秒
    remaining_seconds: float    # 剩余服务秒（不小于 0）
    frozen: bool                # 暂停中：截止按“立即恢复”估算、剩余冻结

    @property
    def deadline_text(self) -> str:
        return self.deadline.astimezone(timezone(self.offset)).isoformat()

    @property
    def elapsed_minutes(self) -> int:
        return int(self.elapsed_seconds // 60)

    @property
    def remaining_minutes(self) -> int:
        return int(self.remaining_seconds // 60)


def compute_sla(
    priority: str,
    submitted_raw: str,
    events: list[tuple[str, str]],
    *,
    paused: bool,
    now: datetime,
) -> SlaView:
    """计算 SLA 视图。

    ``events`` 为按时间先后排列的 ``(kind, raw_text)``，kind 为 ``pause``/``resume``；
    ``paused`` 表示当前是否处于暂停（末个 pause 尚未配对 resume）；``now`` 为
    时区感知的观察时刻。
    """
    submitted = parse_iso8601(submitted_raw)
    offset = submitted.offset
    target_seconds = TARGETS[priority].total_seconds()

    # 已结束暂停段 [(pause_abs, resume_abs), ...]；若当前仍暂停，末尾再补一个开口段。
    closed_pauses: list[tuple[datetime, datetime]] = []
    open_pause: datetime | None = None
    pending_pause: datetime | None = None
    for kind, raw in events:
        at = parse_iso8601(raw).abs_dt
        if kind == "pause":
            pending_pause = at
        elif kind == "resume":
            if pending_pause is not None:
                closed_pauses.append((pending_pause, at))
                pending_pause = None
    if paused and pending_pause is not None:
        open_pause = pending_pause

    # ---- 截止时间：从提交起按服务时段累计，暂停区间整段跳过 ----
    cursor = submitted.abs_dt
    remaining = target_seconds
    deadline: datetime | None = None
    for pause_at, resume_at in closed_pauses:
        avail = service_seconds_between(cursor, pause_at, offset)
        if remaining <= avail:
            deadline = advance_in_service(cursor, remaining, offset)
            remaining = 0.0
            break
        remaining -= avail
        cursor = resume_at  # 暂停期间不消耗，直接跳到恢复时刻
    if deadline is None:
        if open_pause is not None:
            # 暂停中：截止时间挂起，按“此刻立即恢复”估算，剩余在暂停期间冻结。
            avail = service_seconds_between(cursor, open_pause, offset)
            if remaining <= avail:
                deadline = advance_in_service(cursor, remaining, offset)
            else:
                deadline = advance_in_service(
                    open_pause, remaining - avail, offset
                )
            frozen = True
        else:
            deadline = advance_in_service(cursor, remaining, offset)
            frozen = False
    else:
        frozen = open_pause is not None

    # ---- 已耗时：到观察时刻（暂停中冻结在暂停时刻）为止的服务秒，扣除暂停 ----
    end = open_pause if open_pause is not None else now.astimezone(timezone.utc)
    if end < submitted.abs_dt:
        end = submitted.abs_dt
    elapsed = service_seconds_between(submitted.abs_dt, end, offset)
    for pause_at, resume_at in closed_pauses:
        seg_lo = pause_at
        seg_hi = min(resume_at, end)
        if seg_hi > seg_lo:
            elapsed -= service_seconds_between(seg_lo, seg_hi, offset)
    elapsed = max(0.0, elapsed)
    remaining_display = max(0.0, target_seconds - elapsed)

    return SlaView(
        deadline=deadline,
        offset=offset,
        elapsed_seconds=elapsed,
        remaining_seconds=remaining_display,
        frozen=frozen,
    )
