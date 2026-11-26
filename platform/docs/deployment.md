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

### 认证 S3

在私有 `infra/.env` 中设置 `VIBEFLOW_STORAGE_BACKEND=s3` 和 `VIBEFLOW_ALLOW_PARTIAL_S3=true`，填入 `VIBEFLOW_S3_ENDPOINT`、`VIBEFLOW_S3_BUCKET`、`VIBEFLOW_S3_REGION`、`VIBEFLOW_S3_ACCESS_KEY` 和 `VIBEFLOW_S3_SECRET_KEY`。生产 Compose 会将同一配置传给 API、对端副本和 worker。外部存储使用其实际 HTTPS endpoint、签名 region 和预建 bucket。API 启动通过 `HeadBucket` 检查目标桶，并保留获授权时自动创建缺失桶的行为。 [HeadBucket 所需权限](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadBucket.html)

使用随栈 SeaweedFS 时，endpoint 可用默认值 `http://seaweedfs:8333`。在私有身份 JSON 中给应用分配目标 bucket 的 `Read`、`List`、`Write` 和 `Tagging` 权限，并使凭据与 `.env` 一致。由存储管理员预先创建 bucket。将 JSON 路径填入 `VIBEFLOW_SEAWEEDFS_IDENTITY_FILE`，文件由认证覆盖配方只读挂载。 [SeaweedFS 身份结构](https://github.com/seaweedfs/seaweedfs/blob/master/weed/command/s3.go)

```powershell
$s3Compose = @('--env-file','infra/.env','-f','infra/docker-compose.prod.yml','-f','infra/docker-compose.s3-auth.yml','--profile','s3','--profile','distributed')
docker compose @s3Compose config --quiet
docker compose @s3Compose up -d postgres redis seaweedfs
```

数据库就绪后沿用上述迁移及 API、worker、Web 启动步骤，继续传同一组 Compose 参数。双副本运行时一并加载 `infra/docker-compose.replicas.yml`。已有部署先按一致快照备份数据库和文件。S3 功能范围及本机隔离验收见 [S3 部署验收](storage-version-operations.md#认证-s3-部署验收)。

### 隔离 CLI 工作容器

Linux 单 API 部署可加载 `infra/docker-compose.docker-cli.yml`，从正式 Agent API 启动 Codex、Kimi 和 OpenCode 的隔离执行容器。覆盖文件配置可信引擎、本地私有工作目录和打包后的工作镜像。 [部署步骤](linux-node-cli-deployment.md#linux-compose-内的隔离-cli)

### 可视化会话内核额度

在具备私有 cgroup v1 控制组命名空间的 Linux API 上，可设置 `VIBEFLOW_VIZ_SESSION_LINUX_CPU_LIMIT_SECONDS` 和 `VIBEFLOW_VIZ_SESSION_LINUX_MEMORY_LIMIT_MB`。生产 Compose 将同名 `.env` 配置传给 API，默认均为 `0`，分别表示关闭对应额度。共享的内核启动门需要可信根进程具备 `SYS_ADMIN`，其已有控制器挂载保持只读。隔离验证采用 Docker 的 `--cgroupns private --cap-add SYS_ADMIN`。 [可视化内核额度及验收](viz-load-acceptance.md#linux-可视化会话内核额度)

### Linux 终端与 Agent Shell

同一 Linux 部署可用 `VIBEFLOW_TERMINAL_SESSION_LINUX_CPU_LIMIT_SECONDS` 和 `VIBEFLOW_TERMINAL_SESSION_LINUX_MEMORY_LIMIT_MB` 设置普通 PTY 会话的独立额度。配置默认为 `0`。启用后，API 在会话登记前核验控制器，成功终止时在 `stop.linux_compute_usage` 写入内核计量。 [终端内核额度及验收](owned-pty-sessions.md#linux-pty-内核额度)

Linux 普通 PTY 会话默认使用独立文件系统和网络命名空间。绑定算例的终端可写该算例或 S3 暂存副本。两种会话均从干净的交互 Bash 启动，历史和临时目录随会话结束清理。 [终端隔离及验收](owned-pty-sessions.md#普通-linux-终端隔离)

生产 Compose 在 `/data/linux-shell-state` 挂载独立的 `linuxshellstate` 卷，并用 `VIBEFLOW_LINUX_SHELL_STATE_ROOT` 启用账户 HOME。两个 API 副本挂载同一卷，并使用同一账户数据库及服务 UID。非 root 部署需让 API 的服务 UID 可写此卷。备份时将该状态卷与账户数据库配套保留。 [账户目录及验收](owned-pty-sessions.md#linux-账户-home-持久化)

挂载状态卷后，`VIBEFLOW_EXECUTION_WORKSPACE_LIMIT_MB` 同时用作每个账户的 HOME 容量上限，默认 4096 MiB。所有 API 副本应保持该配置相同。`VIBEFLOW_NODE_STORAGE_MIN_FREE_MB` 设定所在卷应保留的可用空间，`VIBEFLOW_EXECUTION_STORAGE_POLL_SECONDS` 控制运行期间的检查间隔。 [容量行为](owned-pty-sessions.md#账户-home-容量控制)

Linux Agent 命令通过 `VIBEFLOW_AGENT_SHELL_LINUX_CPU_LIMIT_SECONDS` 和 `VIBEFLOW_AGENT_SHELL_LINUX_MEMORY_LIMIT_MB` 设置每条命令及其后代的累计 CPU 与内存额度。两项默认均为 `0`，独立启用。启用时同样需要私有 cgroup v1 命名空间及受信执行门的控制器权限。命令达到额度时可在工具执行详情读取 `stop.linux_compute_usage`。 [Agent 命令额度](owned-agent-shell.md#linux-agent-命令内核额度)

Linux Agent 本机 POSIX 命令及其后代默认使用私有网络命名空间。执行门在放行前核验网络身份，工具执行详情记录 `identity.linux_network`。该控制覆盖命令的 IPv4 和 IPv6 出口，API 仍在原网络中使用已配置的模型服务。 [Agent 命令网络验收](owned-agent-shell.md#linux-agent-命令网络隔离)

Linux Agent 命令的可写工作区为当前算例或该命令的 S3 暂存目录。安装在系统路径下的工具通过只读挂载提供，命令使用自己的 `/tmp`。现有 Linux 执行门的挂载和 PID 命名空间必须可用，准备失败会终止此次工具执行。 [Agent 文件系统隔离](owned-agent-shell.md#linux-agent-命令文件系统隔离)

Linux CalculiX 和 SU2 求解使用独立的文件系统和网络命名空间。工作区为本次作业副本，系统运行时只读。`VIBEFLOW_CALCULIX` 和 `VIBEFLOW_SU2` 可指向镜像外的单文件可执行程序，依赖库从系统运行时目录加载。 [CalculiX 子进程隔离](calculix-publication.md#linux-求解子进程隔离) · [SU2 子进程隔离](su2-publication.md#linux-求解子进程隔离)

Linux Elmer 复用同一隔离入口，`VIBEFLOW_ELMER` 可指向源码构建的串行发行包入口。求解进程按只读方式使用同包的 `share/elmersolver/lib`。 [Elmer 子进程隔离](elmer-runtime.md#linux-求解子进程隔离)

Linux MOOSE 的 `VIBEFLOW_MOOSE` 可指向源码包的实际 ELF 入口。外部 `bin` 入口及其 `lib` 和 `share` 目录按只读方式挂载，包中引用的绝对库路径放在可信系统目录或 `/opt`。检查点暂存卷和 MPI 共享内存随进程范围回收。 [MOOSE 子进程隔离](open-solver-native-runtime.md#linux-moose-子进程隔离)

Linux Code_Aster 用 `VIBEFLOW_CODE_ASTER` 指定 `/opt` 内的独立安装入口 `bin/run_aster`。API 和 worker 使用包含 `rsync` 的镜像，原生子进程使用私有网络并从只读安装运行，计算库随算例成果发布。 [Code_Aster 子进程隔离](open-solver-native-runtime.md#linux-code_aster-子进程隔离)

### Linux 账户 HOME 硬配额

把一块启用了 `quota` 和 `project` 功能、以 `prjquota` 挂载的专用 ext4 文件系统接给 `linuxshellstate`。所有 API 副本使用同一状态卷和相同的 HOME 限额。宿主文件系统根目录和 `users/` 由 root 管理，账户目录由 API 的 UID/GID 使用。宿主上的配置工具需要 Linux 5.14 或更新版本的 `quotactl_fd`。 [配额接口](https://man7.org/linux/man-pages/man2/quotactl.2.html)

例如，服务 UID/GID 为 65534、上限为 4096 MiB、已注册账户 ID 为 17 和 23 时，在宿主运行下列命令。

```bash
sudo python3 platform/server/app/services/linux_home_quota.py \
  --root /srv/feacraft-shell-state --uid 65534 --gid 65534 --limit-mib 4096 17 23
```

配置 `LINUX_SHELL_REQUIRE_PROJECT_QUOTA=true` 和 `EXECUTION_WORKSPACE_LIMIT_MB=4096`，并使 `linuxshellstate` 挂载该专用文件系统。直接启动 API 时使用对应的 `VIBEFLOW_` 环境变量。新增账户在首次使用 HOME 前运行同一配置工具。已有 HOME 数据迁移时，先关闭相关会话，在新卷创建项目目录，再复制原 HOME 的内容，由目标目录继承项目配额。上线前核对文件和空间用量，备份保留原数据卷。 [硬配额实测](owned-pty-sessions.md#账户-home-内核硬配额)

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
