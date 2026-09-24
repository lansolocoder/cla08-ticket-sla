"""Checks for the documented command-line entry point."""

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


class TicketTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "sla.db"

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

    def open_ticket(self, ticket_id: str = "INC-1") -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "ticket", "open",
            "--id", ticket_id,
            "--priority", "P2",
            "--created", "2026-09-25T08:30:00",
        )

    def test_open_and_show_roundtrip(self) -> None:
        result = self.open_ticket()
        self.assertEqual(result.returncode, 0, result.stderr)

        shown = self.invoke("ticket", "show", "--id", "INC-1")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(
            shown.stdout.splitlines(),
            [
                "id=INC-1",
                "priority=P2",
                "created=2026-09-25T08:30:00",
                "state=open",
            ],
        )

    def test_open_duplicate_id_is_rejected(self) -> None:
        self.assertEqual(self.open_ticket().returncode, 0)
        again = self.invoke(
            "ticket", "open",
            "--id", "INC-1",
            "--priority", "P1",
            "--created", "2026-09-26T00:00:00",
        )
        self.assertEqual(again.returncode, 2)
        self.assertNotEqual(again.stderr, "")
        shown = self.invoke("ticket", "show", "--id", "INC-1")
        self.assertIn("priority=P2", shown.stdout)

    def test_open_validation_failures(self) -> None:
        cases = [
            ("--priority", "P5"),
            ("--priority", "p1"),
            ("--created", "2026-09-25"),
            ("--created", "2026-09-25 08:30:00"),
            ("--created", "2026-13-25T08:30:00"),
            ("--id", ""),
            ("--id", "INC 1"),
        ]
        for option, value in cases:
            with self.subTest(option=option, value=value):
                arguments = [
                    "ticket", "open",
                    "--id", "INC-9",
                    "--priority", "P1",
                    "--created", "2026-09-25T08:30:00",
                ]
                arguments[arguments.index(option) + 1] = value
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 5, result)
                self.assertNotEqual(result.stderr, "")
                self.assertEqual(result.stdout, "")
        self.assertEqual(self.invoke("ticket", "show", "--id", "INC-9").returncode, 4)

    def test_state_transitions(self) -> None:
        self.assertEqual(self.open_ticket().returncode, 0)

        acked = self.invoke("ticket", "ack", "--id", "INC-1")
        self.assertEqual(acked.returncode, 0, acked.stderr)
        self.assertEqual(acked.stdout.strip(), "id=INC-1 state=acknowledged")

        resolved = self.invoke("ticket", "resolve", "--id", "INC-1")
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertEqual(resolved.stdout.strip(), "id=INC-1 state=resolved")

        reopened = self.invoke("ticket", "reopen", "--id", "INC-1")
        self.assertEqual(reopened.returncode, 0, reopened.stderr)
        self.assertEqual(reopened.stdout.strip(), "id=INC-1 state=open")

    def test_illegal_transitions_are_rejected(self) -> None:
        self.assertEqual(self.open_ticket().returncode, 0)

        direct = self.invoke("ticket", "resolve", "--id", "INC-1")
        self.assertEqual(direct.returncode, 3)
        self.assertNotEqual(direct.stderr, "")

        self.assertEqual(self.invoke("ticket", "ack", "--id", "INC-1").returncode, 0)
        repeated = self.invoke("ticket", "ack", "--id", "INC-1")
        self.assertEqual(repeated.returncode, 3)

        self.assertEqual(self.invoke("ticket", "resolve", "--id", "INC-1").returncode, 0)
        backwards = self.invoke("ticket", "ack", "--id", "INC-1")
        self.assertEqual(backwards.returncode, 3)

        shown = self.invoke("ticket", "show", "--id", "INC-1")
        self.assertIn("state=resolved", shown.stdout)

    def test_missing_ticket_is_an_error(self) -> None:
        for action in ("show", "ack", "resolve", "reopen"):
            with self.subTest(action=action):
                result = self.invoke("ticket", action, "--id", "NOPE-1")
                self.assertEqual(result.returncode, 4, result)
                self.assertNotEqual(result.stderr, "")
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
