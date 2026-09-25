# 工单 SLA 与升级路由

用于本地服务工单 SLA 计时与升级路由的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m sla_desk --help
python3 -m sla_desk --version
python3 -m sla_desk register --id INC-1 --priority P1 --title "网关超时" --response-minutes 30
python3 -m sla_desk status --id INC-1
python3 -m sla_desk list
python3 -m unittest discover -s tests -v
```

工单数据持久化在仓库根目录的 `sla_desk.db`（SQLite，首次使用时自动创建）。`register` 登记新工单（重复工单号以退出码 3 拒绝），`status` 输出单个工单的 JSON 详情（含 `response_due_at` 与 `paused_seconds`），`list` 按 `created_at` 升序逐行输出全部工单。非法输入以退出码 2 报错且不写库，查询不存在的工单号以退出码 4 报错。尚未实现 SLA 暂停、到期升级与重复工单合并。
