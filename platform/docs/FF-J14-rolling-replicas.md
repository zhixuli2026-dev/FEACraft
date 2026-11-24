# FF-J14 本机双 API 与滚动替换

2026-11-25 的隔离验收使用生产 Nginx 配置、当前应用源码、正式 ParaView 可选 Dockerfile、PostgreSQL 16 和 Redis 7。两个 API 各有 hostname 和私有地址。Nginx 按入口配置分发新请求，Docker DNS 负责解析所列私有地址。PTY 和可视化的已有会话仍由其所属实例服务。 [Compose 副本配置](../infra/docker-compose.replicas.yml) · [Nginx 配置](../infra/nginx-web.conf) · [初轮运行回执](../../tmp/ff-j14-rolling-replicas-20261125/result.json)

## 私有副本地址

`docker-compose.replicas.yml` 与 `docker-compose.prod.yml` 一起使用。`VIBEFLOW_REPLICA_API_IMAGE` 指向已有的当前 API 镜像，两个 API 和 arq worker 共用这个版本。同一部署共用数据库、文件卷、索引卷、运行配置、站点凭据和 Redis。首次启用时，将双副本模板 `nginx-upstream-replicas` 复制到部署配置目录，以 `VIBEFLOW_REPLICA_UPSTREAM_DIR` 指定该目录。Web 镜像须包含当前入口配置。 [部署基础](deployment.md)

```sh
docker compose -f docker-compose.prod.yml -f docker-compose.replicas.yml --profile distributed config
docker compose -f docker-compose.prod.yml -f docker-compose.replicas.yml --profile distributed build web
docker compose -f docker-compose.prod.yml -f docker-compose.replicas.yml --profile distributed up -d --no-build
```

PTY 的新会话由收到请求的 API 创建。已有会话使用持久记录中的所属实例和租约。入口先核对当前用户、算例、会话所有者，给私有所属实例发一跳有效的短时票据，重连和关闭票据分别绑定操作和会话。所属实例再次检查会话权限，继续使用原进程、历史输出和停止证据。原会话关闭后也可按原协议读回记录。 [PTY 路由](../server/app/routers/terminal.py) · [实例间转发](../server/app/services/terminal_routing.py) · [票据](../server/app/services/connection_tickets.py)

## 有序切换

以替换 `api-peer` 为例。先用工具把入口成员设为 `api-primary`。工具逐一检查目标 API 的健康响应，原子写入部署目录中的 `api.conf`，再检查并平滑重载 Nginx。命令成功表示已发出重载信号。实际入口恢复健康、退役成员退出入口后，再按发布流程正常停止和替换该成员。退役实例上仍有业务会话时，按该会话的使用窗口和停机流程处理。待替换成员健康后再加入入口。 [入口工具](../tools/replica_upstreams.py)

```sh
python ../tools/replica_upstreams.py --web-container vibeflow-web --directory "$VIBEFLOW_REPLICA_UPSTREAM_DIR" api-primary
# 核对入口请求和待退役会话，完成 api-peer 的正常停止和替换。
python ../tools/replica_upstreams.py --web-container vibeflow-web --directory "$VIBEFLOW_REPLICA_UPSTREAM_DIR" api-primary api-peer
```

## 实际验收

| 路径 | 运行证据 |
| --- | --- |
| Nginx 和 API 副本 | 替换前的 10 次健康响应分别来自 A、B，各 5 次。启动 C 并正常停止 B 后，最后一组 10 次响应来自 A、C，各 5 次。复验切换期间有 3 个 HTTP 502，初次成功回执的切换段为 10/10。 |
| PTY | A 创建原生 Bash PTY，B 经私有路由挂接，读回原缓存，空闲 9 秒后执行另一条实际命令。B 上的独立 PTY 随 B 退出，SQL 保留核验过的停止回执。A 的原会话经 Nginx 再次接上，根进程身份一致。经 C 关闭后，A 读回相同停止证据。外账号关闭请求返回 404。 |
| 可视化 | A 上载真实 VTP 数据并启动 ParaView 5.11.2。B 通过正式视图票据读回 14,372 B 的原生 JPEG。替换后经 C 再次出帧和关闭，A 读回已核验停止记录。 |
| Redis 和独立 arq worker | 两个实际作业都读回独有输出与停止回执。第一个由初始 worker 完成。初始 worker 停止后，第二个请求持久保留在排队状态，由替换 worker 完成。两次绑定进程均为 UID 65534，SQL 进程记录中的 worker 与实际 Docker hostname 一致。 |

执行拓扑由验收脚本启动，发布用双副本 Compose 配置另行实际渲染。API 完整性和 Compose 回归均通过。隔离服务和卷退出后，原 11 个 Docker 服务仍在运行。模型调用 0 次。 [运行日志](../../tmp/ff-j14-rolling-replicas-20261125/run.log) · [Compose 回执](../../tmp/ff-j14-rolling-replicas-20261125/compose-regression-final/summary.json) · [核心 API](../../tmp/ff-j14-rolling-replicas-20261125/core-api.log)

发布基础镜像缺少可选 ParaView 的 `libcurl.so.4`。正式可选 Dockerfile 现明确安装 `libcurl4t64`，重新用固定归档构建并核对 `pvpython`、`pvbatch` 的实际版本和可视化流程。首次替换测试在等待后端时超时，Nginx 现将到内部 API 的连接期限设为 3 秒。两次 DNS 切换复测都恢复了流量，其中一轮在切换窗口记录到 3 次 HTTP 502。 [ParaView 重建](../../tmp/ff-j14-rolling-replicas-20261125/build-optional.log) · [首次替换观察](../../tmp/ff-j14-rolling-replicas-20261125/run-before-nginx-connect-bound.log) · [首次成功回执](../../tmp/ff-j14-rolling-replicas-20261125/run-first-success.log)

有序切换复测先拒绝未就绪的候选成员，原 A、B 继续服务。C 健康后切到 A、C，并确认仍运行的 B 已退出入口，再正常停止 B。挂载当前配置的一轮从重载前到停机后采样 170 次健康及已认证账户读取，全部成功。A 上已经连通的 Nginx WebSocket 在切换后仍能执行命令。B 的终端有已核验停止回执，原会话跨副本重连及 ParaView 出帧正常。采样覆盖本轮两类请求。 [有序切换回执](../../tmp/ff-j14-ingress-drain-20261125/result-mounted.json) · [逐请求记录](../../tmp/ff-j14-ingress-drain-20261125/ingress-samples-mounted.json)

从头构建 Web 镜像遇到 Docker Hub 清单请求返回 503。以本地已验收的发布候选为基础，按正式 Dockerfile 的两条 COPY 指令重新构建 Nginx 配置层。该镜像启动时读取内置的单副本配置，10 次健康请求均成功。挂载双副本配置后完整重跑有序切换，168 个采样请求全部通过，终端、真实 ParaView 帧和两个 worker 作业均复验。原 11 个服务继续运行，Compose 配置回归通过，模型调用 0 次。 [完整构建失败记录](../../tmp/ff-j14-ingress-drain-20261125/web-build.log) · [离线构建](../../tmp/ff-j14-ingress-drain-20261125/offline-web-build.log) · [离线镜像复验](../../tmp/ff-j14-ingress-drain-20261125/result.json) · [Compose 回执](../../tmp/ff-j14-ingress-drain-20261125/compose-final/summary.json)

以上为同一物理宿主的滚动替换。第二台授权机器上的跨宿主验收及最终阶段评审按原计划保留。 [Redis 停机重放](FF-J14-redis-event-relay.md) · [主线计划](next-stage-mainline-plan.md)
