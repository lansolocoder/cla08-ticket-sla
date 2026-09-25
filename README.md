# 工单 SLA 与升级路由

用于本地服务工单 SLA 计时与升级路由的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m sla_desk --help
python3 -m sla_desk --version
python3 -m unittest discover -s tests -v
```

当前提供工单登记与查询入口，数据持久化于仓库根目录的 SQLite 文件 `ledger.db`（首次登记时自动创建）。无参数显示帮助，未知参数以非零状态退出。

```bash
# 登记工单（priority 仅接受 low/medium/high/critical）
python3 -m sla_desk register --ticket T1 --title "打印机无法打印" --priority high --response-minutes 30
# 输出：T1 registered

# 列出全部工单，每行 TICKET|PRIORITY|RESPONSE_MINUTES|TITLE，按 ticket Unicode 码点升序
python3 -m sla_desk list
```

退出码约定：`0` 成功；`2` 参数错误（缺参、非法 priority、非法分钟数、list 多余参数等）；`3` 重复 ticket。参数错误不会创建或改动 `ledger.db`。

尚未实现 SLA 计时与暂停、到期升级以及重复工单合并。
