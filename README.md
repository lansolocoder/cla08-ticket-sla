# 工单 SLA 与升级路由

用于本地服务工单 SLA 计时与升级路由的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m sla_desk --help
python3 -m sla_desk --version
python3 -m unittest discover -s tests -v
```

无参数显示帮助，未知子命令或参数以非零状态退出（错误写 stderr，stdout 无业务输出）。

## 工单登记与状态流转

工单数据持久化在仓库根目录的 `sla.db`（标准库 sqlite3），跨进程可读回。

```bash
# 登记新工单（状态初始为 open）
python3 -m sla_desk ticket open --id INC-1 --priority P2 --created 2026-09-25T08:30:00

# 查看工单（stdout 逐行 key=value：id、priority、created、state）
python3 -m sla_desk ticket show --id INC-1

# 状态流转，每次变更后 stdout 输出一行 id=<工单号> state=<新状态>
python3 -m sla_desk ticket ack --id INC-1      # open -> acknowledged
python3 -m sla_desk ticket resolve --id INC-1  # acknowledged -> resolved
python3 -m sla_desk ticket reopen --id INC-1   # resolved -> open（重开）
```

状态字面值只有 `open`、`acknowledged`、`resolved`（大小写完全一致）。合法转换仅限
`open→acknowledged`、`acknowledged→resolved`、`resolved→open`；其余转换一律拒绝，
工单记录保持原值不变。

## 退出码

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 2 | `ticket open` 的 `--id` 已存在（不覆盖既有记录）；或 argparse 层面的未知子命令/参数错误 |
| 3 | 非法状态转换（如 open→resolved、resolved→acknowledged、重复对同状态转换） |
| 4 | `show`/`ack`/`resolve`/`reopen` 的 `--id` 不存在（不创建记录） |
| 5 | 参数校验失败：`--priority` 非 P1\|P2\|P3\|P4、`--created` 非合法 UTC 时间戳、`--id` 为空或含空白字符 |

所有校验失败与非法转换都不会改动 `sla.db` 中的数据。

尚未实现 SLA 计时与暂停、到期升级以及重复工单合并。
