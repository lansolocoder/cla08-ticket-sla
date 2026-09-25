"""Checks for the documented command-line entry point."""

import json
from pathlib import Path
import re
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "sla_desk.db"
RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        DB.unlink(missing_ok=True)

    def tearDown(self) -> None:
        DB.unlink(missing_ok=True)

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "sla_desk", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def register(self, ticket_id: str, minutes: str = "30") -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "register",
            "--id", ticket_id,
            "--priority", "P2",
            "--title", "网关超时",
            "--response-minutes", minutes,
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

    def test_unknown_subcommand_is_an_error(self) -> None:
        result = self.invoke("frobnicate")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_register_and_status(self) -> None:
        result = self.register("INC-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INC-1", result.stdout)
        self.assertIn("accepted", result.stdout)

        status = self.invoke("status", "--id", "INC-1")
        self.assertEqual(status.returncode, 0, status.stderr)
        record = json.loads(status.stdout)
        self.assertEqual(
            list(record),
            ["id", "priority", "title", "response_minutes", "state",
             "created_at", "response_due_at", "paused_seconds"],
        )
        self.assertEqual(record["id"], "INC-1")
        self.assertEqual(record["priority"], "P2")
        self.assertEqual(record["title"], "网关超时")
        self.assertEqual(record["response_minutes"], 30)
        self.assertEqual(record["state"], "accepted")
        self.assertEqual(record["paused_seconds"], 0)
        self.assertRegex(record["created_at"], RFC3339_UTC)
        self.assertRegex(record["response_due_at"], RFC3339_UTC)

    def test_register_duplicate_id_is_rejected(self) -> None:
        self.assertEqual(self.register("INC-1").returncode, 0)
        duplicate = self.invoke(
            "register",
            "--id", "INC-1",
            "--priority", "P1",
            "--title", "别的标题",
            "--response-minutes", "5",
        )
        self.assertEqual(duplicate.returncode, 3)
        self.assertIn("INC-1", duplicate.stderr)
        self.assertEqual(duplicate.stdout, "")
        # 已有记录保持不变
        record = json.loads(self.invoke("status", "--id", "INC-1").stdout)
        self.assertEqual(record["priority"], "P2")
        self.assertEqual(record["title"], "网关超时")
        self.assertEqual(record["response_minutes"], 30)

    def test_register_invalid_input(self) -> None:
        cases = [
            ("register", "--id", "", "--priority", "P1", "--title", "t", "--response-minutes", "5"),
            ("register", "--id", "A", "--priority", "p1", "--title", "t", "--response-minutes", "5"),
            ("register", "--id", "A", "--priority", "P1", "--title", "", "--response-minutes", "5"),
            ("register", "--id", "A", "--priority", "P1", "--title", "t", "--response-minutes", "0"),
            ("register", "--id", "A", "--priority", "P1", "--title", "t", "--response-minutes", "-3"),
            ("register", "--id", "A", "--priority", "P1", "--title", "t", "--response-minutes", "x"),
            ("register", "--id", "A", "--priority", "P1", "--response-minutes", "5"),
            ("register", "--priority", "P1", "--title", "t", "--response-minutes", "5"),
            ("register", "--id", "A", "--priority", "P1", "--title", "t",
             "--response-minutes", "5", "--bogus", "1"),
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 2, arguments)
                self.assertNotEqual(result.stderr, "")
        self.assertFalse(DB.exists() and self._ticket_count() > 0)

    def _ticket_count(self) -> int:
        import sqlite3
        with sqlite3.connect(DB) as connection:
            return connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]

    def test_status_unknown_id(self) -> None:
        result = self.invoke("status", "--id", "NOPE")
        self.assertEqual(result.returncode, 4)
        self.assertIn("NOPE", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_list_empty_and_ordered(self) -> None:
        empty = self.invoke("list")
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertEqual(empty.stdout, "")

        self.assertEqual(self.register("INC-1").returncode, 0)
        self.assertEqual(self.register("INC-2").returncode, 0)
        result = self.invoke("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 2)
        records = [json.loads(line) for line in lines]
        self.assertEqual([r["id"] for r in records], ["INC-1", "INC-2"])
        created = [r["created_at"] for r in records]
        self.assertEqual(created, sorted(created))

    def test_ticket_id_is_case_and_whitespace_sensitive(self) -> None:
        self.assertEqual(self.register("INC-1").returncode, 0)
        self.assertEqual(self.register("inc-1").returncode, 0)
        self.assertEqual(self.register(" INC-1 ").returncode, 0)
        record = json.loads(self.invoke("status", "--id", " INC-1 ").stdout)
        self.assertEqual(record["id"], " INC-1 ")


if __name__ == "__main__":
    unittest.main()
