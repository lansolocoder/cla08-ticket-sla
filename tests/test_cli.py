"""Checks for the documented command-line entry point."""

from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "sla.db"


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
        DB_PATH.unlink(missing_ok=True)

    def tearDown(self) -> None:
        DB_PATH.unlink(missing_ok=True)

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "sla_desk", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def open_ticket(self, ticket_id: str = "T-1") -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "ticket", "open",
            "--id", ticket_id,
            "--priority", "P2",
            "--created", "2026-09-25T08:30:00",
        )

    def test_open_then_show_roundtrip(self) -> None:
        result = self.open_ticket()
        self.assertEqual(result.returncode, 0, result.stderr)
        shown = self.invoke("ticket", "show", "--id", "T-1")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(
            shown.stdout,
            "id=T-1\npriority=P2\ncreated=2026-09-25T08:30:00\nstate=open\n",
        )
        self.assertEqual(shown.stderr, "")

    def test_duplicate_open_is_rejected(self) -> None:
        self.open_ticket()
        again = self.invoke(
            "ticket", "open",
            "--id", "T-1",
            "--priority", "P1",
            "--created", "2026-09-26T00:00:00",
        )
        self.assertEqual(again.returncode, 2)
        self.assertEqual(again.stdout, "")
        self.assertNotEqual(again.stderr, "")
        shown = self.invoke("ticket", "show", "--id", "T-1")
        self.assertIn("priority=P2", shown.stdout)

    def test_ack_then_resolve(self) -> None:
        self.open_ticket()
        acked = self.invoke("ticket", "ack", "--id", "T-1")
        self.assertEqual(acked.returncode, 0, acked.stderr)
        self.assertEqual(acked.stdout, "id=T-1 state=acknowledged\n")
        resolved = self.invoke("ticket", "resolve", "--id", "T-1")
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertEqual(resolved.stdout, "id=T-1 state=resolved\n")
        shown = self.invoke("ticket", "show", "--id", "T-1")
        self.assertIn("state=resolved", shown.stdout)

    def test_reopen_after_resolve(self) -> None:
        self.open_ticket()
        self.invoke("ticket", "ack", "--id", "T-1")
        self.invoke("ticket", "resolve", "--id", "T-1")
        reopened = self.invoke("ticket", "reopen", "--id", "T-1")
        self.assertEqual(reopened.returncode, 0, reopened.stderr)
        self.assertEqual(reopened.stdout, "id=T-1 state=open\n")

    def test_illegal_transitions_are_rejected(self) -> None:
        self.open_ticket()
        skipped = self.invoke("ticket", "resolve", "--id", "T-1")
        self.assertEqual(skipped.returncode, 3)
        self.assertEqual(skipped.stdout, "")
        self.invoke("ticket", "ack", "--id", "T-1")
        repeated = self.invoke("ticket", "ack", "--id", "T-1")
        self.assertEqual(repeated.returncode, 3)
        self.assertEqual(repeated.stdout, "")
        self.invoke("ticket", "resolve", "--id", "T-1")
        backwards = self.invoke("ticket", "ack", "--id", "T-1")
        self.assertEqual(backwards.returncode, 3)
        shown = self.invoke("ticket", "show", "--id", "T-1")
        self.assertIn("state=resolved", shown.stdout)

    def test_missing_ticket_is_not_found(self) -> None:
        for arguments in [
            ("ticket", "show", "--id", "NOPE"),
            ("ticket", "ack", "--id", "NOPE"),
            ("ticket", "resolve", "--id", "NOPE"),
        ]:
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 4)
                self.assertEqual(result.stdout, "")
                self.assertNotEqual(result.stderr, "")

    def test_invalid_fields_are_rejected(self) -> None:
        cases = [
            ("ticket", "open", "--id", "T-9", "--priority", "P5",
             "--created", "2026-09-25T08:30:00"),
            ("ticket", "open", "--id", "T-9", "--priority", "p1",
             "--created", "2026-09-25T08:30:00"),
            ("ticket", "open", "--id", "T-9", "--priority", "P1",
             "--created", "2026-09-25 08:30:00"),
            ("ticket", "open", "--id", "T-9", "--priority", "P1",
             "--created", "2026-13-25T08:30:00"),
            ("ticket", "open", "--id", "", "--priority", "P1",
             "--created", "2026-09-25T08:30:00"),
            ("ticket", "open", "--id", "T 9", "--priority", "P1",
             "--created", "2026-09-25T08:30:00"),
            ("ticket", "show", "--id", " "),
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 5)
                self.assertEqual(result.stdout, "")
                self.assertNotEqual(result.stderr, "")
        shown = self.invoke("ticket", "show", "--id", "T-9")
        self.assertEqual(shown.returncode, 4)


if __name__ == "__main__":
    unittest.main()
