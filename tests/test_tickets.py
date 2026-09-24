"""End-to-end checks for ticket registration, viewing, merging and listing."""

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TicketTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)
        # Tests must not leave a database behind inside the repository.
        self.assertFalse((ROOT / "sla-desk.db").exists())

    def tearDown(self) -> None:
        self._tmp.cleanup()
        self.assertFalse((ROOT / "sla-desk.db").exists())

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "sla_desk", *arguments],
            cwd=self.cwd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def register(
        self,
        customer_ref: str = "CR-1",
        title: str = "打印机故障",
        email: str = "user@example.com",
        priority: str = "P2",
        submitted_at: str = "2026-09-24T10:00:00+08:00",
    ) -> subprocess.CompletedProcess[str]:
        return self.invoke(
            "register",
            "--customer-ref", customer_ref,
            "--title", title,
            "--email", email,
            "--priority", priority,
            "--submitted-at", submitted_at,
        )

    def test_register_creates_incrementing_tickets(self) -> None:
        result = self.register()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("工单号: T1", result.stdout)
        self.assertIn("状态: open", result.stdout)
        self.assertIn("提交时间: 2026-09-24T10:00:00+08:00", result.stdout)

        result = self.register(customer_ref="CR-2", title="网络中断", priority="P1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("工单号: T2", result.stdout)

    def test_show_outputs_all_fields_and_persists_across_runs(self) -> None:
        result = self.register()
        self.assertEqual(result.returncode, 0, result.stderr)

        result = self.invoke("show", "T1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("工单号: T1", result.stdout)
        self.assertIn("客户请求号: CR-1", result.stdout)
        self.assertIn("标题: 打印机故障", result.stdout)
        self.assertIn("报障人: user@example.com", result.stdout)
        self.assertIn("优先级: P2", result.stdout)
        self.assertIn("提交时间: 2026-09-24T10:00:00+08:00", result.stdout)
        self.assertIn("状态: open", result.stdout)

    def test_show_unknown_ticket_is_an_error(self) -> None:
        result = self.invoke("show", "T99")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("T99", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_identical_retry_is_idempotent(self) -> None:
        first = self.register()
        self.assertEqual(first.returncode, 0, first.stderr)

        retry = self.register()
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertIn("工单号: T1", retry.stdout)
        self.assertNotIn("T2", retry.stdout)

        listing = self.invoke("list")
        lines = [line for line in listing.stdout.splitlines() if line]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("T1\t"))

    def test_retry_with_different_field_is_rejected(self) -> None:
        first = self.register()
        self.assertEqual(first.returncode, 0, first.stderr)

        retry = self.register(priority="P3")
        self.assertNotEqual(retry.returncode, 0)
        self.assertIn("CR-1", retry.stderr)
        self.assertEqual(retry.stdout, "")

        # The original record is untouched and still T1.
        shown = self.invoke("show", "T1")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("优先级: P2", shown.stdout)

    def test_all_invalid_fields_are_reported_together(self) -> None:
        result = self.register(
            title="   ",
            email="not-an-email",
            priority="P9",
            submitted_at="not-a-timestamp",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("标题", result.stderr)
        self.assertIn("邮箱", result.stderr)
        self.assertIn("优先级", result.stderr)
        self.assertIn("提交时间", result.stderr)
        self.assertEqual(result.stdout, "")

        # Nothing was written: listing is empty and no T1 exists.
        self.assertEqual(self.invoke("list").stdout, "")
        self.assertNotEqual(self.invoke("show", "T1").returncode, 0)

    def test_merge_moves_ticket_and_keeps_master_unchanged(self) -> None:
        self.assertEqual(self.register().returncode, 0)
        self.assertEqual(
            self.register(customer_ref="CR-2", title="网络中断", priority="P1").returncode,
            0,
        )

        result = self.invoke("merge", "T2", "T1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("T2", result.stdout)
        self.assertIn("T1", result.stdout)
        self.assertIn("状态: merged", result.stdout)

        merged = self.invoke("show", "T2")
        self.assertEqual(merged.returncode, 0, merged.stderr)
        self.assertIn("状态: merged", merged.stdout)
        self.assertIn("主工单号: T1", merged.stdout)
        self.assertIn("合并时间:", merged.stdout)

        master = self.invoke("show", "T1")
        self.assertEqual(master.returncode, 0, master.stderr)
        self.assertIn("状态: open", master.stdout)
        self.assertIn("标题: 打印机故障", master.stdout)
        self.assertNotIn("主工单号", master.stdout)

    def test_merge_rejects_missing_self_and_already_merged(self) -> None:
        self.assertEqual(self.register().returncode, 0)
        self.assertEqual(
            self.register(customer_ref="CR-2", title="网络中断").returncode, 0
        )
        self.assertEqual(
            self.register(customer_ref="CR-3", title="显示器黑屏").returncode, 0
        )

        for args in [
            ("merge", "T9", "T1"),       # merged ticket missing
            ("merge", "T1", "T9"),       # master missing
            ("merge", "T1", "T1"),       # same ticket
        ]:
            with self.subTest(args=args):
                result = self.invoke(*args)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                for ticket_no in ("T1", "T2", "T3"):
                    self.assertIn(
                        "状态: open",
                        self.invoke("show", ticket_no).stdout,
                    )

        self.assertEqual(self.invoke("merge", "T2", "T1").returncode, 0)

        # Merged ticket cannot be merged again, nor act as master.
        again = self.invoke("merge", "T2", "T3")
        self.assertNotEqual(again.returncode, 0)
        into_merged = self.invoke("merge", "T3", "T2")
        self.assertNotEqual(into_merged.returncode, 0)

        # Uninvolved tickets stay open.
        self.assertIn("状态: open", self.invoke("show", "T3").stdout)

    def test_customer_ref_of_merged_ticket_cannot_be_reused(self) -> None:
        self.assertEqual(self.register().returncode, 0)
        self.assertEqual(self.register(customer_ref="CR-2").returncode, 0)
        self.assertEqual(self.invoke("merge", "T2", "T1").returncode, 0)

        # Same content, but the request number now belongs to a merged ticket.
        reuse = self.register(customer_ref="CR-2")
        self.assertNotEqual(reuse.returncode, 0)
        self.assertNotEqual(self.invoke("show", "T3").returncode, 0)

    def test_list_shows_only_open_in_ascending_order(self) -> None:
        self.assertEqual(self.register(customer_ref="CR-1", priority="P3").returncode, 0)
        self.assertEqual(self.register(customer_ref="CR-2", priority="P1").returncode, 0)
        self.assertEqual(self.register(customer_ref="CR-3", priority="P2").returncode, 0)
        self.assertEqual(self.invoke("merge", "T2", "T1").returncode, 0)

        listing = self.invoke("list")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        lines = [line for line in listing.stdout.splitlines() if line]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].split("\t")[:2], ["T1", "P3"])
        self.assertTrue(lines[0].endswith("\topen"))
        self.assertEqual(lines[1].split("\t")[:2], ["T3", "P2"])
        self.assertTrue(lines[1].endswith("\topen"))
        self.assertNotIn("T2", listing.stdout)

    def test_submitted_time_text_is_stored_verbatim_with_offset(self) -> None:
        text = "2026-01-02T03:04:05-05:30"
        result = self.register(submitted_at=text)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"提交时间: {text}", self.invoke("show", "T1").stdout)

    def test_database_file_lives_in_working_directory(self) -> None:
        result = self.register()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.cwd / "sla-desk.db").exists())


if __name__ == "__main__":
    unittest.main()
