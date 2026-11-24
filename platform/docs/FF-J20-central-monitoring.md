# FF-J20 集中监控、请求日志与故障恢复告警

独立采集进程轮询显式配置的 API 节点，将数据库和存储探测、作业、进程、租约及告警写入共享监控库。相同数据库组使用一份最新完整快照统计作业，独立数据库组的数量相加。现有实时监控和 Prometheus 输出继续提供，管理接口 `/api/v1/runtime/monitor/history`、`/api/v1/runtime/monitor/logs` 提供有界历史。 [采集进程](../server/app/services/monitoring_collector.py) · [监控存储](../server/app/services/monitoring_store.py)

每个 API 以进程启动 ID 和顺序游标输出请求元数据，只保留路由模板、方法、状态、时间和耗时。触发与恢复事件持久化后再投递，重试复用事件 ID。采集失败保持上一次已知故障，直到该节点重新报告。默认保留七天历史，未送达的告警留待重试。 [日志导出](../server/app/services/monitor_export.py) · [管理接口](../server/app/routers/runtime.py)

2026-11-24 两个实际 API 共用 SQLite，真实 Windows shell 作业完成后汇总仍为 1。收集两节点请求日志及八个样本。停止对等 API 后触发告警，接收端第一次响应 503，采集进程重新启动后用同一事件 ID 重试成功。对等 API 重启后收到一次恢复告警，日志区分两个进程启动 ID。隔离 API 已确认停止，测试日志中未找到查询 canary 或两种测试凭据，模型调用 0。 [验收回执](../../tmp/ff-j20-monitor-primary-abeaxhot/monitoring-result.json) · [验收入口](../server/monitoring_collector_acceptance.py)

启用时同时加载生产 Compose 与监控覆盖文件。`MONITORING_NODES` 填节点名、`origin` 及 `cluster`，同一数据库的节点用相同 `cluster`。`MONITORING_EXPORT_TOKEN` 由采集器和导出节点共享。配置 `MONITORING_WEBHOOK` 后投递至获授权的 HTTPS 接收端，本次验收的 HTTP 接收端仅监听回环。 [监控 Compose](../infra/docker-compose.monitoring.yml)
