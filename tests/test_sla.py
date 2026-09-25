"""SLA 服务时段计时单元测试：截止时间、已耗时、剩余与暂停顺延。"""

import unittest
from datetime import datetime

from sla_desk import sla


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


class DeadlineTests(unittest.TestCase):
    def test_p1_within_service_window(self) -> None:
        v = sla.compute_sla(
            "P1", "2026-09-24T10:00:00+08:00", [],
            paused=False, now=at("2026-09-24T10:05:00+08:00"),
        )
        self.assertEqual(v.deadline_text, "2026-09-24T10:15:00+08:00")
        self.assertEqual(v.elapsed_minutes, 5)
        self.assertEqual(v.remaining_minutes, 10)

    def test_p2_thirty_minutes(self) -> None:
        v = sla.compute_sla(
            "P2", "2026-09-24T09:30:00+00:00", [],
            paused=False, now=at("2026-09-24T09:45:00+00:00"),
        )
        self.assertEqual(v.deadline_text, "2026-09-24T10:00:00+00:00")
        self.assertEqual(v.elapsed_minutes, 15)
        self.assertEqual(v.remaining_minutes, 15)

    def test_p3_carries_overnight_to_next_business_day(self) -> None:
        # 周四 16:00：当日剩 2 服务小时，余下 2 小时计入周五 09:00–11:00
        v = sla.compute_sla(
            "P3", "2026-09-24T16:00:00+08:00", [],
            paused=False, now=at("2026-09-24T16:00:00+08:00"),
        )
        self.assertEqual(v.deadline_text, "2026-09-25T11:00:00+08:00")

    def test_p1_spans_weekend(self) -> None:
        # 周五 17:50：10 分钟到 18:00，余 5 分钟顺延到周一 09:05
        v = sla.compute_sla(
            "P1", "2026-09-25T17:50:00+08:00", [],
            paused=False, now=at("2026-09-25T17:50:00+08:00"),
        )
        self.assertEqual(v.deadline_text, "2026-09-28T09:05:00+08:00")

    def test_p4_one_business_day_is_eight_service_hours(self) -> None:
        v = sla.compute_sla(
            "P4", "2026-09-25T10:00:00+08:00", [],
            paused=False, now=at("2026-09-25T10:00:00+08:00"),
        )
        self.assertEqual(v.deadline_text, "2026-09-25T18:00:00+08:00")

    def test_p4_spans_multiple_days(self) -> None:
        # 周四 16:00 起 8 服务小时：周四 2h + 周五 8h 中的 6h -> 周五 15:00
        v = sla.compute_sla(
            "P4", "2026-09-24T16:00:00+08:00", [],
            paused=False, now=at("2026-09-24T16:00:00+08:00"),
        )
        self.assertEqual(v.deadline_text, "2026-09-25T15:00:00+08:00")

    def test_submission_before_service_hours_starts_at_open(self) -> None:
        # 周四 08:00 提交：08:00–09:00 不消耗，P1 截止 09:15
        v = sla.compute_sla(
            "P1", "2026-09-24T08:00:00+08:00", [],
            paused=False, now=at("2026-09-24T08:30:00+08:00"),
        )
        self.assertEqual(v.deadline_text, "2026-09-24T09:15:00+08:00")
        self.assertEqual(v.elapsed_minutes, 0)

    def test_submission_after_close_waits_until_next_business_day(self) -> None:
        # 周四 20:00 提交：P1 截止周五 09:15
        v = sla.compute_sla(
            "P1", "2026-09-24T20:00:00+08:00", [],
            paused=False, now=at("2026-09-24T20:30:00+08:00"),
        )
        self.assertEqual(v.deadline_text, "2026-09-25T09:15:00+08:00")

    def test_weekend_submission_waits_until_monday(self) -> None:
        # 周六 10:00 提交：整个周末不消耗，P1 截止周一 09:15
        v = sla.compute_sla(
            "P1", "2026-09-26T10:00:00+08:00", [],
            paused=False, now=at("2026-09-27T12:00:00+08:00"),
        )
        self.assertEqual(v.deadline_text, "2026-09-28T09:15:00+08:00")
        self.assertEqual(v.elapsed_minutes, 0)

    def test_output_uses_submission_offset_frame(self) -> None:
        # 提交带 Z，截止也以 +00:00 偏移表示
        v = sla.compute_sla(
            "P1", "2026-09-24T10:00:00Z", [],
            paused=False, now=at("2026-09-24T10:05:00+00:00"),
        )
        self.assertEqual(v.deadline_text, "2026-09-24T10:15:00+00:00")


class ElapsedTests(unittest.TestCase):
    def test_minutes_floor(self) -> None:
        # 已耗 5 分 59 秒 -> 向下取整 5 分钟
        v = sla.compute_sla(
            "P1", "2026-09-24T10:00:00+08:00", [],
            paused=False, now=at("2026-09-24T10:05:59+08:00"),
        )
        self.assertEqual(v.elapsed_minutes, 5)
        self.assertEqual(v.remaining_minutes, 9)  # 15:00 - 5:59 = 9:01 -> 9

    def test_night_time_does_not_accrue(self) -> None:
        # 周四 17:55 到周五 09:05：服务秒只有 5（到 18:00）+ 5（从 09:00）= 10
        v = sla.compute_sla(
            "P3", "2026-09-24T17:55:00+08:00", [],
            paused=False, now=at("2026-09-25T09:05:00+08:00"),
        )
        self.assertEqual(v.elapsed_minutes, 10)

    def test_remaining_clamps_at_zero(self) -> None:
        v = sla.compute_sla(
            "P1", "2026-09-24T10:00:00+08:00", [],
            paused=False, now=at("2026-09-25T10:00:00+08:00"),
        )
        self.assertEqual(v.remaining_minutes, 0)


