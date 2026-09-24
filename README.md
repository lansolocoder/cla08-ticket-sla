# 工单 SLA 与升级路由

用于本地服务工单 SLA 计时与升级路由的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m sla_desk --help
python3 -m sla_desk --version
python3 -m sla_desk register TICKET_ID PRIORITY CREATED_AT
python3 -m unittest discover -s tests -v
```

当前提供帮助、版本查询与工单登记入口；无参数显示帮助，未知参数以非零状态退出。

## 工单登记

```bash
python3 -m sla_desk register T-001 P1 2025-01-02T03:04:05Z
```

- `TICKET_ID`：工单号，原样作为记录键。
- `PRIORITY`：仅接受 `P1`、`P2`、`P3`、`P4` 四个大写字面值（小写等其它写法一律拒绝）。
- `CREATED_AT`：UTC 创建时间，仅接受精确到秒、以 `Z` 结尾的 ISO 8601 字面值（如 `2025-01-02T03:04:05Z`），不接受其它时区偏移，不做本地时区推断。

登记成功后向 stdout 输出一行 JSON 记录，字段固定为 `ticket_id`、`priority`、`created_at`、`sla_due_at`、`elapsed_seconds`（整数 0）。`sla_due_at` 为创建时间加上响应目标秒数：P1 为 3600、P2 为 14400、P3 为 28800、P4 为 57600。

业务数据写入仓库根目录的 `ticket_ledger.json`（JSON 对象，键为工单号）。同一工单号重复登记、优先级或创建时间不合法、ledger 文件不是合法 JSON 对象或结构不符时，均向 stderr 报错并以退出码 1 退出，stdout 为空，且不创建或改动 `ticket_ledger.json`。

SLA 计时与暂停、到期升级以及重复工单合并尚未实现。
