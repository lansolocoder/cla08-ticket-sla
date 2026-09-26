"""Checks for the documented command-line entry point."""

from pathlib import Path
import subprocess
import sys
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
    db_path = ROOT / "ledger.db"

    def setUp(self) -> None:
        self.db_path.unlink(missing_ok=True)

    def tearDown(self) -> None:
        self.db_path.unlink(missing_ok=True)

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "sla_desk", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def register(self, ticket: str = "T1", title: str = "打印机无法打印",
                 priority: str = "high", minutes: str = "30") -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "register", "--ticket", ticket, "--title", title,
            "--priority", priority, "--response-minutes", minutes,
        )

    def test_register_creates_db_and_reports(self) -> None:
        result = self.register()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "T1 registered\n")
        self.assertEqual(result.stderr, "")
        self.assertTrue(self.db_path.exists())

    def test_duplicate_ticket_is_rejected(self) -> None:
        self.register()
        result = self.register(title="另一条工单")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "duplicate ticket: T1\n")
        listed = self.invoke("list")
        self.assertEqual(listed.stdout, "T1|high|30|打印机无法打印\n")

    def test_invalid_priority_is_rejected_without_db(self) -> None:
        for priority in ["HIGH", "High", "urgent", ""]:
            with self.subTest(priority=priority):
                result = self.register(priority=priority)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, f"invalid priority: {priority}\n")
                self.assertFalse(self.db_path.exists())

    def test_missing_argument_is_rejected_without_db(self) -> None:
        result = self.invoke("register", "--ticket", "T1", "--title", "x",
                             "--priority", "low")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.db_path.exists())

    def test_invalid_response_minutes_is_rejected_without_db(self) -> None:
        for minutes in ["0", "-5", "1.5", "abc", ""]:
            with self.subTest(minutes=minutes):
                result = self.register(minutes=minutes)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertFalse(self.db_path.exists())

    def test_list_outputs_sorted_pipe_format(self) -> None:
        self.register(ticket="T2", title="  保留空白  ", priority="low", minutes="5")
        self.register(ticket="T1", title="打印机无法打印", priority="high", minutes="30")
        result = self.invoke("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "T1|high|30|打印机无法打印\nT2|low|5|  保留空白  \n",
        )
        self.assertEqual(result.stderr, "")

    def test_list_empty_outputs_nothing(self) -> None:
        result = self.invoke("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertFalse(self.db_path.exists())

    def test_list_rejects_extra_arguments(self) -> None:
        result = self.invoke("list", "extra")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
