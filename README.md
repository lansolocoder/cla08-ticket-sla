# 工单 SLA 与升级路由

用于本地服务工单 SLA 计时与升级路由的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m sla_desk --help
python3 -m sla_desk --version
python3 -m unittest discover -s tests -v
```

当前仅提供帮助与版本查询入口；无参数显示帮助，未知参数以非零状态退出。尚未实现工单登记、SLA 计时与暂停、到期升级以及重复工单合并，不会创建业务数据文件。
