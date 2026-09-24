"""Checks for the documented command-line entry point."""

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

from sla_desk import sla


ROOT = Path(__file__).resolve().parents[1]

# 窗口按机器本地挂钟时间解释；固定时区保证用例可复现（上海无夏令时）。
FIXED_ENV = {**os.environ, "TZ": "Asia/Shanghai"}

TICKET_FIELDS = ["id", "priority", "window_id", "created_at", "deadline"]


class CommandLineTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "sla_desk", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=FIXED_ENV,
        )

    def test_help_and_no_arguments(self) -> None:
        for arguments in [(), ("--help",)]:
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--help", result.stdout)
                self.assertIn("--version", result.stdout)
                self.assertEqual(result.stderr, "")

    def test_version(self) -> None:
        result = self.invoke("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "sla-desk 0.1.0")
        self.assertEqual(result.stderr, "")

    def test_unknown_argument_is_an_error(self) -> None:
        result = self.invoke("--unknown-option")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--unknown-option", result.stderr)
        self.assertEqual(result.stdout, "")


class LedgerCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "ledger.sqlite")

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "sla_desk", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=FIXED_ENV,
        )

    def create_window(
        self, window_id: str = "w", start: str = "09:00", end: str = "18:00"
    ) -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "window", "create", "--db", self.db,
            "--id", window_id, "--start", start, "--end", end,
        )

    def create_ticket(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.invoke("ticket", "create", "--db", self.db, *extra)

    def assertTicketJson(self, payload: dict[str, object], ticket_id: str) -> None:
        self.assertEqual(list(payload), TICKET_FIELDS)
        self.assertEqual(payload["id"], ticket_id)
        for key in ("created_at", "deadline"):
            text = str(payload[key])
            self.assertTrue(text.endswith("Z"), text)
            self.assertNotIn(".", text, text)
            # 必须能以 UTC 解析回秒级时刻。
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            self.assertEqual(parsed.microsecond, 0)

    # ---- 窗口 -----------------------------------------------------------

    def test_window_create_success_and_duplicate(self) -> None:
        result = self.create_window()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

        dup = self.create_window()
        self.assertEqual(dup.returncode, 2, dup.stderr)
        self.assertEqual(dup.stdout, "")
        self.assertIn("w", dup.stderr)

    def test_window_rejects_crossing_midnight_and_equal_bounds(self) -> None:
        for start, end in [("22:00", "02:00"), ("09:00", "09:00")]:
            with self.subTest(start=start, end=end):
                result = self.create_window("bad", start, end)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_window_rejects_bad_time_format(self) -> None:
        for start, end in [("9:00", "18:00"), ("09:00:00", "18:00"),
                           ("ab:cd", "18:00"), ("24:00", "18:00")]:
            with self.subTest(start=start, end=end):
                result = self.create_window("bad", start, end)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_window_requires_db(self) -> None:
        result = self.invoke(
            "window", "create", "--id", "w", "--start", "09:00", "--end", "18:00"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--db", result.stderr)
        self.assertEqual(result.stdout, "")

    # ---- 工单创建与 SLA  deadline ---------------------------------------

    def test_ticket_create_high_within_window(self) -> None:
        self.create_window()
        result = self.create_ticket(
            "--id", "t1", "--priority", "high", "--window-id", "w",
            "--created-at", "2026-03-02T09:15:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTicketJson(payload, "t1")
        self.assertEqual(payload["priority"], "high")
        self.assertEqual(payload["window_id"], "w")
        self.assertEqual(payload["created_at"], "2026-03-02T01:15:00Z")
        self.assertEqual(payload["deadline"], "2026-03-02T02:15:00Z")

    def test_ticket_offset_is_normalized_to_utc(self) -> None:
        self.create_window()
        # +09:00 的 10:15 等于上海 09:15，deadline 应与 +08:00 用例一致。
        result = self.create_ticket(
            "--id", "t1", "--priority", "high", "--window-id", "w",
            "--created-at", "2026-03-02T10:15:00+09:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["created_at"], "2026-03-02T01:15:00Z")
        self.assertEqual(payload["deadline"], "2026-03-02T02:15:00Z")

    def test_ticket_before_window_starts_at_next_window(self) -> None:
        self.create_window()
        result = self.create_ticket(
            "--id", "t2", "--priority", "high", "--window-id", "w",
            "--created-at", "2026-03-02T07:00:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["created_at"], "2026-03-01T23:00:00Z")
        self.assertEqual(payload["deadline"], "2026-03-02T02:00:00Z")

    def test_ticket_after_window_end_spans_multiple_windows(self) -> None:
        self.create_window()
        # medium = 240 分钟；17:00 受理，当天剩 60 分钟，次日再计 180 分钟。
        result = self.create_ticket(
            "--id", "t3", "--priority", "medium", "--window-id", "w",
            "--created-at", "2026-03-02T17:00:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["deadline"], "2026-03-03T04:00:00Z"
        )

    def test_ticket_exactly_at_window_end_starts_next_day(self) -> None:
        self.create_window()
        # low = 480 分钟；18:00 受理视为窗口外，次日 09:00 起计 8 小时→17:00。
        result = self.create_ticket(
            "--id", "t4", "--priority", "low", "--window-id", "w",
            "--created-at", "2026-03-02T18:00:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["deadline"], "2026-03-03T09:00:00Z"
        )

    def test_ticket_low_accumulates_across_more_than_two_windows(self) -> None:
        # 窗口仅 5 小时（300 分钟），low=480 需要两天多：
        # 第 1 天 17:00 后 0 分钟 → 第 2 天 300 分钟 → 第 3 天再 180 分钟 = 12:00。
        self.create_window("short", "09:00", "14:00")
        result = self.create_ticket(
            "--id", "t5", "--priority", "low", "--window-id", "short",
            "--created-at", "2026-03-02T17:00:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["deadline"], "2026-03-04T04:00:00Z"
        )

    def test_deadline_may_land_exactly_on_window_end(self) -> None:
        # 窗口 09:00-10:00 恰好 60 分钟，high 从起点受理，deadline 落在终点。
        self.create_window("hour", "09:00", "10:00")
        result = self.create_ticket(
            "--id", "t6", "--priority", "high", "--window-id", "hour",
            "--created-at", "2026-03-02T09:00:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["deadline"], "2026-03-02T02:00:00Z"
        )

    def test_create_output_is_single_json_line(self) -> None:
        self.create_window()
        result = self.create_ticket(
            "--id", "t1", "--priority", "high", "--window-id", "w",
            "--created-at", "2026-03-02T09:15:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)

    # ---- 工单失败路径 ----------------------------------------------------

    def test_bad_priority_is_rejected_and_not_stored(self) -> None:
        self.create_window()
        for priority in ["HIGH", "High", "urgent", ""]:
            with self.subTest(priority=priority):
                result = self.create_ticket(
                    "--id", "bad", "--priority", priority, "--window-id", "w",
                    "--created-at", "2026-03-02T09:15:00+08:00",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
        listing = self.invoke("ticket", "list", "--db", self.db)
        self.assertEqual(json.loads(listing.stdout), [])

    def test_naive_and_zulu_timestamps_are_rejected(self) -> None:
        self.create_window()
        for value in ["2026-03-02T09:15:00", "2026-03-02T01:15:00Z",
                      "not-a-date", "2026-03-02 09:15:00+08:00"]:
            with self.subTest(value=value):
                result = self.create_ticket(
                    "--id", "bad", "--priority", "high", "--window-id", "w",
                    "--created-at", value,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_missing_window_fails_without_touching_ledger(self) -> None:
        self.create_window()
        result = self.create_ticket(
            "--id", "orphan", "--priority", "high", "--window-id", "ghost",
            "--created-at", "2026-03-02T09:15:00+08:00",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ghost", result.stderr)
        self.assertEqual(result.stdout, "")
        # 台账里不得出现半条记录。
        self.assertEqual(
            json.loads(self.invoke("ticket", "list", "--db", self.db).stdout), []
        )

    def test_missing_db_file_is_not_created_on_failure(self) -> None:
        missing = str(Path(self._tmp.name) / "nope.sqlite")
        result = self.invoke(
            "ticket", "create", "--db", missing,
            "--id", "x", "--priority", "high", "--window-id", "w",
            "--created-at", "2026-03-02T09:15:00+08:00",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(Path(missing).exists())

    def test_duplicate_ticket_exits_2_and_keeps_old_record(self) -> None:
        self.create_window()
        args = ("--priority", "high", "--window-id", "w",
                "--created-at", "2026-03-02T09:15:00+08:00")
        first = self.create_ticket("--id", "dup", *args)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.create_ticket(
            "--id", "dup", "--priority", "low", "--window-id", "w",
            "--created-at", "2026-03-03T09:15:00+08:00",
        )
        self.assertEqual(second.returncode, 2, second.stderr)
        self.assertEqual(second.stdout, "")
        shown = self.invoke("ticket", "show", "--db", self.db, "--id", "dup")
        payload = json.loads(shown.stdout)
        self.assertEqual(payload["priority"], "high")
        self.assertEqual(payload["created_at"], "2026-03-02T01:15:00Z")

    # ---- show / list ----------------------------------------------------

    def test_show_existing_ticket(self) -> None:
        self.create_window()
        self.create_ticket(
            "--id", "t1", "--priority", "high", "--window-id", "w",
            "--created-at", "2026-03-02T09:15:00+08:00",
        )
        result = self.invoke("ticket", "show", "--db", self.db, "--id", "t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTicketJson(payload, "t1")

    def test_show_missing_ticket_exits_nonzero(self) -> None:
        self.create_window()
        result = self.invoke("ticket", "show", "--db", self.db, "--id", "nope")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("nope", result.stderr)

    def test_list_empty_ledger_outputs_empty_array(self) -> None:
        self.create_window()
        result = self.invoke("ticket", "list", "--db", self.db)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")

    def test_list_orders_by_created_at_then_id(self) -> None:
        self.create_window()
        def make(ticket_id: str, created_at: str, priority: str = "high") -> None:
            result = self.create_ticket(
                "--id", ticket_id, "--priority", priority,
                "--window-id", "w", "--created-at", created_at,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        # 故意乱序插入；b 与 c 同一时刻，按 id 字典序 b 在 c 前。
        make("zeta", "2026-03-04T09:00:00+08:00")
        make("c", "2026-03-02T09:30:00+08:00")
        make("b", "2026-03-02T09:30:00+08:00", priority="low")
        make("alpha", "2026-03-01T09:00:00+08:00")

        result = self.invoke("ticket", "list", "--db", self.db)
        ids = [row["id"] for row in json.loads(result.stdout)]
        self.assertEqual(ids, ["alpha", "b", "c", "zeta"])
        self.assertEqual(len(result.stdout.splitlines()), 1)

    def test_list_without_db_file_fails_and_creates_nothing(self) -> None:
        missing = str(Path(self._tmp.name) / "ghost.sqlite")
        result = self.invoke("ticket", "list", "--db", missing)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertFalse(Path(missing).exists())

    # ---- 通用 CLI 约定 ---------------------------------------------------

    def test_db_option_works_before_subcommand(self) -> None:
        self.create_window()
        self.create_ticket(
            "--id", "t1", "--priority", "high", "--window-id", "w",
            "--created-at", "2026-03-02T09:15:00+08:00",
        )
        result = self.invoke("--db", self.db, "ticket", "show", "--id", "t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["id"], "t1")

    def test_subcommand_help_does_not_require_db(self) -> None:
        for arguments in [("ticket", "--help"), ("ticket", "create", "--help"),
                          ("window", "--help"), ("window", "create", "--help")]:
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")

    def test_unknown_and_missing_subcommands_fail(self) -> None:
        for arguments in [("ticket",), ("window",), ("nope",),
                          ("ticket", "frobnicate")]:
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")


class DeadlineCalculationTests(unittest.TestCase):
    """直接覆盖纯计算逻辑，包括夏令时切换日。"""

    SHANGHAI = ZoneInfo("Asia/Shanghai")
    NEW_YORK = ZoneInfo("America/New_York")

    def deadline(
        self, created_at: str, start: str, end: str, budget: int,
        tz: ZoneInfo = SHANGHAI,
    ) -> dt.datetime:
        return sla.calculate_deadline(
            dt.datetime.fromisoformat(created_at),
            dt.time.fromisoformat(start),
            dt.time.fromisoformat(end),
            budget,
            tz,
        )

    def test_within_window(self) -> None:
        d = self.deadline("2026-03-02T09:15:00+08:00", "09:00", "18:00", 60)
        self.assertEqual(d, dt.datetime(2026, 3, 2, 10, 15, tzinfo=self.SHANGHAI))

    def test_outside_window_starts_at_next_window(self) -> None:
        d = self.deadline("2026-03-02T20:00:00+08:00", "09:00", "18:00", 60)
        self.assertEqual(d, dt.datetime(2026, 3, 3, 10, 0, tzinfo=self.SHANGHAI))

    def test_exactly_window_end_is_treated_as_outside(self) -> None:
        d = self.deadline("2026-03-02T18:00:00+08:00", "09:00", "18:00", 540)
        self.assertEqual(d, dt.datetime(2026, 3, 3, 18, 0, tzinfo=self.SHANGHAI))

    def test_spring_forward_day_uses_elapsed_time(self) -> None:
        # 2026-03-08 美东 02:00 跳到 03:00；01:30 起在窗口内累计 60 分钟，
        # 挂钟应走到 03:30 EDT（即 07:30Z）。
        d = self.deadline(
            "2026-03-08T01:30:00-05:00", "00:00", "23:59", 60, self.NEW_YORK
        )
        self.assertEqual(d.astimezone(dt.timezone.utc),
                         dt.datetime(2026, 3, 8, 7, 30, tzinfo=dt.timezone.utc))

    def test_format_utc_strips_fractional_seconds(self) -> None:
        value = dt.datetime(2026, 3, 2, 9, 15, 30, 500000,
                            tzinfo=self.SHANGHAI)
        self.assertEqual(sla.format_utc(value), "2026-03-02T01:15:30Z")

    def test_parse_created_at_requires_offset(self) -> None:
        with self.assertRaises(sla.DateTimeError):
            sla.parse_created_at("2026-03-02T09:15:00")
        with self.assertRaises(sla.DateTimeError):
            sla.parse_created_at("2026-03-02T01:15:00Z")
        parsed = sla.parse_created_at("2026-03-02T09:15:00+08:00")
        self.assertEqual(parsed.utcoffset(), dt.timedelta(hours=8))


if __name__ == "__main__":
    unittest.main()
