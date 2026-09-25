# 工单 SLA 与升级路由

用于本地服务工单 SLA 计时与升级路由的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m sla_desk --help
python3 -m sla_desk --version
python3 -m unittest discover -s tests -v
```

无参数显示帮助；未知参数/子命令以非零状态退出并在 stderr 说明。
除 `--help`/`--version` 外，所有调用都必须通过 `--db` 指定 SQLite 台账路径。

## 服务时段

服务时段定义为每天重复的本地时间区间（`HH:MM`），`--end` 必须晚于 `--start`，
不允许跨零点。同一 `--id` 重复登记报错（退出码 2），旧记录不变。

```bash
python3 -m sla_desk window create --db ledger.db \
    --id business-hours --start 09:00 --end 18:00
```

## 工单登记

`ticket create` 接收：

- `--id`：工单 id，重复登记报错（退出码 2），不覆盖旧记录
- `--priority`：`high` | `medium` | `low`（精确小写），其他取值报错且不写库
- `--window-id`：引用已登记的服务时段，不存在则整条创建失败
- `--created-at`：带时区偏移的 ISO8601 时刻，如 `2026-03-02T09:15:00+08:00`

```bash
python3 -m sla_desk ticket create --db ledger.db \
    --id T-001 --priority high --window-id business-hours \
    --created-at 2026-03-02T09:15:00+08:00
```

成功时退出码 0，stdout 输出一行工单 JSON：

```json
{"id":"T-001","priority":"high","window_id":"business-hours","created_at":"2026-03-02T01:15:00Z","deadline":"2026-03-02T02:15:00Z"}
```

失败时 stdout 为空、退出码非零，台账不会出现半条记录。

### 响应截止（deadline）

SLA 响应额度按优先级为 high/medium/low 分别 60/240/480 分钟。从 `created-at`
起只累计落在服务时段内的时间，跨多个天的时段逐日累加；若受理时刻在时段之外，
则从下一个时段起点开始累计。deadline 可落在时段边界内，也可恰在时段终点。

时间字段统一输出 UTC ISO8601：带 `Z`、秒级精度、不带小数秒。

## 暂停与恢复

`ticket pause` / `ticket resume` 各接收 `--db`、`--id` 与 `--at`
（带时区偏移的 ISO8601 时刻），计时口径与 deadline 相同：只累计服务时段内的时间。

```bash
python3 -m sla_desk ticket pause  --db ledger.db --id T-001 --at 2026-03-02T10:00:00+08:00
python3 -m sla_desk ticket resume --db ledger.db --id T-001 --at 2026-03-02T14:00:00+08:00
```

- `pause` 在 `--at` 时刻停止累计；`--at` 早于受理时刻或晚于当前 deadline 时拒绝且不写库
- `resume` 在 `--at` 恢复计时，按 `resumed_at` 之后服务时段内的时间补足剩余额度，
  重算新截止；`--at` 早于暂停时刻（或此前已生效的更早暂停时刻）时拒绝且不写库
- 重复 pause（已暂停）/ 重复 resume（未暂停）报错退出码 2；工单不存在报错退出码 1
- 成功时退出码 0，stdout 输出与 `ticket show` 相同形状的一行 JSON

## 升级路由

`ticket escalate` 接收 `--db`、`--id`、`--at`（带时区偏移的 ISO8601 时刻）与
`--to`（非空目标队列名），登记一条不可变升级记录。升级不影响 SLA 计时：
`state`、`paused_at`、`resumed_at`、`deadline`、`consumed_seconds` 均不改变，
工单处于暂停状态时也可升级。

```bash
python3 -m sla_desk ticket escalate --db ledger.db --id T-001 \
    --to l2-oncall --at 2026-03-02T10:20:00+08:00
```

每条记录含 `id`（工单 id）、`to`（目标队列）、`at`（UTC ISO8601）、`reason`：

- `--at` 不早于工单当前有效 `deadline` 时 `reason` 为 `deadline-exceeded`，否则为 `manual`
- `--at` 早于 `created_at` 时拒绝（退出码 2）不写库
- 同一工单可多次升级，同一 `--to` 重复升级照常新增记录（不覆盖不合并）
- 但 `--at` 与该工单最近一条升级记录时刻完全相同时拒绝（退出码 2）且不写新记录；
  因此连续两次内容完全相同（`--at`、`--to` 均相同）的升级只有第一次生效
- 工单不存在报错退出码 1；成功时退出码 0，stdout 输出该条记录的一行 JSON

查询工单的全部升级记录，按 `at` 升序（同时刻按写入先后），从未升级输出 `[]`；
工单不存在报错退出码 1：

```bash
python3 -m sla_desk ticket escalations --db ledger.db --id T-001
```

```json
[{"id":"T-001","to":"l2-oncall","at":"2026-03-02T02:20:00Z","reason":"deadline-exceeded"}]
```

## 查询

按 id 查询，stdout 输出一行 JSON，包含 `id`、`priority`、`window_id`、`created_at`、
`deadline`、`state`、`paused_at`、`resumed_at`。`state` 为 `running` 或 `paused`；
未暂停或从未暂停过时 `paused_at`/`resumed_at` 为 `null`。`deadline` 恒为按当前记录
重算的有效截止：running 且从未暂停时与受理时算法一致，暂停中为按暂停时刻冻结的截止，
恢复后为补足剩余额度后的新截止。

```bash
python3 -m sla_desk ticket show --db ledger.db --id T-001
```

列出全部工单，数组元素形状与 show 相同，按 `created_at` 升序（相同时按 id 字典序）；
空台账输出 `[]`：

```bash
python3 -m sla_desk ticket list --db ledger.db
```
