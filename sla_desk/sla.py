"""SLA 响应计时：服务时段累计、暂停扣除、截止顺延。

服务时段为周一至周五 09:00–18:00（按工单提交时间文本自带的时区偏移
解释；无偏移按本地时间）。响应目标：

- P1：15 分钟
- P2：30 分钟
- P3：4 小时
- P4：1 个工作日（等同 8 小时服务时段）

P1–P3 也只在服务时段内消耗计时；落在服务时段外（夜晚、周末）的时间
不计入，也不会被暂停区间重复扣除。暂停期间不消耗计时，恢复后从已消耗
的服务时长继续累计，截止时间相应顺延。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

# 各优先级的响应目标（服务时段内的累计时长）。
RESPONSE_TARGETS: dict[str, timedelta] = {
    "P1": timedelta(minutes=15),
    "P2": timedelta(minutes=30),
    "P3": timedelta(hours=4),
    "P4": timedelta(hours=8),  # 1 个工作日 = 8 小时服务时段
}

SERVICE_START = time(9, 0)
SERVICE_END = time(18, 0)

# 累计推进时的保护性上限：8 小时目标最多跨越约 3 个自然日。
_MAX_DAYS = 10000


def parse_instant(text: str) -> datetime:
    """解析 ISO 8601 时刻文本；无偏移按本地时间解释。"""
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def _overlap(a_start: datetime, a_end: datetime,
             b_start: datetime, b_end: datetime) -> timedelta:
    lo = max(a_start, b_start)
    hi = min(a_end, b_end)
    return hi - lo if hi > lo else timedelta(0)


def _window_bounds(day: date, tzinfo) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, SERVICE_START, tzinfo=tzinfo),
        datetime.combine(day, SERVICE_END, tzinfo=tzinfo),
    )


def business_time(
    start: datetime,
    end: datetime,
    paused: list[tuple[datetime, datetime | None]] | None = None,
) -> timedelta:
    """[start, end] 内落在服务时段、且未被暂停覆盖的累计时长。

    暂停区间止点为 None 表示暂停至今（与 end 取较早者）。按天逐段
    处理，时间复杂度与跨越天数成正比。
    """
    if end <= start:
        return timedelta(0)

    total = timedelta(0)
    day = start.date()
    while day <= end.date():
        if day.weekday() < 5:  # 周一=0 … 周五=4
            w_start, w_end = _window_bounds(day, start.tzinfo)
            seg_start = max(start, w_start)
            seg_end = min(end, w_end)
            if seg_end > seg_start:
                total += seg_end - seg_start
                for p_start, p_end in paused or []:
                    stop = end if p_end is None else p_end
                    stop = min(stop, end)
                    if stop > start:
                        total -= _overlap(
                            seg_start, seg_end, max(p_start, start), stop
                        )
        day += timedelta(days=1)
    return total


def _next_window_start(t: datetime) -> datetime:
    """t 之后下一个服务窗口的起点（t 本身恰为窗口起点时返回其本身）。"""
    if t.weekday() < 5 and t.time() < SERVICE_START:
        return datetime.combine(t.date(), SERVICE_START, tzinfo=t.tzinfo)
    day = t.date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, SERVICE_START, tzinfo=t.tzinfo)


def advance(t: datetime, budget: timedelta) -> datetime:
    """从 t 起在服务时段内累计 budget 服务时长后到达的时刻。

    服务时段外的等待不计入：遇窗口结束或周末即跳到下一个服务窗口。
    时间运算沿用 t 自身的时区偏移。
    """
    if budget <= timedelta(0):
        return t
    remaining = budget
    cur = t
    for _ in range(_MAX_DAYS):
        if cur.weekday() >= 5 or cur.time() >= SERVICE_END:
            cur = _next_window_start(cur)
            continue
        if cur.time() < SERVICE_START:
            cur = datetime.combine(cur.date(), SERVICE_START,
                                   tzinfo=cur.tzinfo)
        w_end = datetime.combine(cur.date(), SERVICE_END,
                                 tzinfo=cur.tzinfo)
        room = w_end - cur
        if remaining <= room:
            return cur + remaining
        remaining -= room
        cur = _next_window_start(cur)
    raise RuntimeError("累计服务时长超过可接受的时间范围")  # pragma: no cover


def initial_deadline(submitted: datetime, priority: str) -> datetime:
    """登记后的初始响应截止：提交时刻起累计目标服务时长。"""
    return advance(submitted, RESPONSE_TARGETS[priority])


def sla_state(
    submitted: datetime,
    priority: str,
    pauses: list[tuple[datetime, datetime | None]],
    now: datetime,
) -> tuple[datetime, timedelta, datetime | None]:
    """计算查看工单时的 ``(截止时刻, 已耗服务时长, 冻结时刻)``。

    ``pauses`` 按时间顺序排列，止点为 None 表示暂停中。运行段消耗剩余
    目标，暂停段不计时；恢复后截止按“从恢复时刻继续累计剩余目标服务
    时长”顺延。暂停中截止与已耗时按暂停起点冻结，``freeze`` 返回该
    起点（非暂停时为 None）。所有暂停时刻先换算到提交时刻的时区偏移，
    使截止时间始终按提交时间所在偏移表示。
    """
    target = RESPONSE_TARGETS[priority]
    tz = submitted.tzinfo
    now = now.astimezone(tz)
    normalized: list[tuple[datetime, datetime | None]] = [
        (p_start.astimezone(tz),
         None if p_end is None else p_end.astimezone(tz))
        for p_start, p_end in pauses
    ]

    open_start: datetime | None = None
    closed: list[tuple[datetime, datetime]] = []
    for p_start, p_end in normalized:
        if p_end is None:
            open_start = p_start
        else:
            closed.append((p_start, p_end))

    # 截止时刻：逐段模拟。运行段消耗剩余目标，暂停段不计时；截止一旦
    # 在某个运行段内到达便固定下来，之后的暂停不再影响它。
    remaining = target
    cursor = submitted
    due: datetime | None = None
    for p_start, p_end in closed:
        consumed = business_time(cursor, p_start)
        if consumed >= remaining:
            due = advance(cursor, remaining)
            break
        remaining -= consumed
        cursor = p_end
    if due is None:
        if open_start is not None:
            consumed = business_time(cursor, open_start)
            if consumed >= remaining:
                due = advance(cursor, remaining)
            else:
                # 投影为“暂停起点立即恢复”后的截止。
                due = advance(open_start, remaining - consumed)
        else:
            due = advance(cursor, remaining)

    # 已耗时单独按查看时刻（暂停中即暂停起点）扣除暂停区间计算，
    # 因此截止已到后的继续运行/暂停也能得到正确的已耗时与负值剩余。
    view_time = open_start if open_start is not None else now
    elapsed = business_time(submitted, view_time, normalized)
    return due, elapsed, open_start


def floor_minutes(delta: timedelta) -> int:
    """时长向下取整为整分钟。"""
    return int(delta.total_seconds() // 60)
