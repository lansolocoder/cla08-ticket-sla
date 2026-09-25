"""SLA 服务时段计时的单元测试：截止累计、暂停扣除与顺延。"""

import unittest
from datetime import datetime, timedelta

from sla_desk import sla


def dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


class DeadlineTests(unittest.TestCase):
    def test_targets_inside_window(self) -> None:
        thu10 = dt("2026-09-24T10:00:00+08:00")
        self.assertEqual(
            sla.initial_deadline(thu10, "P1"),
            dt("2026-09-24T10:15:00+08:00"),
        )
        self.assertEqual(
            sla.initial_deadline(thu10, "P2"),
            dt("2026-09-24T10:30:00+08:00"),
        )
        self.assertEqual(
            sla.initial_deadline(thu10, "P3"),
            dt("2026-09-24T14:00:00+08:00"),
        )
        # P4 = 8 小时服务时段：10:00 起恰好在 18:00 结束。
        self.assertEqual(
            sla.initial_deadline(thu10, "P4"),
            dt("2026-09-24T18:00:00+08:00"),
        )

    def test_evening_overflows_to_next_morning(self) -> None:
        # 17:50 起 15 分钟：10 分钟到 18:00，余 5 分钟次日 09:05。
        self.assertEqual(
            sla.initial_deadline(dt("2026-09-24T17:50:00+08:00"), "P1"),
            dt("2026-09-25T09:05:00+08:00"),
        )
        # 周五傍晚溢出到周一。
        self.assertEqual(
            sla.initial_deadline(dt("2026-09-25T17:45:00+08:00"), "P2"),
            dt("2026-09-28T09:15:00+08:00"),
        )

    def test_weekend_and_before_window_do_not_count(self) -> None:
        # 周六中午提交：周一 09:00 才起算，P3 到 13:00。
        self.assertEqual(
            sla.initial_deadline(dt("2026-09-26T12:00:00+08:00"), "P3"),
            dt("2026-09-28T13:00:00+08:00"),
        )
        # 清晨 08:00 提交：09:00 起算。
        self.assertEqual(
            sla.initial_deadline(dt("2026-09-24T08:00:00+08:00"), "P1"),
            dt("2026-09-24T09:15:00+08:00"),
        )

    def test_p4_spans_multiple_days(self) -> None:
        # 周四 14:00：当天 4 小时到 18:00，次日 4 小时到 13:00。
        self.assertEqual(
            sla.initial_deadline(dt("2026-09-24T14:00:00+08:00"), "P4"),
            dt("2026-09-25T13:00:00+08:00"),
        )
        # 周五 18:00 整：下周一 17:00。
        self.assertEqual(
            sla.initial_deadline(dt("2026-09-25T18:00:00+08:00"), "P4"),
            dt("2026-09-28T17:00:00+08:00"),
        )


class BusinessTimeTests(unittest.TestCase):
    def test_excludes_nights_and_weekends(self) -> None:
        # 周四 17:00 到周五 10:00：1 + 1 = 2 小时。
        self.assertEqual(
            sla.business_time(
                dt("2026-09-24T17:00:00+08:00"),
                dt("2026-09-25T10:00:00+08:00"),
            ),
            timedelta(hours=2),
        )
        # 整个周末不计。
        self.assertEqual(
            sla.business_time(
                dt("2026-09-25T18:00:00+08:00"),
                dt("2026-09-28T09:00:00+08:00"),
            ),
            timedelta(0),
        )

    def test_excludes_paused_intervals(self) -> None:
        # 10:00–11:00 共 1 小时，中间暂停 10:20–10:50，实耗 30 分钟。
        self.assertEqual(
            sla.business_time(
                dt("2026-09-24T10:00:00+08:00"),
                dt("2026-09-24T11:00:00+08:00"),
                [(dt("2026-09-24T10:20:00+08:00"),
                  dt("2026-09-24T10:50:00+08:00"))],
            ),
            timedelta(minutes=30),
        )
        # 暂停区间落在窗口外时不产生重复扣除。
        self.assertEqual(
            sla.business_time(
                dt("2026-09-24T10:00:00+08:00"),
                dt("2026-09-24T11:00:00+08:00"),
                [(dt("2026-09-24T19:00:00+08:00"),
                  dt("2026-09-24T20:00:00+08:00"))],
            ),
            timedelta(hours=1),
        )


