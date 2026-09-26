# 工单 SLA 与升级路由

用于本地服务工单 SLA 计时与升级路由的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m sla_desk --help
python3 -m sla_desk --version
python3 -m unittest discover -s tests -v
```

当前支持工单登记与查询：`register` 将工单写入仓库根目录的 `ledger.db`（SQLite），`list` 按 ticket 升序输出全部工单。尚未实现 SLA 计时与暂停、到期升级以及重复工单合并。

```bash
python3 -m sla_desk register --ticket T1 --title "打印机无法打印" --priority high --response-minutes 30
python3 -m sla_desk list
```
