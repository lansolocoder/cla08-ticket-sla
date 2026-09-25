"""Checks for the documented command-line entry point."""

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CommandLineTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "sla_desk", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
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


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmp.name) / "ledger.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "sla_desk", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def create_window(self, wid: str = "w1", start: str = "09:00", end: str = "18:00"):
        return self.invoke(
            "window", "create", "--db", self.db,
            "--id", wid, "--start", start, "--end", end,
        )

    def ticket_count(self) -> int:
        with sqlite3.connect(self.db) as conn:
            return conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]

    def window_count(self) -> int:
        with sqlite3.connect(self.db) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'windows'"
            ).fetchone()
            if exists is None:
                return 0
            return conn.execute("SELECT COUNT(*) FROM windows").fetchone()[0]

    # --- windows -----------------------------------------------------------

    def test_window_create_persists(self) -> None:
        result = self.create_window()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT start_minute, end_minute FROM windows WHERE id = 'w1'"
            ).fetchone()
        self.assertEqual(row, (9 * 60, 18 * 60))

    def test_duplicate_window_is_rc2_and_not_overwritten(self) -> None:
        self.assertEqual(self.create_window().returncode, 0)
        result = self.invoke(
            "window", "create", "--db", self.db,
            "--id", "w1", "--start", "10:00", "--end", "11:00",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("w1", result.stderr)
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT start_minute, end_minute FROM windows WHERE id = 'w1'"
            ).fetchone()
        self.assertEqual(row, (9 * 60, 18 * 60))

    def test_cross_midnight_window_rejected(self) -> None:
        result = self.create_window("w2", "22:00", "02:00")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.window_count(), 0)

    def test_equal_start_end_rejected(self) -> None:
        result = self.create_window("w2", "09:00", "09:00")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_bad_hhmm_rejected(self) -> None:
        for bad in ["9:00", "09:60", "24:00", "ab:cd", "0900"]:
            with self.subTest(bad=bad):
                result = self.invoke(
                    "window", "create", "--db", self.db,
                    "--id", "w", "--start", bad, "--end", "10:00",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    # --- ticket create / SLA ----------------------------------------------

    def test_create_within_window(self) -> None:
        self.create_window()
        result = self.invoke(
            "ticket", "create", "--db", self.db,
            "--id", "t1", "--priority", "high", "--window-id", "w1",
            "--created-at", "2026-03-02T09:15:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload,
            {
                "id": "t1",
                "priority": "high",
                "window_id": "w1",
                "created_at": "2026-03-02T01:15:00Z",
                "deadline": "2026-03-02T02:15:00Z",
            },
        )
        self.assertEqual(self.ticket_count(), 1)

    def test_deadline_accumulates_across_days(self) -> None:
        self.create_window()
        # medium = 240 分钟；17:00 起当天只剩 60 分钟，次日再计 180 分钟到 12:00
        result = self.invoke(
            "ticket", "create", "--db", self.db,
            "--id", "t1", "--priority", "medium", "--window-id", "w1",
            "--created-at", "2026-03-02T17:00:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["deadline"], "2026-03-03T04:00:00Z"
        )

    def test_created_before_window_starts_at_next_start(self) -> None:
        self.create_window()
        # low = 480 分钟；08:00 在时段外，从 09:00 起算，480 <= 540 → 当天 17:00
        result = self.invoke(
            "ticket", "create", "--db", self.db,
            "--id", "t1", "--priority", "low", "--window-id", "w1",
            "--created-at", "2026-03-02T08:00:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["deadline"], "2026-03-02T09:00:00Z"
        )

    def test_created_after_window_starts_next_day(self) -> None:
        self.create_window()
        result = self.invoke(
            "ticket", "create", "--db", self.db,
            "--id", "t1", "--priority", "high", "--window-id", "w1",
            "--created-at", "2026-03-02T20:00:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["deadline"], "2026-03-03T02:00:00Z"
        )

    def test_created_exactly_at_window_end_starts_next_day(self) -> None:
        self.create_window()
        result = self.invoke(
            "ticket", "create", "--db", self.db,
            "--id", "t1", "--priority", "high", "--window-id", "w1",
            "--created-at", "2026-03-02T18:00:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["deadline"], "2026-03-03T02:00:00Z"
        )

    def test_deadline_may_land_exactly_on_window_end(self) -> None:
        self.create_window("wshort", "09:00", "10:00")
        # high = 60 分钟恰为整个时段长度
        result = self.invoke(
            "ticket", "create", "--db", self.db,
            "--id", "t1", "--priority", "high", "--window-id", "wshort",
            "--created-at", "2026-03-02T09:00:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["deadline"], "2026-03-02T02:00:00Z"
        )

    def test_timezone_offset_converted_to_utc(self) -> None:
        self.create_window()
        result = self.invoke(
            "ticket", "create", "--db", self.db,
            "--id", "t1", "--priority", "high", "--window-id", "w1",
            "--created-at", "2026-03-02T09:15:00+05:30",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["created_at"], "2026-03-02T03:45:00Z")
        self.assertEqual(payload["deadline"], "2026-03-02T04:45:00Z")

    def test_invalid_priority_rejected_and_not_written(self) -> None:
        self.create_window()
        for bad in ["HIGH", "High", "urgent", ""]:
            with self.subTest(bad=bad):
                result = self.invoke(
                    "ticket", "create", "--db", self.db,
                    "--id", "t" + bad, "--priority", bad, "--window-id", "w1",
                    "--created-at", "2026-03-02T09:15:00+08:00",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
        self.assertEqual(self.ticket_count(), 0)

    def test_naive_timestamp_rejected(self) -> None:
        self.create_window()
        result = self.invoke(
            "ticket", "create", "--db", self.db,
            "--id", "t1", "--priority", "high", "--window-id", "w1",
            "--created-at", "2026-03-02T09:15:00",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("timezone", result.stderr)
        self.assertEqual(self.ticket_count(), 0)

    def test_malformed_timestamp_rejected(self) -> None:
        self.create_window()
        result = self.invoke(
            "ticket", "create", "--db", self.db,
            "--id", "t1", "--priority", "high", "--window-id", "w1",
            "--created-at", "not-a-time",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.ticket_count(), 0)

    def test_missing_window_fails_create_without_write(self) -> None:
        self.create_window()
        result = self.invoke(
            "ticket", "create", "--db", self.db,
            "--id", "t1", "--priority", "high", "--window-id", "nope",
            "--created-at", "2026-03-02T09:15:00+08:00",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("nope", result.stderr)
        self.assertEqual(self.ticket_count(), 0)

    def test_duplicate_ticket_is_rc2_and_not_overwritten(self) -> None:
        self.create_window()
        first = self.invoke(
            "ticket", "create", "--db", self.db,
            "--id", "t1", "--priority", "high", "--window-id", "w1",
            "--created-at", "2026-03-02T09:15:00+08:00",
        )
        self.assertEqual(first.returncode, 0)
        result = self.invoke(
            "ticket", "create", "--db", self.db,
            "--id", "t1", "--priority", "low", "--window-id", "w1",
            "--created-at", "2026-03-03T09:15:00+08:00",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("t1", result.stderr)
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT priority, created_at, deadline FROM tickets WHERE id = 't1'"
            ).fetchone()
        self.assertEqual(row[0], "high")
        self.assertEqual(row[1], "2026-03-02T01:15:00Z")

    # --- show / list -------------------------------------------------------

    def test_show_returns_json_row(self) -> None:
        self.create_window()
        self.invoke(
            "ticket", "create", "--db", self.db,
            "--id", "t1", "--priority", "high", "--window-id", "w1",
            "--created-at", "2026-03-02T09:15:00+08:00",
        )
        result = self.invoke("ticket", "show", "--db", self.db, "--id", "t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "id": "t1",
                "priority": "high",
                "window_id": "w1",
                "created_at": "2026-03-02T01:15:00Z",
                "deadline": "2026-03-02T02:15:00Z",
                "state": "running",
                "paused_at": None,
                "resumed_at": None,
            },
        )
        self.assertEqual(result.stderr, "")

    def test_show_missing_is_error_with_empty_stdout(self) -> None:
        self.create_window()
        result = self.invoke("ticket", "show", "--db", self.db, "--id", "ghost")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("ghost", result.stderr)

    def test_list_orders_by_created_at_then_id(self) -> None:
        self.create_window()
        specs = [
            ("b", "2026-03-02T17:00:00+08:00"),
            ("a", "2026-03-02T17:00:00+08:00"),  # 同一时刻，按 id 字典序
            ("c", "2026-03-02T09:15:00+08:00"),
        ]
        for tid, created in specs:
            result = self.invoke(
                "ticket", "create", "--db", self.db,
                "--id", tid, "--priority", "high", "--window-id", "w1",
                "--created-at", created,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        result = self.invoke("ticket", "list", "--db", self.db)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual([row["id"] for row in rows], ["c", "a", "b"])
        for row in rows:
            self.assertEqual(
                set(row),
                {
                    "id", "priority", "window_id", "created_at", "deadline",
                    "state", "paused_at", "resumed_at",
                },
            )

    def test_list_empty_ledger_outputs_empty_array(self) -> None:
        result = self.invoke("ticket", "list", "--db", self.db)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")

    # --- pause / resume ----------------------------------------------------

    def create_ticket(
        self,
        tid: str = "t1",
        priority: str = "high",
        created: str = "2026-03-02T09:15:00+08:00",
    ):
        return self.invoke(
            "ticket", "create", "--db", self.db,
            "--id", tid, "--priority", priority, "--window-id", "w1",
            "--created-at", created,
        )

    def ticket_row(self, tid: str = "t1"):
        with sqlite3.connect(self.db) as conn:
            return conn.execute(
                "SELECT state, paused_at, resumed_at, deadline FROM tickets "
                "WHERE id = ?",
                (tid,),
            ).fetchone()

    def test_pause_freezes_deadline_and_stops_counting(self) -> None:
        self.create_window()
        self.create_ticket()
        result = self.invoke(
            "ticket", "pause", "--db", self.db,
            "--id", "t1", "--at", "2026-03-02T09:45:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        expected = {
            "id": "t1",
            "priority": "high",
            "window_id": "w1",
            "created_at": "2026-03-02T01:15:00Z",
            "deadline": "2026-03-02T02:15:00Z",
            "state": "paused",
            "paused_at": "2026-03-02T01:45:00Z",
            "resumed_at": None,
        }
        self.assertEqual(json.loads(result.stdout), expected)
        shown = self.invoke("ticket", "show", "--db", self.db, "--id", "t1")
        self.assertEqual(json.loads(shown.stdout), expected)

    def test_resume_recomputes_deadline_with_remaining_quota(self) -> None:
        self.create_window()
        self.create_ticket()
        # high = 60 分钟；09:15→09:45 已用 30 分钟，14:00 恢复后补 30 分钟
        self.invoke(
            "ticket", "pause", "--db", self.db,
            "--id", "t1", "--at", "2026-03-02T09:45:00+08:00",
        )
        result = self.invoke(
            "ticket", "resume", "--db", self.db,
            "--id", "t1", "--at", "2026-03-02T14:00:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "id": "t1",
                "priority": "high",
                "window_id": "w1",
                "created_at": "2026-03-02T01:15:00Z",
                "deadline": "2026-03-02T06:30:00Z",
                "state": "running",
                "paused_at": None,
                "resumed_at": "2026-03-02T06:00:00Z",
            },
        )

    def test_pause_outside_window_counts_only_service_time(self) -> None:
        self.create_window()
        # low = 480 分钟；17:00 受理，当天计 60 分钟，次日 09:00 起再计 420 分钟
        self.create_ticket(priority="low", created="2026-03-02T17:00:00+08:00")
        # 20:00 在时段外但仍早于 deadline（次日 16:00），只累计时段内 60 分钟
        result = self.invoke(
            "ticket", "pause", "--db", self.db,
            "--id", "t1", "--at", "2026-03-02T20:00:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["deadline"], "2026-03-03T08:00:00Z"
        )
        result = self.invoke(
            "ticket", "resume", "--db", self.db,
            "--id", "t1", "--at", "2026-03-03T10:00:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # 剩余 420 分钟，从 10:00 起算 → 当天 17:00
        self.assertEqual(
            json.loads(result.stdout)["deadline"], "2026-03-03T09:00:00Z"
        )

    def test_multiple_pause_resume_cycles_accumulate(self) -> None:
        self.create_window()
        self.create_ticket(created="2026-03-02T09:00:00+08:00")
        self.invoke(
            "ticket", "pause", "--db", self.db,
            "--id", "t1", "--at", "2026-03-02T09:20:00+08:00",
        )
        self.invoke(
            "ticket", "resume", "--db", self.db,
            "--id", "t1", "--at", "2026-03-02T12:00:00+08:00",
        )
        # 已用 20 分钟，恢复后 deadline 为 12:40
        self.assertEqual(
            self.ticket_row()[3], "2026-03-02T04:40:00Z"
        )
        self.invoke(
            "ticket", "pause", "--db", self.db,
            "--id", "t1", "--at", "2026-03-02T12:30:00+08:00",
        )
        result = self.invoke(
            "ticket", "resume", "--db", self.db,
            "--id", "t1", "--at", "2026-03-02T15:00:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # 共用 20+30=50 分钟，剩余 10 分钟 → 15:10
        payload = json.loads(result.stdout)
        self.assertEqual(payload["deadline"], "2026-03-02T07:10:00Z")
        self.assertEqual(payload["resumed_at"], "2026-03-02T07:00:00Z")
        self.assertEqual(payload["paused_at"], None)

    def test_pause_at_exact_created_and_deadline_allowed(self) -> None:
        self.create_window()
        self.create_ticket()
        result = self.invoke(
            "ticket", "pause", "--db", self.db,
            "--id", "t1", "--at", "2026-03-02T09:15:00+08:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_duplicate_pause_is_rc2_and_not_written(self) -> None:
        self.create_window()
        self.create_ticket()
        self.invoke(
            "ticket", "pause", "--db", self.db,
            "--id", "t1", "--at", "2026-03-02T09:45:00+08:00",
        )
        result = self.invoke(
            "ticket", "pause", "--db", self.db,
            "--id", "t1", "--at", "2026-03-02T10:00:00+08:00",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("t1", result.stderr)
        self.assertEqual(self.ticket_row()[:2], ("paused", "2026-03-02T01:45:00Z"))

    def test_resume_without_pause_is_rc2_and_not_written(self) -> None:
        self.create_window()
        self.create_ticket()
        result = self.invoke(
            "ticket", "resume", "--db", self.db,
            "--id", "t1", "--at", "2026-03-02T10:00:00+08:00",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("t1", result.stderr)
        self.assertEqual(self.ticket_row(), ("running", None, None, "2026-03-02T02:15:00Z"))

    def test_pause_missing_ticket_is_rc1(self) -> None:
        self.create_window()
        result = self.invoke(
            "ticket", "pause", "--db", self.db,
            "--id", "ghost", "--at", "2026-03-02T10:00:00+08:00",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("ghost", result.stderr)

    def test_resume_missing_ticket_is_rc1(self) -> None:
        self.create_window()
        result = self.invoke(
            "ticket", "resume", "--db", self.db,
            "--id", "ghost", "--at", "2026-03-02T10:00:00+08:00",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("ghost", result.stderr)

    def test_pause_before_created_rejected_and_not_written(self) -> None:
        self.create_window()
        self.create_ticket()
        result = self.invoke(
            "ticket", "pause", "--db", self.db,
            "--id", "t1", "--at", "2026-03-02T09:00:00+08:00",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.ticket_row(), ("running", None, None, "2026-03-02T02:15:00Z"))

    def test_pause_after_deadline_rejected_and_not_written(self) -> None:
        self.create_window()
        self.create_ticket()
        result = self.invoke(
            "ticket", "pause", "--db", self.db,
            "--id", "t1", "--at", "2026-03-02T10:16:00+08:00",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.ticket_row(), ("running", None, None, "2026-03-02T02:15:00Z"))

    def test_resume_before_pause_rejected_and_not_written(self) -> None:
        self.create_window()
        self.create_ticket()
        self.invoke(
            "ticket", "pause", "--db", self.db,
            "--id", "t1", "--at", "2026-03-02T09:45:00+08:00",
        )
        result = self.invoke(
            "ticket", "resume", "--db", self.db,
            "--id", "t1", "--at", "2026-03-02T09:30:00+08:00",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.ticket_row()[:2], ("paused", "2026-03-02T01:45:00Z"))

    def test_pause_naive_timestamp_rejected(self) -> None:
        self.create_window()
        self.create_ticket()
        result = self.invoke(
            "ticket", "pause", "--db", self.db,
            "--id", "t1", "--at", "2026-03-02T09:45:00",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("timezone", result.stderr)
        self.assertEqual(self.ticket_row(), ("running", None, None, "2026-03-02T02:15:00Z"))

    def test_pause_resume_accept_equivalent_offsets(self) -> None:
        self.create_window()
        self.create_ticket()
        # 与 09:45+08:00 同一时刻，不同写法
        result = self.invoke(
            "ticket", "pause", "--db", self.db,
            "--id", "t1", "--at", "2026-03-02T01:45:00Z",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["paused_at"], "2026-03-02T01:45:00Z"
        )

    def test_list_reflects_pause_resume_state(self) -> None:
        self.create_window()
        self.create_ticket()
        self.invoke(
            "ticket", "pause", "--db", self.db,
            "--id", "t1", "--at", "2026-03-02T09:45:00+08:00",
        )
        result = self.invoke("ticket", "list", "--db", self.db)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "paused")
        self.assertEqual(rows[0]["paused_at"], "2026-03-02T01:45:00Z")
        self.assertIsNone(rows[0]["resumed_at"])

    # --- global rules ------------------------------------------------------

    def test_db_is_required(self) -> None:
        for invocation in [
            ("ticket", "list"),
            ("ticket", "show", "--id", "t1"),
            ("ticket", "create", "--id", "t1", "--priority", "high",
             "--window-id", "w1", "--created-at", "2026-03-02T09:15:00+08:00"),
            ("window", "create", "--id", "w1", "--start", "09:00", "--end", "18:00"),
            ("ticket", "pause", "--id", "t1", "--at", "2026-03-02T10:00:00+08:00"),
            ("ticket", "resume", "--id", "t1", "--at", "2026-03-02T10:00:00+08:00"),
        ]:
            with self.subTest(invocation=invocation):
                result = self.invoke(*invocation)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("--db", result.stderr)

    def test_unknown_subcommand_is_error(self) -> None:
        result = self.invoke("ticket", "frobnicate", "--db", self.db)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("frobnicate", result.stderr)

    def test_help_outputs_include_new_commands(self) -> None:
        result = self.invoke("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("ticket", result.stdout)
        self.assertIn("window", result.stdout)


if __name__ == "__main__":
    unittest.main()