class PauseResumeTests(unittest.TestCase):
    SUB = dt("2026-09-24T10:00:00+08:00")

    def test_resume_extends_deadline(self) -> None:
        # 10:05 暂停、10:10 恢复：已耗 5 分钟，剩 10 分钟，截止顺延 10:20。
        pauses = [
            (dt("2026-09-24T10:05:00+08:00"),
             dt("2026-09-24T10:10:00+08:00")),
        ]
        due, elapsed, freeze = sla.sla_state(
            self.SUB, "P1", pauses, dt("2026-09-24T10:30:00+08:00")
        )
        self.assertEqual(due, dt("2026-09-24T10:20:00+08:00"))
        self.assertIsNone(freeze)
        # 10:00–10:30 扣除 5 分钟暂停 = 25 分钟。
        self.assertEqual(elapsed, timedelta(minutes=25))

    def test_paused_freezes_elapsed_and_remaining(self) -> None:
        pauses = [(dt("2026-09-24T10:05:00+08:00"), None)]
        due, elapsed, freeze = sla.sla_state(
            self.SUB, "P1", pauses, dt("2026-09-24T12:00:00+08:00")
        )
        self.assertEqual(freeze, dt("2026-09-24T10:05:00+08:00"))
        self.assertEqual(elapsed, timedelta(minutes=5))
        self.assertEqual(
            sla.RESPONSE_TARGETS["P1"] - elapsed, timedelta(minutes=10)
        )
        # 截止投影为暂停起点立即恢复：10:05 + 10 分钟 = 10:15。
        self.assertEqual(due, dt("2026-09-24T10:15:00+08:00"))

    def test_resume_across_window_boundary(self) -> None:
        # 17:55 提交（到 18:00 有 5 分钟），17:58 暂停、次日 09:30 恢复：
        # 暂停前耗 3 分钟，剩 12 分钟，09:30 起累计 -> 09:42。
        pauses = [
            (dt("2026-09-24T17:58:00+08:00"),
             dt("2026-09-25T09:30:00+08:00")),
        ]
        due, _elapsed, freeze = sla.sla_state(
            dt("2026-09-24T17:55:00+08:00"), "P1", pauses,
            dt("2026-09-25T10:00:00+08:00"),
        )
        self.assertEqual(due, dt("2026-09-25T09:42:00+08:00"))
        self.assertIsNone(freeze)

    def test_multiple_pause_cycles(self) -> None:
        # 运行段 5 + 2 + 10 = 17 分钟（10:30 查看），截止 = 10:28。
        pauses = [
            (dt("2026-09-24T10:05:00+08:00"),
             dt("2026-09-24T10:10:00+08:00")),
            (dt("2026-09-24T10:12:00+08:00"),
             dt("2026-09-24T10:20:00+08:00")),
        ]
        due, elapsed, _ = sla.sla_state(
            self.SUB, "P1", pauses, dt("2026-09-24T10:30:00+08:00")
        )
        self.assertEqual(due, dt("2026-09-24T10:28:00+08:00"))
        self.assertEqual(elapsed, timedelta(minutes=17))

    def test_deadline_reached_before_later_pause_stays_fixed(self) -> None:
        # 截止 10:15 已到；10:20 的暂停不再改变截止，已耗时照常累计。
        pauses = [
            (dt("2026-09-24T10:20:00+08:00"),
             dt("2026-09-24T10:40:00+08:00")),
        ]
        due, elapsed, _ = sla.sla_state(
            self.SUB, "P1", pauses, dt("2026-09-24T11:00:00+08:00")
        )
        self.assertEqual(due, dt("2026-09-24T10:15:00+08:00"))
        # 10:00–11:00 扣 20 分钟暂停 = 40 分钟。
        self.assertEqual(elapsed, timedelta(minutes=40))

    def test_offsets_normalized_to_submitted_offset(self) -> None:
        # 暂停时刻用 UTC 给出（02:05Z = 10:05+08），截止仍按 +08:00 表示。
        pauses = [
            (dt("2026-09-24T02:05:00+00:00"),
             dt("2026-09-24T02:10:00+00:00")),
        ]
        due, _elapsed, _ = sla.sla_state(
            self.SUB, "P1", pauses, dt("2026-09-24T10:30:00+08:00")
        )
        self.assertEqual(due.utcoffset(), timedelta(hours=8))
        self.assertEqual(due, dt("2026-09-24T10:20:00+08:00"))

    def test_floor_minutes_rounds_down(self) -> None:
        self.assertEqual(sla.floor_minutes(timedelta(seconds=119)), 1)
        self.assertEqual(sla.floor_minutes(timedelta(seconds=59)), 0)
        # 超期的负时长向负方向取整。
        self.assertEqual(sla.floor_minutes(timedelta(seconds=-61)), -2)


if __name__ == "__main__":
    unittest.main()
