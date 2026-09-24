"""端到端测试：登记、重试、校验、查看、合并、列出与持久化。

每个用例在独立临时目录中运行，sla-desk.db 只落在该目录内。
"""

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TicketCliTests(unittest.TestCase):
    def invoke(self, cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "sla_desk", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def register(self, cwd: Path, request_no: str = "CR-1", title: str = "网络中断",
                 email: str = "ops@example.com", priority: str = "P1",
                 submitted_at: str = "2026-09-24T10:00:00+08:00") -> subprocess.CompletedProcess[str]:
        return self.invoke(
            cwd, "register",
            "--request-no", request_no,
            "--title", title,
            "--email", email,
            "--priority", priority,
            "--submitted-at", submitted_at,
        )

    # ------------------------------------------------------------------
    # 登记
    # ------------------------------------------------------------------

    def test_register_creates_incrementing_tickets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            r1 = self.register(cwd, request_no="CR-1")
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertIn("工单号: T1", r1.stdout)
            self.assertIn("状态: open", r1.stdout)
            self.assertIn("提交时间: 2026-09-24T10:00:00+08:00", r1.stdout)

            r2 = self.register(cwd, request_no="CR-2", title="打印机故障",
                               email="help@example.com", priority="P3",
                               submitted_at="2026-09-24T09:30:00Z")
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertIn("工单号: T2", r2.stdout)
            # 提交时间文本按原样保存（Z 不被改写为 +00:00）
            self.assertIn("提交时间: 2026-09-24T09:30:00Z", r2.stdout)
            self.assertTrue((cwd / "sla-desk.db").exists())

    def test_data_persists_across_invocations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(self.register(cwd, request_no="CR-1").returncode, 0)
            show = self.invoke(cwd, "show", "T1")
            self.assertEqual(show.returncode, 0, show.stderr)
            self.assertIn("客户请求号: CR-1", show.stdout)
            self.assertIn("报障人: ops@example.com", show.stdout)
            self.assertIn("优先级: P1", show.stdout)
            self.assertIn("状态: open", show.stdout)

    def test_identical_retry_returns_original_without_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, request_no="CR-1")
            retry = self.register(cwd, request_no="CR-1")
            self.assertEqual(retry.returncode, 0, retry.stderr)
            self.assertIn("工单号: T1", retry.stdout)
            # 重试不新建工单：下一张仍是 T2
            other = self.register(cwd, request_no="CR-2", title="另一单")
            self.assertIn("工单号: T2", other.stdout)

    def test_retry_with_different_fields_fails_and_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, request_no="CR-1", title="原标题")
            for field_args in [
                ("--title",),
                ("--email",),
                ("--priority",),
                ("--submitted-at",),
            ]:
                with self.subTest(field=field_args[0]):
                    result = self.invoke(
                        cwd, "register",
                        "--request-no", "CR-1",
                        "--title",
                        "改了标题" if field_args[0] == "--title" else "原标题",
                        "--email",
                        "other@example.com" if field_args[0] == "--email" else "ops@example.com",
                        "--priority",
                        "P2" if field_args[0] == "--priority" else "P1",
                        "--submitted-at",
                        "2026-09-24T11:00:00+08:00"
                        if field_args[0] == "--submitted-at"
                        else "2026-09-24T10:00:00+08:00",
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("CR-1", result.stderr)
            # 原记录字段未被改写，且没有失败登记残留
            show = self.invoke(cwd, "show", "T1")
            self.assertIn("标题: 原标题", show.stdout)
            self.assertIn("报障人: ops@example.com", show.stdout)
            self.assertIn("优先级: P1", show.stdout)
            listing = self.invoke(cwd, "list")
            self.assertEqual(listing.stdout.count("\n"), 1)

    def test_invalid_fields_are_all_reported_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = self.register(
                cwd, title="   ", email="no-at-sign",
                priority="P9", submitted_at="not-a-time",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertIn("标题不能为空", result.stderr)
            self.assertIn("邮箱", result.stderr)
            self.assertIn("优先级", result.stderr)
            self.assertIn("提交时间", result.stderr)
            # 失败不留任何记录：库中没有工单
            listing = self.invoke(cwd, "list")
            self.assertEqual(listing.returncode, 0)
            self.assertEqual(listing.stdout, "")

    def test_failed_validation_does_not_consume_ticket_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            bad = self.register(cwd, request_no="CR-X", email="bad",
                                priority="P9", submitted_at="nope")
            self.assertNotEqual(bad.returncode, 0)
            good = self.register(cwd, request_no="CR-1")
            self.assertIn("工单号: T1", good.stdout)

    # ------------------------------------------------------------------
    # 查看
    # ------------------------------------------------------------------

    def test_show_unknown_ticket_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = self.invoke(cwd, "show", "T9")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("T9", result.stderr)
            self.assertEqual(result.stdout, "")

    def test_show_malformed_ticket_id_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.invoke(Path(tmp), "show", "XYZ")
            self.assertNotEqual(result.returncode, 0)

    # ------------------------------------------------------------------
    # 合并
    # ------------------------------------------------------------------

    def test_merge_moves_source_to_merged_and_keeps_master(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, request_no="CR-1", title="主单")
            self.register(cwd, request_no="CR-2", title="被并单",
                          email="x@y.com", priority="P2",
                          submitted_at="2026-09-24T09:00:00+08:00")
            result = self.invoke(cwd, "merge", "T2", "T1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("工单号: T2", result.stdout)
            self.assertIn("状态: merged", result.stdout)
            self.assertIn("主工单号: T1", result.stdout)
            self.assertIn("合并时间:", result.stdout)

            source = self.invoke(cwd, "show", "T2")
            self.assertIn("状态: merged", source.stdout)
            self.assertIn("主工单号: T1", source.stdout)

            master = self.invoke(cwd, "show", "T1")
            self.assertIn("状态: open", master.stdout)
            self.assertIn("标题: 主单", master.stdout)
            self.assertNotIn("主工单号", master.stdout)

            # list 只剩主单
            listing = self.invoke(cwd, "list")
            self.assertEqual(listing.stdout, "T1\tP1\t主单\topen\n")

    def test_merged_request_no_cannot_be_registered_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, request_no="CR-1")
            self.register(cwd, request_no="CR-2", title="dup")
            self.invoke(cwd, "merge", "T2", "T1")
            again = self.register(cwd, request_no="CR-2", title="全新内容",
                                  email="z@z.com", priority="P4",
                                  submitted_at="2026-09-25T00:00:00+08:00")
            self.assertNotEqual(again.returncode, 0)
            # 原 T2 未被改写
            show = self.invoke(cwd, "show", "T2")
            self.assertIn("标题: dup", show.stdout)

    def test_merge_rejects_same_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd)
            result = self.invoke(cwd, "merge", "T1", "T1")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("自身", result.stderr)
            show = self.invoke(cwd, "show", "T1")
            self.assertIn("状态: open", show.stdout)

    def test_merge_rejects_unknown_ticket_without_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd)
            for args in [("T9", "T1"), ("T1", "T9")]:
                with self.subTest(args=args):
                    result = self.invoke(cwd, "merge", *args)
                    self.assertNotEqual(result.returncode, 0)
            self.assertIn("状态: open", self.invoke(cwd, "show", "T1").stdout)

    def test_already_merged_ticket_cannot_merge_again_either_side(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, request_no="CR-1")
            self.register(cwd, request_no="CR-2", title="b")
            self.register(cwd, request_no="CR-3", title="c")
            self.assertEqual(self.invoke(cwd, "merge", "T2", "T1").returncode, 0)

            # 已合并的单不能作为被并单
            as_source = self.invoke(cwd, "merge", "T2", "T3")
            self.assertNotEqual(as_source.returncode, 0)
            # 已合并的单不能作为主单
            as_target = self.invoke(cwd, "merge", "T3", "T2")
            self.assertNotEqual(as_target.returncode, 0)
            # T3 仍为 open，未被任何失败操作影响
            self.assertIn("状态: open", self.invoke(cwd, "show", "T3").stdout)

    # ------------------------------------------------------------------
    # 列出
    # ------------------------------------------------------------------

    def test_list_empty_is_quiet_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.invoke(Path(tmp), "list")
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

    def test_list_is_sorted_by_ticket_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, request_no="CR-1", title="first", priority="P3")
            self.register(cwd, request_no="CR-2", title="second", priority="P1")
            result = self.invoke(cwd, "list")
        lines = result.stdout.strip().splitlines()
        self.assertEqual(lines[0].split("\t")[:2], ["T1", "P3"])
        self.assertEqual(lines[1].split("\t")[:2], ["T2", "P1"])


if __name__ == "__main__":
    unittest.main()
