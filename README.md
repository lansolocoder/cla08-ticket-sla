# 工单 SLA 与升级路由

用于本地服务工单 SLA 计时与升级路由的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m sla_desk --help
python3 -m sla_desk --version
python3 -m unittest discover -s tests -v
```

## 用法约定

- `--help` / `--version` 无需其他参数；无参数时显示帮助（退出码 0）。
- 除 help/version 外，所有命令都必须通过 `--db PATH` 指定 SQLite 台账路径
  （`--db` 写在子命令前后均可）；缺失时以非零退出码退出并在 stderr 说明。
- 未知子命令/参数、校验失败均以非零退出码退出且 stdout 为空；
  重复登记窗口或工单退出码为 **2**，记录不存在为 **3**。
- 失败不会在台账中留下半条记录，也不会凭空创建台账文件。

## 服务时段

窗口定义为**每天**一个本地挂钟时间区间，`HH:MM` 格式，结束必须严格晚于
开始，不支持跨零点区间：

```bash
python3 -m sla_desk window create --db ledger.sqlite \
    --id business-hours --start 09:00 --end 18:00
```

窗口按运行机器的本地时区解释。

## 工单登记

```bash
python3 -m sla_desk ticket create --db ledger.sqlite \
    --id T-001 \
    --priority high \
    --window-id business-hours \
    --created-at 2026-03-02T09:15:00+08:00
```

- `--priority` 只接受精确小写的 `high|medium|low`，对应响应额度
  60 / 240 / 480 分钟。
- `--created-at` 必须是带显式时区偏移的 ISO8601 时刻（不接受裸时间或 `Z`）。
- `--window-id` 引用的时段必须已存在，否则整条创建失败、台账不变。
- 同一 id 重复登记窗口或工单一律报错（退出码 2），不覆盖旧记录。

成功时 stdout 输出一行 JSON，退出码 0：

```json
{"id":"T-001","priority":"high","window_id":"business-hours","created_at":"2026-03-02T01:15:00Z","deadline":"2026-03-02T02:15:00Z"}
```

## 响应截止时间（deadline）

- 从 `created-at` 起**只累计服务时段内**的时间，跨多个时段逐日累加，
  累计满额度即为 deadline；deadline 可落在时段边界内或恰在终点。
- `created-at` 不在时段内（含恰在终点、早于起点、晚于终点）时，
  从下一个时段的起点开始累计。
- 输出时间统一为 UTC ISO8601、秒级精度、`Z` 结尾、不带小数秒。

## 查询

按 id 查询，输出与 create 成功时同形状的一行 JSON：

```bash
python3 -m sla_desk ticket show --db ledger.sqlite --id T-001
```

按 `created_at` 升序列出全部工单（相同时按 id 字典序），输出 JSON 数组，
空台账输出 `[]`：

```bash
python3 -m sla_desk ticket list --db ledger.sqlite
```
