# FF-J19 按用户和模型配置限流

平台入口额度按登录用户或来访 IP 计量。通过模型配置权限校验后，模型发现、连接测试、翻译、压缩和 API Agent 每次对外请求共用同一份用户与 profile 额度。受管原生 CLI 按新回合准入计量，带平台凭据的交互终端按启动准入计量。手工终端内部自行发出的网络请求不经过平台网关。重连和原生幂等回执查询沿用已有执行。 [限流实现](../server/app/services/rate_limit.py) · [API 网关](../server/app/routers/llm.py)

单实例默认使用内存额度。多实例配置 `RATE_LIMIT_BACKEND=redis` 并指向同一认证 Redis；各实例通过 Redis 服务器时间和原子更新共用额度，空闲额度键两分钟过期。`RATE_LIMIT_PROFILE_PER_MINUTE` 设置每用户每 profile 每分钟额度。API 的 Redis 不可用时返回带 `Retry-After` 的 503，健康探针仍可用。对外代理只在 `TRUSTED_PROXY_IPS` 列出的转发入口受信，共享配额以转发校验后的地址计量。 [生产配置](../infra/docker-compose.prod.yml)

2026-11-24，两实例共用隔离 SQLite 与实际认证 Redis。初始实例调用真实 DeepSeek 成功后，第二实例以同用户同 profile 请求收到 429 和 58 秒 `Retry-After`。同用户其他 profile 及共享同一 profile 的另一用户均成功。连接测试和 Agent 流被拦截，实际外发模型请求合计 3 次 HTTP 200。首次复验另有 3 次真实 HTTP 200，随后发现测试在 Uvicorn 读取配置后才设可信代理，修正配置顺序后全量通过。模拟 0 次。 [最终回执](../../tmp/live-j19-rate-primary-ny197t26/rate-limit-result.json) · [首次回执](../../tmp/live-j19-rate-primary-6ispd2rn/rate-limit-result.json) · [复验入口](../server/live_llm_redis_rate_limit_acceptance.py)

不受信转发头轮换不能重置访客额度。另一个隔离进程显式信任测试代理后，同转发地址重复请求得到 429，不同地址进入自己的桶。暂停实际 Redis 时受保护请求返回 503，恢复后返回 200。验收 API 已退出，Redis 测试容器已删除。 [可信代理验收](../../tmp/j19-explicit-proxy-lqth4ujx/trusted-proxy-result.json)
