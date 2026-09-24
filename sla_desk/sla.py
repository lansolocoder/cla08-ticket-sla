"""服务时段与 SLA 响应截止时间计算。

窗口定义为 *每天* 一个本地挂钟时间区间（``HH:MM``），因此在带时区的
时间线上，夏令时切换日区间的绝对长度可能不同。所有计算都先把时刻换到
窗口所在的本地时区再逐日累加。
"""

import datetime as _dt
import re

# 高 / 中 / 低优先级对应的响应额度（分钟）。
SLA_MINUTES = {"high": 60, "medium": 240, "low": 480}

_OFFSET_RE = re.compile(r"([+-])(\d{2}):?(\d{2})$")


class DateTimeError(ValueError):
    """时间参数无法解析。"""


class WindowError(ValueError):
    """窗口时间参数非法。"""


def parse_hhmm(value: str) -> _dt.time:
    """解析 ``HH:MM``；秒等多余精度一律拒绝。"""
    if not re.fullmatch(r"\d{2}:\d{2}", value or ""):
        raise WindowError(f"时间必须为 HH:MM 格式: {value!r}")
    hour, minute = (int(part) for part in value.split(":"))
    try:
        return _dt.time(hour, minute)
    except ValueError as exc:
        raise WindowError(f"时间不是合法的 HH:MM: {value!r}") from exc


def local_timezone() -> _dt.tzinfo:
    """返回机器本地时区（窗口按本地挂钟时间解释）。"""
    return _dt.datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def parse_created_at(value: str) -> _dt.datetime:
    """解析带偏移的 ISO8601 时刻，``Z`` 与裸时间均拒绝。"""
    text = value.strip()
    if not text or text.endswith(("Z", "z")):
        raise DateTimeError("时刻必须带显式时区偏移，如 2026-03-02T09:15:00+08:00")
    if not _OFFSET_RE.search(text) or "T" not in text:
        raise DateTimeError("时刻必须带显式时区偏移，如 2026-03-02T09:15:00+08:00")
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise DateTimeError(f"无法解析 ISO8601 时刻: {value!r}") from exc
    if parsed.tzinfo is None:
        raise DateTimeError("时刻必须带显式时区偏移，如 2026-03-02T09:15:00+08:00")
    return parsed


def validate_window(start: _dt.time, end: _dt.time) -> None:
    """end 必须严格晚于 start；跨零点区间按 end <= start 拒绝。"""
    if end <= start:
        raise WindowError("窗口结束时间必须晚于开始时间，且不支持跨零点区间")


def calculate_deadline(
    created_at: _dt.datetime,
    start: _dt.time,
    end: _dt.time,
    budget_minutes: int,
    tz: _dt.tzinfo,
) -> _dt.datetime:
    """从受理时刻起，只累计每日服务时段内的时间，满 ``budget_minutes`` 即止。

    - ``created_at`` 落在时段之外（含早于起点）时，从下一个时段起点开始；
    - 逐日累加，当天额度不够则顺延至次日窗口起点；
    - 截止点可能恰好落在窗口终点（边界闭合）。
    """
    if end <= start:
        raise WindowError("窗口结束时间必须晚于开始时间，且不支持跨零点区间")

    local = created_at.astimezone(tz)
    day = local.date()
    cursor = local
    remaining = float(budget_minutes)

    # 最多向后扫描一年，给异常输入（如长度为 0 的窗口）兜底。
    for _ in range(366 * 2):
        day_start = _dt.datetime.combine(day, start, tzinfo=tz)
        day_end = _dt.datetime.combine(day, end, tzinfo=tz)
        if cursor < day_start:
            # 当天窗口尚未开始（含 created_at 不在任何窗口内的情形）。
            cursor = day_start
        if day_start <= cursor < day_end:
            available = (day_end - cursor).total_seconds() / 60.0
            if remaining <= available:
                return cursor + _dt.timedelta(minutes=remaining)
            remaining -= available
        # 当天窗口已过，或额度用穿当天窗口 → 进入下一天。
        day += _dt.timedelta(days=1)
        cursor = _dt.datetime.combine(day, start, tzinfo=tz)

    raise WindowError("服务时段计算超出可处理的日期范围")  # pragma: no cover


def format_utc(value: _dt.datetime) -> str:
    """格式化为 UTC、秒级精度、``Z`` 结尾、无小数秒。"""
    utc = value.astimezone(_dt.timezone.utc).replace(microsecond=0)
    return utc.isoformat().replace("+00:00", "Z")
