# 部署与运维

2026-11-18。可复用 Windows API/非特权 WSL，也可使用已验收的 Linux Docker PostgreSQL/Redis/S3/API/worker/Web 栈。当前 S3 工程镜像包含 OpenFOAM、Gmsh、TeX、CalculiX、SU2、ParaView、Node 与三种非 Claude CLI，并已有真实工程结果和 DeepSeek 回合证据；工具支持范围见[部署环境矩阵](deployment-environment-matrix.md)。主线已完成 50/53，N4-05、N7-01、N7-06 继续；物理多主机、完整外部后端和最终发布门槛未由本机验收替代。

## 本机启动

已有环境从仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File platform/scripts/dev-up.ps1
```

脚本启动 API（8000）和前端（5173），FS 模式复用现有数据库、不启动 Docker。脚本不启动模拟模型；模型在应用中选择已配置的真实服务。新机器依赖见 [dev-setup.md](dev-setup.md)。普通 WSL 执行沿用非特权路径。

脚本会重启它自己记录且路径/命令行匹配的后端；有活动任务时先通过应用结束任务，避免直接重启。

上述执行策略参数只作用于这个启动进程，不修改系统策略。工具安装目录、Setup 备份和重启恢复见 [工具链部署](toolchain-deployment.md)。

## Redis worker

Redis 投递、持久作业和恢复已接入，旧文档的“Redis 未使用、全部状态只在 API 内”已过时。默认本机模式不要求独立 worker。需要 Redis 时，在 API 与 worker 的启动环境中设置相同配置：

```powershell
$env:VIBEFLOW_DISTRIBUTED_JOBS = 'true'
$env:VIBEFLOW_REDIS_HOST = '127.0.0.1'
$env:VIBEFLOW_REDIS_PORT = '6380'
# VIBEFLOW_REDIS_PASSWORD 从私有配置注入。
Set-Location platform/server
.venv/Scripts/python.exe -m app.services.distributed_worker
```

双方使用同一数据库和存储配置。Redis 配置由代码直接读取进程环境，仅写入 `server/.env` 不足以开启该开关。上述 worker 入口包含已实现的等待、取消及收尾逻辑，不再使用旧的 `python -m arq ...` 入口。

正常关闭默认等待 30 秒，再中断未完成的 delivery，等待原生进程和数据库收尾。用户取消保持 cancelled，停机中断为 interrupted；缺少停止证据的任务保持需处理。见 [停机说明](worker-shutdown-drain.md)及[恢复边界](N4-07-original-criteria-audit.md)。

本机 FS 可用于同机 API/worker。异机定向执行要求共享 PostgreSQL/S3，配置 `VIBEFLOW_JOB_EXECUTION_HOSTS`，按处理器覆盖用 `VIBEFLOW_JOB_HANDLER_EXECUTION_HOSTS`，目标 worker 开启 `VIBEFLOW_WORKER_HOST_QUEUE=true`。语义见 [执行主机恢复](worker-owner-recovery.md)及[后处理和报告](confirmed-postprocess-workers.md)。同机 WSL 验收不能当作物理多主机验收。

## Docker 部署

在 `platform/` 目录执行。复制 `infra/.env.example` 为私有 `infra/.env`，设置 PG_PASSWORD、JWT_SECRET 和 WEB_ORIGIN。分布式模式另配置 REDIS_PASSWORD，并将 VIBEFLOW_DISTRIBUTED_JOBS 设为 true。命令显式指定 env 文件：

首次空数据库部署先创建 provider 加密密钥。已有数据库必须复制原密钥，两条命令按实际情况选择一条，目标存在时均拒绝覆盖：

```powershell
# 仅用于首次空数据库
server/.venv/Scripts/python.exe scripts/init-provider-vault.py infra/secrets/provider-vault.key
# 已有数据库：保留旧凭据可解密所需的原密钥
server/.venv/Scripts/python.exe scripts/init-provider-vault.py infra/secrets/provider-vault.key --copy-from server/app/.vault_key
```

`PROVIDER_VAULT_FILE` 相对 Compose 文件所在目录解析，默认 `./secrets/provider-vault.key`。API 和 worker 通过同一只读 Docker secret 挂载读取；私有文件不进入构建上下文或审查包。不要因为重建容器而重新生成密钥。

```powershell
docker compose --env-file infra/.env -f infra/docker-compose.prod.yml config --quiet
docker compose --env-file infra/.env -f infra/docker-compose.prod.yml build api web
docker compose --env-file infra/.env -f infra/docker-compose.prod.yml up -d postgres
```

新空库在 API 启动前迁移；已有库先备份并审查迁移：

```powershell
docker compose --env-file infra/.env -f infra/docker-compose.prod.yml run --rm --no-deps api python -m alembic upgrade head
docker compose --env-file infra/.env -f infra/docker-compose.prod.yml up -d api web
```

独立 worker 模式先启动 Redis，再启动业务服务：

```powershell
docker compose --env-file infra/.env -f infra/docker-compose.prod.yml --profile distributed up -d redis
docker compose --env-file infra/.env -f infra/docker-compose.prod.yml --profile distributed up -d api arq-worker web
```

Redis 使用 6380、认证健康检查及 AOF；worker 使用平台入口，容器关闭等待为两分钟。该等待不是所有任务收尾耗时的保证，维护前仍应结束活动工作。默认 FS 使用 casedata 卷，API 镜像包含迁移目录和配置，不包含 mock LLM。

多 API 实例可设置 `RATE_LIMIT_BACKEND=redis`，共用同一认证 Redis 的请求和模型配置额度。设置 `RATE_LIMIT_PROFILE_PER_MINUTE` 调整单用户对单模型配置的速率。`TRUSTED_PROXY_IPS` 只填能核对身份的实际转发代理地址或网段。 [限流配置与验收](FF-J19-shared-provider-rate-limits.md)

API/worker 的 Linux 执行门需要创建 user/mount/PID namespace；已实际验证 Docker 默认 seccomp 会拒绝 `unshare`。Compose 对这两个服务设置 `seccomp=unconfined`，允许现有执行门启动；数据库、Redis、Web 不设置该选项。此配置取消了这两个容器的默认 seccomp 过滤，不是完整多租户隔离承诺。

Nginx 请求正文上限为 101 MiB，覆盖现有 100 MiB 工作区备份及 multipart 开销；`/api/` 使用 HTTP/1.1 并关闭请求正文预缓冲。各接口较小的限额仍由 API 执行。原默认 1 MiB 已在实际 2 MiB 文件写入中触发 413，修正后逐字节读回和工作区恢复通过。

生产入口可使用 `infra/docker-compose.tls.yml` 提供 HTTPS。设置正式证书目录、HTTPS 起源和本地 HTTP 绑定后，同时加载生产 Compose 与 TLS 覆盖文件。浏览器按算例或会话使用短期连接票据，可视化 iframe 使用按路径限定的 Cookie。 [TLS 部署和实际验收](FF-J18-tls-connection-tickets.md)

运行时 Setup 保存在独立 runtimeconfig 卷，API/worker 共用 `/data/runtime/runtime-config.json`；备份时一并保留它。工具程序仍需存在于实际执行环境，持久化路径配置不等于自动安装工具。

API/worker 通过 `indexdata` 卷共用 `/data/index`，由 `VIBEFLOW_INDEX_ROOT` 指定。这是同主机本地共享目录方案；索引卷丢失可从当前算例文件重建，见[共享索引与恢复](shared-index-deployment.md)。

Linux 镜像已包含 Pandoc，正式 HTML/DOCX 报告生成、产物下载、重启保持和真实模型读取通过，见 [Docker 原生报告验收](docker-report-deployment.md)。OpenFOAM、TeX Live、原生 CLI 和可视化工具仍需在实际目标环境安装验收；不能仅凭容器健康认定完整 CAE 可用。`Dockerfile.sandbox` 不作为已接入的执行后端声明。

S3 仍有显式启动保护：选择 s3 时同时设置 `VIBEFLOW_ALLOW_PARTIAL_S3=true`。该开关只解除启动保护，不代表所有功能已验收；命名场景和剩余范围以台账为准。Compose 的 SeaweedFS 是候选本地环境，认证、版本和条件操作需按目标服务配置。无需 S3 时保持 FS。

## 日常检查和恢复

使用 `infra/docker-compose.monitoring.yml` 时，独立采集器依照节点配置保存时序、请求日志和告警投递状态。监控数据卷和正常业务卷分别备份。 [配置与实测](FF-J20-central-monitoring.md)

| 入口 | 用途 |
|---|---|
| `/api/health` | API 存活，不代表所有工具可用 |
| 认证后的 `/api/v1/runtime/capabilities` | 检查工具及运行环境 |
| 作业详情、尝试历史和日志 | 区分失败、取消、中断与需要处理 |
| [作业恢复](job-recovery-readability.md) | 使用现有恢复入口，不直接改任务终态 |
| [输入恢复](job-input-recovery.md) | 原输入不可用时明确重新准备 |
| [未完成上传](unfinished-upload-recovery.md) | 续传、取消及恢复记录 |
| [人工文件恢复](manual-file-recovery.md) | 核对版本并显式选择处置 |

保留 API/worker 日志、job ID、attempt ID 和证据路径。旧 case 日志路径不是所有 FS/S3 作业的统一入口。`progress.md` 是历史，不是实时状态监控。

模型验收只能使用授权的真实服务；不可用时记录未验证，禁止历史 mock 或未审查的模拟回归。执行入口须符合 `platform/tools/regression-llm-review.json` 与 AGENTS.md。

## 备份、升级和发布

单机 Docker 整栈的可执行联合冷备份、原密钥与全部命名卷的独立恢复步骤见[联合部署恢复手册](deployment-recovery-runbook.md)。该流程保留源服务，使用全新项目和端口验收，不能以普通工作区 ZIP 代替。

联合备份数据库、文件或对象存储、私有配置及原 provider 加密密钥。本机未配置时使用 `server/app/.vault_key`；部署通过 `VIBEFLOW_VAULT_KEY_PATH` 指定已存在的密钥文件，Compose 默认读取 `/run/secrets/provider_vault`。配置的文件缺失、不可读或格式损坏会在数据库初始化前拒绝启动，不会重新生成密钥。新环境恢复数据库时，必须一并恢复原密钥；格式正确但属于另一个数据库的密钥也不能解密旧凭据。

存储恢复、版本保留、S3 停机回收与删除清单压缩见[操作手册](storage-version-operations.md)。首次压缩会启用 v2 清单；所有读写实例须先升级，回退必须恢复完整一致备份，不能只回退代码或 HEAD。

可用初始化工具的 `--copy-from` 将离线备份的原密钥复制到新的挂载位置，然后让 API/worker 指向该位置并启动。初始化工具不覆盖已有目标，也不输出密钥内容。`docker compose down` 不删除宿主上的这份文件；不要把它放进镜像、审查包或公开日志。默认本机旧配置继续兼容，不自动迁移开发密钥。实际重启和真实模型验证见 [密钥恢复验收](provider-vault-persistence.md)。

工作区 ZIP 不是全库灾备。先在隔离环境恢复并确认数据库与文件一致；FTS 等可重建索引不能代替原数据。同主机实例共享索引目录，跨物理主机或独立副本仍须按目标拓扑验收，见[共享索引部署](shared-index-deployment.md)与[索引消费者](durable-index-consumers.md)。

升级先停止新工作、等待在途任务收尾，记录代码和 migration revision 并联合备份，审查后执行 `alembic upgrade head`。不盲目 stamp，不把 `downgrade base` 当回滚；回退需匹配的应用、数据库及存储备份。

发布包排除 `.env`、`.vault_key`、用户数据库、算例、缓存与凭据。外部工具和许可见 [第三方说明](third-party.md)；正式 SIWC 联调按用户决定暂缓。部署交接、审查包和恢复验收见 [N7-05 验收记录](N7-05-deployment-acceptance.md)。
