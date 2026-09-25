"""Checks for the documented command-line entry point."""

import json
import os
from pathlib import Path
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


class TicketLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "sla_desk.db"

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, SLA_DESK_DB=str(self.db_path))
        return subprocess.run(
            [sys.executable, "-m", "sla_desk", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def register(self, ticket_id: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        options = {
            "--priority": "P2",
            "--title": "示例工单",
            "--response-minutes": "30",
        }
        options.update(overrides)
        arguments = ["register", "--id", ticket_id]
        for flag, value in options.items():
            arguments += [flag, value]
        return self.invoke(*arguments)

    def test_register_then_status(self) -> None:
        result = self.register("INC-1", **{"--priority": "P1", "--response-minutes": "45"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INC-1", result.stdout)
        self.assertIn("accepted", result.stdout)

        status = self.invoke("status", "--id", "INC-1")
        self.assertEqual(status.returncode, 0, status.stderr)
        ticket = json.loads(status.stdout)
        self.assertEqual(
            list(ticket),
            [
                "id",
                "priority",
                "title",
                "response_minutes",
                "state",
                "created_at",
                "response_due_at",
                "paused_seconds",
            ],
        )
        self.assertEqual(ticket["id"], "INC-1")
        self.assertEqual(ticket["priority"], "P1")
        self.assertEqual(ticket["title"], "示例工单")
        self.assertEqual(ticket["response_minutes"], 45)
        self.assertEqual(ticket["state"], "accepted")
        self.assertEqual(ticket["paused_seconds"], 0)
        self.assertRegex(ticket["created_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertRegex(ticket["response_due_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_response_due_at_is_created_at_plus_minutes(self) -> None:
        self.register("INC-2", **{"--response-minutes": "90"})
        ticket = json.loads(self.invoke("status", "--id", "INC-2").stdout)
        from datetime import datetime, timedelta

        created = datetime.strptime(ticket["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        due = datetime.strptime(ticket["response_due_at"], "%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(due - created, timedelta(minutes=90))

    def test_list_outputs_all_tickets_in_created_order(self) -> None:
        self.assertEqual(self.register("INC-3").returncode, 0)
        self.assertEqual(self.register("INC-4").returncode, 0)
        result = self.invoke("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([ticket["id"] for ticket in lines], ["INC-3", "INC-4"])

    def test_list_with_no_tickets_outputs_nothing(self) -> None:
        result = self.invoke("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_duplicate_id_is_rejected(self) -> None:
        self.assertEqual(self.register("INC-5").returncode, 0)
        result = self.register("INC-5", **{"--priority": "P3", "--response-minutes": "10"})
        self.assertEqual(result.returncode, 3)
        self.assertIn("INC-5", result.stderr)
        ticket = json.loads(self.invoke("status", "--id", "INC-5").stdout)
        self.assertEqual(ticket["priority"], "P2")
        self.assertEqual(ticket["response_minutes"], 30)

    def test_status_of_unknown_id_is_not_found(self) -> None:
        result = self.invoke("status", "--id", "NOPE-1")
        self.assertEqual(result.returncode, 4)
        self.assertIn("NOPE-1", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_invalid_inputs_exit_2_and_do_not_write(self) -> None:
        cases = [
            ("register", "--id", "", "--priority", "P1", "--title", "x", "--response-minutes", "5"),
            ("register", "--id", "A", "--priority", "p1", "--title", "x", "--response-minutes", "5"),
            ("register", "--id", "A", "--priority", "P1", "--title", "", "--response-minutes", "5"),
            ("register", "--id", "A", "--priority", "P1", "--title", "x", "--response-minutes", "0"),
            ("register", "--id", "A", "--priority", "P1", "--title", "x", "--response-minutes", "-3"),
            ("register", "--id", "A", "--priority", "P1", "--title", "x", "--response-minutes", "1.5"),
            ("register", "--id", "A", "--priority", "P1", "--title", "x"),
            ("register", "--priority", "P1", "--title", "x", "--response-minutes", "5"),
            ("register", "--id", "A", "--priority", "P1", "--title", "x", "--response-minutes", "5", "--bogus", "1"),
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 2, arguments)
                self.assertNotEqual(result.stderr, "")
        self.assertEqual(self.invoke("list").stdout, "")

    def test_ticket_id_is_case_sensitive_and_keeps_whitespace(self) -> None:
        self.assertEqual(self.register(" INC-6 ").returncode, 0)
        self.assertEqual(self.register("inc-6").returncode, 0)
        self.assertEqual(self.register("INC-6").returncode, 0)
        listed = [json.loads(line)["id"] for line in self.invoke("list").stdout.splitlines()]
        self.assertEqual(listed, [" INC-6 ", "inc-6", "INC-6"])


class PauseResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "sla_desk.db"
        result = self.invoke(
            "register", "--id", "INC-7", "--priority", "P1",
            "--title", "等待客户补件", "--response-minutes", "30",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, SLA_DESK_DB=str(self.db_path))
        return subprocess.run(
            [sys.executable, "-m", "sla_desk", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def status(self) -> dict:
        result = self.invoke("status", "--id", "INC-7")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_pause_then_resume_shifts_response_due_at(self) -> None:
        before = self.status()
        self.assertEqual(before["state"], "accepted")

        result = self.invoke("pause", "--id", "INC-7", "--at", "2026-09-25T10:00:00Z")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "paused INC-7 paused")
        paused = self.status()
        self.assertEqual(paused["state"], "paused")
        self.assertEqual(paused["paused_seconds"], 0)
        self.assertEqual(paused["response_due_at"], before["response_due_at"])

        result = self.invoke("resume", "--id", "INC-7", "--at", "2026-09-25T10:05:30Z")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "resumed INC-7 accepted")
        resumed = self.status()
        self.assertEqual(resumed["state"], "accepted")
        self.assertEqual(resumed["paused_seconds"], 330)

        from datetime import datetime, timedelta

        fmt = "%Y-%m-%dT%H:%M:%SZ"
        shift = datetime.strptime(resumed["response_due_at"], fmt) - datetime.strptime(
            before["response_due_at"], fmt
        )
        self.assertEqual(shift, timedelta(seconds=330))

    def test_pause_intervals_accumulate(self) -> None:
        self.invoke("pause", "--id", "INC-7", "--at", "2026-09-25T10:00:00Z")
        self.invoke("resume", "--id", "INC-7", "--at", "2026-09-25T10:01:00Z")
        self.invoke("pause", "--id", "INC-7", "--at", "2026-09-25T11:00:00Z")
        self.invoke("resume", "--id", "INC-7", "--at", "2026-09-25T11:02:30Z")
        ticket = self.status()
        self.assertEqual(ticket["paused_seconds"], 60 + 150)

    def test_double_pause_is_conflict_and_keeps_state(self) -> None:
        self.invoke("pause", "--id", "INC-7", "--at", "2026-09-25T10:00:00Z")
        result = self.invoke("pause", "--id", "INC-7", "--at", "2026-09-25T11:00:00Z")
        self.assertEqual(result.returncode, 5)
        self.assertIn("INC-7", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.status()["state"], "paused")
        result = self.invoke("resume", "--id", "INC-7", "--at", "2026-09-25T10:00:30Z")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.status()["paused_seconds"], 30)

    def test_resume_without_pause_is_conflict(self) -> None:
        result = self.invoke("resume", "--id", "INC-7", "--at", "2026-09-25T10:00:00Z")
        self.assertEqual(result.returncode, 5)
        self.assertIn("INC-7", result.stderr)
        self.assertEqual(self.status()["state"], "accepted")
        self.assertEqual(self.status()["paused_seconds"], 0)

    def test_pause_and_resume_unknown_id_is_not_found(self) -> None:
        for command in ("pause", "resume"):
            with self.subTest(command=command):
                result = self.invoke(command, "--id", "NOPE-1", "--at", "2026-09-25T10:00:00Z")
                self.assertEqual(result.returncode, 4)
                self.assertIn("NOPE-1", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_resume_before_pause_is_rejected(self) -> None:
        self.invoke("pause", "--id", "INC-7", "--at", "2026-09-25T10:00:00Z")
        result = self.invoke("resume", "--id", "INC-7", "--at", "2026-09-25T09:59:59Z")
        self.assertEqual(result.returncode, 2)
        self.assertIn("INC-7", result.stderr)
        ticket = self.status()
        self.assertEqual(ticket["state"], "paused")
        self.assertEqual(ticket["paused_seconds"], 0)

    def test_invalid_time_format_exit_2(self) -> None:
        for moment in ("2026-09-25 10:00:00", "2026-09-25T10:00:00", "not-a-time", ""):
            with self.subTest(moment=moment):
                result = self.invoke("pause", "--id", "INC-7", "--at", moment)
                self.assertEqual(result.returncode, 2)
                self.assertNotEqual(result.stderr, "")
        self.assertEqual(self.status()["state"], "accepted")

    def test_list_matches_status_shape_after_pause(self) -> None:
        self.invoke("pause", "--id", "INC-7", "--at", "2026-09-25T10:00:00Z")
        listed = [json.loads(line) for line in self.invoke("list").stdout.splitlines()]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0], self.status())


if __name__ == "__main__":
    unittest.main()
