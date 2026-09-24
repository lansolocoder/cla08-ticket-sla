# 工单 SLA 与升级路由

用于本地服务工单 SLA 计时与升级路由的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m sla_desk --help
python3 -m sla_desk --version
python3 -m unittest discover -s tests -v
```

## 工单登记与状态流转

工单数据持久化在仓库根目录的 `sla.db`（标准库 sqlite3），跨进程可读回。

```bash
# 登记新工单：--id 唯一工单号，--priority 取 P1|P2|P3|P4，
# --created 为 YYYY-MM-DDTHH:MM:SS 的 UTC 时间戳
python3 -m sla_desk ticket open --id INC-1001 --priority P2 --created 2026-09-25T08:30:00

# 查看工单：stdout 逐行输出 id/priority/created/state 的 key=value
python3 -m sla_desk ticket show --id INC-1001

# 状态流转，每次变更后 stdout 输出一行 id=<工单号> state=<新状态>
python3 -m sla_desk ticket ack --id INC-1001      # open -> acknowledged
python3 -m sla_desk ticket resolve --id INC-1001  # acknowledged -> resolved
python3 -m sla_desk ticket reopen --id INC-1001   # resolved -> open（重开）
```

状态字面值只有 `open`、`acknowledged`、`resolved`。合法转换仅限
`open -> acknowledged`、`acknowledged -> resolved`、`resolved -> open`；
其余转换（如 `open -> resolved`、`resolved -> acknowledged`、重复对同状态转换）一律拒绝，
工单记录保持原值不变。

## 退出码

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 2 | 用法错误（未知子命令或参数），或 `ticket open` 的 `--id` 已存在（不覆盖既有记录） |
| 3 | 非法状态转换，记录保持原值不变 |
| 4 | `show`/`ack`/`resolve`/`reopen` 的 `--id` 不存在（不创建记录） |
| 5 | 字段校验失败：`--priority` 非 P1\|P2\|P3\|P4、`--created` 非合法时间戳、`--id` 为空或含空白字符 |

所有错误均写 stderr，stdout 无业务输出，且数据不变。
