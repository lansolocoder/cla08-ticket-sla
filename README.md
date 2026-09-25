# 工单 SLA 与升级路由

用于本地服务工单 SLA 计时与升级路由的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m sla_desk --help
python3 -m sla_desk --version
python3 -m unittest discover -s tests -v
```

当前提供工单登记、查询与 SLA 暂停/恢复：工单数据持久化到仓库根目录的 `sla_desk.db`（SQLite，不存在时自动创建；可用环境变量 `SLA_DESK_DB` 覆盖路径）。

```bash
python3 -m sla_desk register --id INC-1 --priority P1 --title "数据库不可用" --response-minutes 30
python3 -m sla_desk status --id INC-1
python3 -m sla_desk list
```

暂停与恢复 SLA 计时（暂停期间 `response_due_at` 相应顺延，累计暂停秒数计入 `paused_seconds`）：

```bash
python3 -m sla_desk pause --id INC-1 --at 2026-09-25T10:00:00Z && python3 -m sla_desk resume --id INC-1 --at 2026-09-25T10:15:00Z
```

尚未实现到期升级以及重复工单合并。
