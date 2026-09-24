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

## 查询

按 id 查询，stdout 输出与 create 相同形状的一行 JSON：

```bash
python3 -m sla_desk ticket show --db ledger.db --id T-001
```

列出全部工单，按 `created_at` 升序（相同时按 id 字典序）输出 JSON 数组；
空台账输出 `[]`：

```bash
python3 -m sla_desk ticket list --db ledger.db
```
