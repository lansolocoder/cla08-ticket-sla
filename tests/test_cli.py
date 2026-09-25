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

    def pause(self, tid: str, at: str):
        return self.invoke("ticket", "pause", "--db", self.db, "--id", tid, "--at", at)

    def resume(self, tid: str, at: str):
        return self.invoke(
            "ticket", "resume", "--db", self.db, "--id", tid, "--at", at
        )

    def show(self, tid: str) -> dict:
        result = self.invoke("ticket", "show", "--db", self.db, "--id", tid)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_pause_freezes_deadline_and_sets_state(self) -> None:
        self.create_window()
        self.create_ticket()
        result = self.pause("t1", "2026-03-02T09:45:00+08:00")
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
                "state": "paused",
                "paused_at": "2026-03-02T01:45:00Z",
                "resumed_at": None,
            },
        )
        self.assertEqual(self.show("t1"), payload)

    def test_pause_outside_window_counts_only_window_time(self) -> None:
        self.create_window()
        # medium = 240 分钟；16:00 受理，当天计 120 分钟，次日再计 120 分钟到 11:00
        self.create_ticket(priority="medium", created="2026-03-02T16:00:00+08:00")
        result = self.pause("t1", "2026-03-02T20:00:00+08:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "paused")
        self.assertEqual(payload["paused_at"], "2026-03-02T12:00:00Z")
        self.assertEqual(payload["deadline"], "2026-03-03T03:00:00Z")

    def test_resume_recomputes_deadline_from_remaining_quota(self) -> None:
        self.create_window()
        self.create_ticket()
        self.assertEqual(self.pause("t1", "2026-03-02T09:45:00+08:00").returncode, 0)
        result = self.resume("t1", "2026-03-02T14:00:00+08:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload,
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
        self.assertEqual(self.show("t1"), payload)

    def test_resume_spills_remaining_quota_into_next_days(self) -> None:
        self.create_window()
        # medium = 240 分钟；17:00 受理，原截止次日 12:00
        self.create_ticket(priority="medium", created="2026-03-02T17:00:00+08:00")
        # 暂停于 17:30，已耗 30 分钟，剩余 210 分钟
        self.assertEqual(self.pause("t1", "2026-03-02T17:30:00+08:00").returncode, 0)
        # 次日 16:00 恢复：当天计 120 分钟，余 90 分钟到再次日 10:30
        result = self.resume("t1", "2026-03-03T16:00:00+08:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["deadline"], "2026-03-04T02:30:00Z")

    def test_multiple_pause_resume_cycles_accumulate_consumed(self) -> None:
        self.create_window()
        # high = 60 分钟；09:00 受理，原截止 10:00
        self.create_ticket(created="2026-03-02T09:00:00+08:00")
        self.assertEqual(self.pause("t1", "2026-03-02T09:20:00+08:00").returncode, 0)
        result = self.resume("t1", "2026-03-02T10:00:00+08:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["deadline"], "2026-03-02T02:40:00Z")
        # 再暂停于 10:30：累计已耗 20 + 30 = 50 分钟，剩余 10 分钟
        result = self.pause("t1", "2026-03-02T10:30:00+08:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "paused")
        self.assertEqual(payload["resumed_at"], "2026-03-02T02:00:00Z")
        self.assertEqual(payload["deadline"], "2026-03-02T02:40:00Z")
        # 12:00 恢复，截止 12:10
        result = self.resume("t1", "2026-03-02T12:00:00+08:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["deadline"], "2026-03-02T04:10:00Z")

    def test_repeated_pause_is_rc2_and_not_written(self) -> None:
        self.create_window()
        self.create_ticket()
        self.assertEqual(self.pause("t1", "2026-03-02T09:45:00+08:00").returncode, 0)
        before = self.show("t1")
        result = self.pause("t1", "2026-03-02T09:50:00+08:00")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("t1", result.stderr)
        self.assertEqual(self.show("t1"), before)

    def test_resume_without_pause_is_rc2_and_not_written(self) -> None:
        self.create_window()
        self.create_ticket()
        before = self.show("t1")
        result = self.resume("t1", "2026-03-02T10:00:00+08:00")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("t1", result.stderr)
        self.assertEqual(self.show("t1"), before)

    def test_pause_and_resume_missing_ticket_is_rc1(self) -> None:
        self.create_window()
        for action in ("pause", "resume"):
            with self.subTest(action=action):
                result = self.invoke(
                    "ticket", action, "--db", self.db,
                    "--id", "ghost", "--at", "2026-03-02T10:00:00+08:00",
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("ghost", result.stderr)

    def test_pause_before_created_rejected_and_not_written(self) -> None:
        self.create_window()
        self.create_ticket()
        before = self.show("t1")
        result = self.pause("t1", "2026-03-02T09:00:00+08:00")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.show("t1"), before)

    def test_pause_after_deadline_rejected_and_not_written(self) -> None:
        self.create_window()
        self.create_ticket()
        before = self.show("t1")
        result = self.pause("t1", "2026-03-02T10:16:00+08:00")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.show("t1"), before)

    def test_pause_exactly_at_deadline_allowed(self) -> None:
        self.create_window()
        self.create_ticket()
        result = self.pause("t1", "2026-03-02T10:15:00+08:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "paused")
        self.assertEqual(payload["deadline"], "2026-03-02T02:15:00Z")

    def test_resume_before_pause_rejected_and_not_written(self) -> None:
        self.create_window()
        self.create_ticket()
        self.assertEqual(self.pause("t1", "2026-03-02T09:45:00+08:00").returncode, 0)
        before = self.show("t1")
        result = self.resume("t1", "2026-03-02T09:30:00+08:00")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.show("t1"), before)

    def test_pause_resume_naive_timestamp_rejected(self) -> None:
        self.create_window()
        self.create_ticket()
        for action in ("pause", "resume"):
            with self.subTest(action=action):
                result = self.invoke(
                    "ticket", action, "--db", self.db,
                    "--id", "t1", "--at", "2026-03-02T10:00:00",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("timezone", result.stderr)

    def test_list_reflects_pause_resume_state(self) -> None:
        self.create_window()
        self.create_ticket("a", created="2026-03-02T09:00:00+08:00")
        self.create_ticket("b", created="2026-03-02T09:30:00+08:00")
        self.assertEqual(self.pause("a", "2026-03-02T09:20:00+08:00").returncode, 0)
        result = self.invoke("ticket", "list", "--db", self.db)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual([row["id"] for row in rows], ["a", "b"])
        self.assertEqual(rows[0]["state"], "paused")
        self.assertEqual(rows[0]["paused_at"], "2026-03-02T01:20:00Z")
        self.assertEqual(rows[1]["state"], "running")
        self.assertIsNone(rows[1]["paused_at"])
        self.assertIsNone(rows[1]["resumed_at"])

    # --- escalate / escalations -------------------------------------------

    def escalate(self, tid: str, to: str, at: str):
        return self.invoke(
            "ticket", "escalate", "--db", self.db,
            "--id", tid, "--to", to, "--at", at,
        )

    def escalations(self, tid: str):
        return self.invoke("ticket", "escalations", "--db", self.db, "--id", tid)

    def escalation_count(self, tid: str = "t1") -> int:
        with sqlite3.connect(self.db) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM escalations WHERE ticket_id = ?", (tid,)
            ).fetchone()[0]

    def test_manual_escalation_before_deadline(self) -> None:
        self.create_window()
        self.create_ticket()
        # deadline 为 10:15+08；09:45 在截止前 → manual
        result = self.escalate("t1", "l2", "2026-03-02T09:45:00+08:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "id": "t1",
                "to": "l2",
                "at": "2026-03-02T01:45:00Z",
                "reason": "manual",
            },
        )

    def test_escalation_exactly_at_deadline_is_deadline_exceeded(self) -> None:
        self.create_window()
        self.create_ticket()
        result = self.escalate("t1", "l2", "2026-03-02T10:15:00+08:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["reason"], "deadline-exceeded")

    def test_escalation_after_deadline_is_deadline_exceeded(self) -> None:
        self.create_window()
        self.create_ticket()
        result = self.escalate("t1", "l2", "2026-03-03T10:00:00+08:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["reason"], "deadline-exceeded")
        self.assertEqual(
            json.loads(result.stdout)["at"], "2026-03-03T02:00:00Z"
        )

    def test_escalations_empty_outputs_empty_array(self) -> None:
        self.create_window()
        self.create_ticket()
        result = self.escalations("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")

    def test_escalations_ordered_by_at_then_write_order(self) -> None:
        self.create_window()
        self.create_ticket()
        # 09:45、10:00（manual），10:15（deadline-exceeded），乱序写入同一时刻两条
        self.assertEqual(
            self.escalate("t1", "c", "2026-03-02T10:15:00+08:00").returncode, 0
        )
        self.assertEqual(
            self.escalate("t1", "a", "2026-03-02T09:45:00+08:00").returncode, 0
        )
        self.assertEqual(
            self.escalate("t1", "b", "2026-03-02T10:00:00+08:00").returncode, 0
        )
        self.assertEqual(
            self.escalate("t1", "a2", "2026-03-02T09:45:00+08:00").returncode, 0
        )
        result = self.escalations("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual(
            [(r["at"], r["to"]) for r in rows],
            [
                ("2026-03-02T01:45:00Z", "a"),
                ("2026-03-02T01:45:00Z", "a2"),
                ("2026-03-02T02:00:00Z", "b"),
                ("2026-03-02T02:15:00Z", "c"),
            ],
        )
        for row in rows:
            self.assertEqual(set(row), {"id", "to", "at", "reason"})
            self.assertEqual(row["id"], "t1")

    def test_repeated_same_to_new_at_adds_record(self) -> None:
        self.create_window()
        self.create_ticket()
        self.assertEqual(
            self.escalate("t1", "l2", "2026-03-02T09:45:00+08:00").returncode, 0
        )
        result = self.escalate("t1", "l2", "2026-03-02T10:00:00+08:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.escalation_count(), 2)

    def test_identical_consecutive_request_is_rc2_and_not_written(self) -> None:
        self.create_window()
        self.create_ticket()
        first = self.escalate("t1", "l2", "2026-03-02T09:45:00+08:00")
        self.assertEqual(first.returncode, 0)
        result = self.escalate("t1", "l2", "2026-03-02T09:45:00+08:00")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("t1", result.stderr)
        self.assertEqual(self.escalation_count(), 1)

    def test_same_at_as_latest_different_to_also_rc2(self) -> None:
        self.create_window()
        self.create_ticket()
        self.assertEqual(
            self.escalate("t1", "l2", "2026-03-02T09:45:00+08:00").returncode, 0
        )
        result = self.escalate("t1", "l3", "2026-03-02T09:45:00+08:00")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.escalation_count(), 1)

    def test_same_at_as_non_latest_record_is_allowed(self) -> None:
        self.create_window()
        self.create_ticket()
        self.assertEqual(
            self.escalate("t1", "l2", "2026-03-02T09:45:00+08:00").returncode, 0
        )
        self.assertEqual(
            self.escalate("t1", "l2", "2026-03-02T10:00:00+08:00").returncode, 0
        )
        # 与最早一条同时刻，但最近一条是 10:00 → 允许
        result = self.escalate("t1", "l3", "2026-03-02T09:45:00+08:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.escalation_count(), 3)

    def test_escalation_before_created_rejected_rc2(self) -> None:
        self.create_window()
        self.create_ticket()
        before = self.show("t1")
        result = self.escalate("t1", "l2", "2026-03-02T09:00:00+08:00")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.escalation_count(), 0)
        self.assertEqual(self.show("t1"), before)

    def test_escalation_exactly_at_created_allowed(self) -> None:
        self.create_window()
        self.create_ticket()
        result = self.escalate("t1", "l2", "2026-03-02T09:15:00+08:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["at"], "2026-03-02T01:15:00Z")

    def test_escalate_missing_ticket_is_rc1(self) -> None:
        self.create_window()
        result = self.escalate("ghost", "l2", "2026-03-02T10:00:00+08:00")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("ghost", result.stderr)

    def test_escalations_missing_ticket_is_rc1(self) -> None:
        self.create_window()
        result = self.escalations("ghost")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("ghost", result.stderr)

    def test_escalate_naive_or_malformed_at_rejected_not_written(self) -> None:
        self.create_window()
        self.create_ticket()
        for bad in ["2026-03-02T10:00:00", "garbage"]:
            with self.subTest(bad=bad):
                result = self.escalate("t1", "l2", bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
        self.assertEqual(self.escalation_count(), 0)

    def test_empty_to_rejected(self) -> None:
        self.create_window()
        self.create_ticket()
        result = self.escalate("t1", "", "2026-03-02T10:00:00+08:00")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.escalation_count(), 0)

    def test_escalation_does_not_change_sla_state(self) -> None:
        self.create_window()
        self.create_ticket()
        self.assertEqual(self.pause("t1", "2026-03-02T09:45:00+08:00").returncode, 0)
        before = self.show("t1")
        # 暂停中且在截止后升级，SLA 字段保持不变
        result = self.escalate("t1", "l2", "2026-03-02T11:00:00+08:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["reason"], "deadline-exceeded")
        self.assertEqual(self.show("t1"), before)
        self.assertEqual(before["state"], "paused")

    def test_failed_escalation_leaves_escalations_unchanged(self) -> None:
        self.create_window()
        self.create_ticket()
        self.assertEqual(
            self.escalate("t1", "l2", "2026-03-02T09:45:00+08:00").returncode, 0
        )
        before = self.escalations("t1").stdout
        result = self.escalate("t1", "l3", "2026-03-02T09:45:00+08:00")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.escalations("t1").stdout, before)

    # --- global rules ------------------------------------------------------

    def test_db_is_required(self) -> None:
        for invocation in [
            ("ticket", "list"),
            ("ticket", "show", "--id", "t1"),
            ("ticket", "create", "--id", "t1", "--priority", "high",
             "--window-id", "w1", "--created-at", "2026-03-02T09:15:00+08:00"),
            ("ticket", "escalate", "--id", "t1", "--to", "l2",
             "--at", "2026-03-02T09:15:00+08:00"),
            ("ticket", "escalations", "--id", "t1"),
            ("window", "create", "--id", "w1", "--start", "09:00", "--end", "18:00"),
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
