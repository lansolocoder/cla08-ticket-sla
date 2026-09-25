# 工单 SLA 与升级路由

用于本地服务工单 SLA 计时与升级路由的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m sla_desk --help
python3 -m sla_desk --version
python3 -m unittest discover -s tests -v
```

当前提供工单登记、查询、SLA 暂停/恢复与到期升级检查：工单数据持久化到仓库根目录的 `sla_desk.db`（SQLite，不存在时自动创建；可用环境变量 `SLA_DESK_DB` 覆盖路径）。

```bash
python3 -m sla_desk register --id INC-1 --priority P1 --title "数据库不可用" --response-minutes 30
python3 -m sla_desk status --id INC-1
python3 -m sla_desk list
```

暂停与恢复 SLA 计时（暂停期间不计入响应时限，`response_due_at` 按累计暂停秒数顺延）：

```bash
python3 -m sla_desk register --id INC-7 --priority P2 --title "等待客户补件" --response-minutes 30
python3 -m sla_desk pause --id INC-7 --at 2026-09-25T10:00:00Z    # 输出 paused INC-7 paused，status 中 state 变为 paused
python3 -m sla_desk resume --id INC-7 --at 2026-09-25T10:05:30Z   # 输出 resumed INC-7 accepted，paused_seconds 累计 330 秒
```

到期升级检查（对全部工单逐一判断：`accepted` 且观察时刻不早于 `response_due_at` 的工单转入 `escalated` 并记录升级；`paused` 工单不升级，恢复后截止顺延；检查原子完成，任一写入失败则全部保持原样）：

```bash
python3 -m sla_desk escalate --at 2026-09-25T12:00:00Z   # 逐行输出本次升级的工单 JSON；无升级时无输出
python3 -m sla_desk status --id INC-7                     # escalations 字段按记录先后列出 {at, level}
```

升级级别按优先级与超出 `response_due_at` 的整秒数定级：P1 超过 3600 秒、P2 超过 7200 秒、P3/P4 超过 14400 秒为 L2，否则 L1。观察时刻格式非法或早于工单 `created_at` 时报错（退出码 2）且不改动任何数据。

尚未实现重复工单合并。