class PauseTests(unittest.TestCase):
    def test_paused_view_freezes_elapsed_and_remaining(self) -> None:
        events = [("pause", "2026-09-24T10:05:00+08:00")]
        # 即便观察时刻在两小时后（甚至更久），剩余仍冻结为 10 分钟
        v = sla.compute_sla(
            "P1", "2026-09-24T10:00:00+08:00", events,
            paused=True, now=at("2026-09-24T12:00:00+08:00"),
        )
        self.assertTrue(v.frozen)
        self.assertEqual(v.elapsed_minutes, 5)
        self.assertEqual(v.remaining_minutes, 10)

    def test_paused_before_deadline_estimate_assumes_immediate_resume(self) -> None:
        events = [("pause", "2026-09-24T10:05:00+08:00")]
        v = sla.compute_sla(
            "P1", "2026-09-24T10:00:00+08:00", events,
            paused=True, now=at("2026-09-24T10:05:00+08:00"),
        )
        self.assertEqual(v.deadline_text, "2026-09-24T10:15:00+08:00")

    def test_resume_extends_deadline(self) -> None:
        events = [
            ("pause", "2026-09-24T10:05:00+08:00"),
            ("resume", "2026-09-24T11:30:00+08:00"),
        ]
        v = sla.compute_sla(
            "P1", "2026-09-24T10:00:00+08:00", events,
            paused=False, now=at("2026-09-24T11:35:00+08:00"),
        )
        # 截止从恢复时刻起重新累计剩余 10 分钟 -> 11:40
        self.assertEqual(v.deadline_text, "2026-09-24T11:40:00+08:00")
        self.assertEqual(v.elapsed_minutes, 10)  # 暂停前 5 + 恢复后 5
        self.assertEqual(v.remaining_minutes, 5)

    def test_pause_over_weekend_extends_deadline_to_monday(self) -> None:
        events = [
            ("pause", "2026-09-25T17:55:00+08:00"),
            ("resume", "2026-09-28T10:00:00+08:00"),
        ]
        v = sla.compute_sla(
            "P1", "2026-09-25T17:50:00+08:00", events,
            paused=False, now=at("2026-09-28T10:00:00+08:00"),
        )
        # 暂停前消耗 5 分钟；周末整段跳过；周一 10:00 恢复后再计 10 分钟
        self.assertEqual(v.deadline_text, "2026-09-28T10:10:00+08:00")
        self.assertEqual(v.elapsed_minutes, 5)

    def test_pause_during_closed_time_does_not_change_deadline(self) -> None:
        # 周五 18:00 后本就不计时；19:00 暂停、周一 09:10 恢复，P1 截止仍是周一 09:15
        events = [
            ("pause", "2026-09-25T19:00:00+08:00"),
            ("resume", "2026-09-28T09:10:00+08:00"),
        ]
        v = sla.compute_sla(
            "P1", "2026-09-25T17:50:00+08:00", events,
            paused=False, now=at("2026-09-28T09:10:00+08:00"),
        )
        self.assertEqual(v.deadline_text, "2026-09-28T09:15:00+08:00")

    def test_multiple_pause_resume_cycles(self) -> None:
        events = [
            ("pause", "2026-09-24T10:05:00+08:00"),
            ("resume", "2026-09-24T10:10:00+08:00"),
            ("pause", "2026-09-24T10:12:00+08:00"),
            ("resume", "2026-09-24T10:20:00+08:00"),
        ]
        v = sla.compute_sla(
            "P1", "2026-09-24T10:00:00+08:00", events,
            paused=False, now=at("2026-09-24T10:22:00+08:00"),
        )
        # 实际计时：00–05（5）+ 10–12（2）+ 20–22（2）= 9 分钟
        self.assertEqual(v.elapsed_minutes, 9)
        self.assertEqual(v.remaining_minutes, 6)
        # 截止：恢复于 10:20 时已消耗 7 分钟，余 8 分钟 -> 10:28
        self.assertEqual(v.deadline_text, "2026-09-24T10:28:00+08:00")

    def test_events_with_different_offsets_compare_as_absolute(self) -> None:
        # +00:00 的 02:05/03:30 等同 +08:00 的 10:05/11:30
        events = [
            ("pause", "2026-09-24T02:05:00+00:00"),
            ("resume", "2026-09-24T03:30:00Z"),
        ]
        v = sla.compute_sla(
            "P1", "2026-09-24T10:00:00+08:00", events,
            paused=False, now=at("2026-09-24T03:35:00+00:00"),
        )
        self.assertEqual(v.deadline_text, "2026-09-24T11:40:00+08:00")


if __name__ == "__main__":
    unittest.main()
