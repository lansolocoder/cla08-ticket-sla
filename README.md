# 工单 SLA 与升级路由

用于本地服务工单 SLA 计时与升级路由的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m sla_desk --help
python3 -m sla_desk --version
python3 -m sla_desk register INC-100 P1 2025-01-02T03:04:05Z
python3 -m unittest discover -s tests -v
```

## 工单登记

`register` 子命令登记新工单：位置参数依次为工单号、优先级（仅 `P1`/`P2`/`P3`/`P4`）、UTC 创建时间（仅接受形如 `2025-01-02T03:04:05Z` 的精确到秒、以 `Z` 结尾的 ISO 8601 字面值）。登记成功时向 stdout 输出一行 JSON 记录（`ticket_id`、`priority`、`created_at`、`sla_due_at`、`elapsed_seconds`），并把记录写入仓库根目录的 `ticket_ledger.json`（键为工单号）。`sla_due_at` 按优先级响应目标（P1 3600 秒、P2 14400 秒、P3 28800 秒、P4 57600 秒）从创建时间起算。

重复工单号、非法优先级或时间格式、以及 ledger 文件损坏时，登记失败：stderr 报错、退出码 1、`ticket_ledger.json` 不被创建或改动。

SLA 计时与暂停、到期升级以及重复工单合并尚未实现。
