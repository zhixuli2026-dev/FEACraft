# FF-J14 Redis 跨实例通知

启用 `VIBEFLOW_DISTRIBUTED_JOBS=true` 的 API 使用现有 Redis 配置转发算例事件到达提示、`scene.available` 和账户级的 `quota.warning`、`runtime.degraded`。分开部署的 API 若共用一套 Redis，使用 `VIBEFLOW_EVENT_BUS_NAMESPACE` 配置各自命名空间。同一部署的各副本使用相同值。 [事件总线](../server/app/services/eventbus.py) · [Compose 配置](../infra/docker-compose.prod.yml)

持久事件在 PostgreSQL 或 SQLite 中按算例序号读回。Redis 提示只携带进程来源、算例 ID 和序号。WebSocket 收到提示后继续用同一数据库游标分页，并检查用户状态和算例读权限。提示丢失时，每半秒的数据库轮询继续。临时通知保留原用户和算例范围，只用于即时提醒。每个进程的本地及发送队列上限均为 256，Redis 重连期间可丢弃这类提醒。 [WebSocket](../server/app/routers/events.py) · [数据库读回](../server/app/services/event_replay.py)

## 2026-11-25 实测

- 两个独立 API 使用同一隔离 PostgreSQL 16 和实际 Redis 7.4.12，HTTP 限流采用默认内存后端。经 API A 触发真实可视化配额拒绝，A 与 B 的 WebSocket 各收到同一条通知。另一账号的合法连接未收到通知，访问该算例的握手返回 HTTP 403。
- 在 API A 写入实际文件，API B 读回持久文件事件。独立 Redis 订阅端收到对应序号的三个字段。停止隔离 Redis 后继续写文件，B 仍读回顺序递增的事件。复机后两 API 恢复订阅，再次成功转发准入通知。替换 B 后以已保存的游标连接新副本，读回 Redis 停机期间提交的原事件。
- 第一轮使用默认关闭分布式作业的测试夹具，未取得订阅。其后一次使用动态端口的 Redis 复机观察超时。最终使用固定本机端口，实际核对复机后的端口与两份订阅。重用上一次的测试存储还触发了算例创建 500。恢复事务可能使用户 ORM 对象失效，创建流程现使用事先读取的账号 ID，恢复后在锁内重新核对工作区归属。将同一份旧测试存储复制给新的隔离 API 后，创建返回 200，实际文件树包含 22 个条目。
- Compose 渲染与核心 API 回归通过。原 11 个 Docker 服务均继续运行，测试 Redis、PostgreSQL 与 API 全部退出。模型调用 0 次。

[双 API 回执](../../tmp/ff-j14-redis-events-20261125/result.json) · [算例恢复复验](../../tmp/ff-j14-redis-events-20261125/provision-recovery-result.json) · [Compose 回执](../../tmp/ff-j14-redis-events-20261125/compose-regression/summary.json) · [核心 API](../../tmp/ff-j14-redis-events-20261125/core-api-after-recovery-fix.log)

本轮完成同物理宿主的 Redis 通知、数据库游标重放和副本替换。Nginx 负载均衡、PTY、可视化和 worker 替换另有实际同宿主验收。跨物理主机按原计划保留。 [本机滚动替换](FF-J14-rolling-replicas.md)
