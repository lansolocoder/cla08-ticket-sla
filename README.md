# 工单 SLA 与升级路由

用于本地服务工单 SLA 计时与升级路由的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m sla_desk --help
python3 -m sla_desk --version
python3 -m unittest discover -s tests -v
```

当前提供工单登记与查询：工单数据持久化到仓库根目录的 `sla_desk.db`（SQLite，不存在时自动创建；可用环境变量 `SLA_DESK_DB` 覆盖路径）。

```bash
python3 -m sla_desk register --id INC-1 --priority P1 --title "数据库不可用" --response-minutes 30
python3 -m sla_desk status --id INC-1
python3 -m sla_desk list
```

尚未实现 SLA 暂停、到期升级以及重复工单合并。
