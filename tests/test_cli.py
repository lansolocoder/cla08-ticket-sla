"""Checks for the documented command-line entry point."""

import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "ticket_ledger.json"


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


class RegisterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = LEDGER.read_bytes() if LEDGER.exists() else None
        LEDGER.unlink(missing_ok=True)

    def tearDown(self) -> None:
        if self._saved is None:
            LEDGER.unlink(missing_ok=True)
        else:
            LEDGER.write_bytes(self._saved)

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "sla_desk", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def read_ledger(self) -> dict:
        return json.loads(LEDGER.read_text(encoding="utf-8"))

    def test_register_writes_record_and_ledger(self) -> None:
        result = self.invoke("register", "T-1", "P2", "2025-01-02T03:04:05Z")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        record = json.loads(result.stdout)
        self.assertEqual(
            record,
            {
                "ticket_id": "T-1",
                "priority": "P2",
                "created_at": "2025-01-02T03:04:05Z",
                "sla_due_at": "2025-01-02T07:04:05Z",
                "elapsed_seconds": 0,
            },
        )
        self.assertEqual(self.read_ledger(), {"T-1": record})

    def test_register_sla_due_at_per_priority(self) -> None:
        expected = {
            "P1": "2025-01-02T04:04:05Z",
            "P2": "2025-01-02T07:04:05Z",
            "P3": "2025-01-02T11:04:05Z",
            "P4": "2025-01-02T19:04:05Z",
        }
        for index, (priority, due) in enumerate(expected.items()):
            with self.subTest(priority=priority):
                result = self.invoke(
                    "register", f"T-{priority}", priority, "2025-01-02T03:04:05Z"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["sla_due_at"], due)
        self.assertEqual(len(self.read_ledger()), 4)

    def test_register_preserves_existing_tickets(self) -> None:
        existing = {
            "T-0": {
                "ticket_id": "T-0",
                "priority": "P1",
                "created_at": "2024-12-31T00:00:00Z",
                "sla_due_at": "2024-12-31T01:00:00Z",
                "elapsed_seconds": 0,
            }
        }
        LEDGER.write_text(json.dumps(existing), encoding="utf-8")
        result = self.invoke("register", "T-1", "P3", "2025-01-02T03:04:05Z")
        self.assertEqual(result.returncode, 0, result.stderr)
        ledger = self.read_ledger()
        self.assertEqual(set(ledger), {"T-0", "T-1"})
        self.assertEqual(ledger["T-0"], existing["T-0"])

    def test_duplicate_ticket_is_an_error(self) -> None:
        first = self.invoke("register", "T-1", "P1", "2025-01-02T03:04:05Z")
        self.assertEqual(first.returncode, 0, first.stderr)
        before = LEDGER.read_bytes()
        result = self.invoke("register", "T-1", "P4", "2025-06-07T08:09:10Z")
        self.assertEqual(result.returncode, 1)
        self.assertIn("T-1", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(LEDGER.read_bytes(), before)

    def test_failed_register_creates_no_ledger_file(self) -> None:
        result = self.invoke("register", "T-1", "P9", "2025-01-02T03:04:05Z")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertFalse(LEDGER.exists())

    def test_invalid_priority_is_an_error(self) -> None:
        for priority in ["p1", "P5", "1", "P1 ", ""]:
            with self.subTest(priority=priority):
                result = self.invoke("register", "T-1", priority, "2025-01-02T03:04:05Z")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertFalse(LEDGER.exists())

    def test_invalid_created_at_is_an_error(self) -> None:
        bad_timestamps = [
            "2025-01-02T03:04:05",       # 缺少 Z
            "2025-01-02T03:04:05+00:00",  # 时区偏移
            "2025-01-02 03:04:05Z",       # 缺少 T
            "2025-01-02T03:04Z",          # 缺少秒
            "2025-1-2T03:04:05Z",         # 非两位数字
            "2025-13-02T03:04:05Z",       # 非法月份
            "2025-01-02T25:04:05Z",       # 非法小时
            "not-a-timestamp",
        ]
        for created_at in bad_timestamps:
            with self.subTest(created_at=created_at):
                result = self.invoke("register", "T-1", "P1", created_at)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertFalse(LEDGER.exists())

    def test_corrupt_ledger_is_an_error(self) -> None:
        for content in ["not json", "[1, 2]", '"text"', '{"T-1": 42}']:
            with self.subTest(content=content):
                LEDGER.write_text(content, encoding="utf-8")
                result = self.invoke("register", "T-1", "P1", "2025-01-02T03:04:05Z")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(LEDGER.read_text(encoding="utf-8"), content)


if __name__ == "__main__":
    unittest.main()
