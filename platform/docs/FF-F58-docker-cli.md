# FF-F58／FF-J16 Docker CLI 隔离验收

2026-11-25，在 Windows Docker Desktop 的 Linux 容器上复用装有 Codex、Kimi、OpenCode 的固定镜像。本轮用实际 CLI 读取独立算例文件，核对工具事件、模型返回随机标识、原文件不变和容器退出。实际发生 7 次 `deepseek-flash` 流式 HTTP 200，模拟模型调用 0。此前同一方案的 Codex 首轮另有 2 次真实 HTTP 200。 [首轮回执](../../tmp/ff-f58-docker-execution-20261125/result-unix-socket.json) · [最终源码审查](../../tmp/ff-f58-docker-execution-20261125/review-streaming-cli.json)

| 实际 CLI | 真实流式响应 | 算例文件和终止 |
|---|---:|---|
| Codex | 2 | 工具真实读取，回复含匹配标识，原文件字节保持，容器退出 0 |
| Kimi | 2 | 工具真实读取，回复含匹配标识，原文件字节保持，容器退出 0 |
| OpenCode | 3 | 工具真实读取，回复含匹配标识，原文件字节保持，容器退出 0 |

[Codex](../../tmp/ff-f58-docker-execution-20261125/result-streaming-codex.json) · [Kimi](../../tmp/ff-f58-docker-execution-20261125/result-streaming-kimi.json) · [OpenCode](../../tmp/ff-f58-docker-execution-20261125/result-streaming-opencode.json)

CLI 容器使用 `--network none`，实测只有 `lo`，无默认 IPv4 路由。容器 UID 为 65534，根文件系统只读，有效 capabilities 为 0，Docker socket 不可见。实际 cgroup 核对 1 核、768 MiB、64 进程限额。模型请求从 CLI 的回环端口经本轮私有 Unix socket 交给单独的可信中继。上游密钥经 stdin 进入中继，仅由中继向 `https://api.deepseek.com` 发送。无对应会话授权时返回 401，非允许路径返回 404。Docker 20.10 的初期 `--internal` 对照仍观察到默认路由，验收改用无网模式和 Unix socket。 [Docker 无网驱动](https://docs.docker.com/engine/network/drivers/none/) · [初期失败回执](../../tmp/ff-f58-docker-execution-20261125/result.json) · [容器参数](../server/app/services/docker_cli_profile.py)

每轮独立启动一个实际运行的受限容器，通过 Docker 终止并核对退出码 137。三轮的临时容器与 Unix socket 卷均回收，原 11 个容器的完整 ID 一致。C 盘可用由 40,685,228,032 B 变为 40,679,673,856 B，Docker 构建缓存前后均为 5.819 GB，回收 0 B。 [磁盘起始](../../tmp/ff-f58-docker-execution-20261125/disk-before.json) · [磁盘结束](../../tmp/ff-f58-docker-execution-20261125/disk-after.json) · [Docker 结束](../../tmp/ff-f58-docker-execution-20261125/docker-after.txt)

本轮使用独立启动器和临时文件系统。平台工作台、每用户持久状态、正式算例暂存与写回、API/worker 中断恢复及各执行入口仍按 FF-F58／FF-J16 继续。 [验收启动器](../server/live_llm_docker_cli_sandbox_acceptance.py)

## 持久状态与重新附着

2026-11-25，Docker 状态卷按部署、账户和平台线程生成名称。挂载前核对原所有者绑定、卷代次及本地驱动，恢复要求原卷已存在。容器以 UID 65534 读写私有状态卷，模式为 `0700`。算例临时目录跟随容器独立回收。 [状态卷实现](../server/app/services/docker_cli_state.py)

首次运行的 Codex 在原容器读取算例随机文件并实际回复。另一账户的独立状态卷用原生线程 ID 请求恢复时被拒绝，发出 0 次模型请求。更换原卷代次也被拒绝。随后终止一个挂载原状态卷的独立运行容器，Docker 读回退出码 137，再新建容器加载原线程。恢复容器只有私有状态卷，原临时算例文件已不存在。实际 DeepSeek 在续答中复现了首轮随机内容，按 SHA-256 核对一致，续答工具调用 0 次。全过程 3 次真实 `deepseek-flash` 流式 HTTP 200，模拟 0 次。 [持久恢复回执](../../tmp/ff-f58-docker-state-20261125/result.json) · [源码审查](../../tmp/ff-f58-docker-state-20261125/review.json)

同一版源码重新验收三种临时状态 CLI，三个容器均实际读取自己的算例文件，合计 7 次真实 `deepseek-flash` 流式 HTTP 200。状态恢复与兼容复验累计 10 次，模型模拟 0。 [Codex](../../tmp/ff-f58-docker-state-20261125/compat-codex.json) · [Kimi](../../tmp/ff-f58-docker-state-20261125/compat-kimi.json) · [OpenCode](../../tmp/ff-f58-docker-state-20261125/compat-opencode.json)

## Linux 内核限额实测

同一套容器参数下，两个实际计算进程触发 15 个 CPU 节流周期。PID 达到 64 时内核拒绝继续创建，62 个测试子进程全部回收。768 MiB 容器尝试分配 800 MiB 时，Docker 报告 `OOMKilled=true` 和退出码 137。该轮模型调用 0 次。临时容器、状态卷和模型中继卷已回收，原 11 个容器的 ID 一致。C 盘可用由 40,679,940,096 B 变为 40,677,191,680 B，Docker 构建缓存均为 5.819 GB，回收 0 B。 [CPU、进程和内存回执](../../tmp/ff-f58-docker-state-20261125/resources.json) · [磁盘起始](../../tmp/ff-f58-docker-state-20261125/disk-before.json) · [磁盘结束](../../tmp/ff-f58-docker-state-20261125/disk-after.json)

这次验证了同一个 Docker 引擎内的隔离卷和 Codex 历史恢复。剩余交付把卷绑定接到正式 Agent 线程授权，复用算例暂存和写回，记录可信宿主持有的容器归属与关停回执，并验收其他 CLI 的历史恢复和 worker 中断。 [剩余任务](full-function-delivery-plan.md#21-最新范围规则)

## 正式 Agent ACP 入口

配置 Docker 后，Agent 原有 Kimi 和 OpenCode 入口使用同一个 ACP 会话协议、账户授权与审批。部署方同时设置 `VIBEFLOW_ACP_DOCKER_IMAGE` 和 `VIBEFLOW_ACP_DOCKER_DEPLOYMENT`。前者指向装有两个 ACP CLI 及 Python 依赖的本地镜像，启动时按镜像 ID 核对。后者为固定部署 UUID，重启时沿用原值。Docker 后端当前使用账户自己保存的官方 DeepSeek 配置。常规部署省略这两个变量时沿用宿主 ACP。 [执行入口](../server/app/services/acp_execution.py) · [Docker 传输](../server/app/services/docker_acp.py) · [容器入口](../server/app/services/docker_acp_entry.py)

平台在创建容器前提交启动归属。执行容器持有私有线程状态卷，按原有模式挂载工作目录，模型凭据经独立中继使用。正常结束时核对 Docker 容器状态、删除原范围，再提交持久化停止回执。恢复走原会话恢复流程，先核验原 API 的实际进程身份已退出，再按原 Docker 引擎和运行范围处理。 [会话恢复入口](../server/app/services/agent_recovery.py)

Windows Docker Desktop 加隔离 SQLite 实测，Kimi 和 OpenCode 通过正式 API 的首次提交、幂等重放、外账户读写拒绝和重启后续聊。两种 CLI 都在新容器加载各自原线程。另在 Kimi 回合已经运行时强制终止 API，重启后的原线程回合记为取消并附两容器停止回执，随后新的显式提交继续使用原线程。主链仍需扩展到 Codex、工程算例暂存／写回、所有 Docker 启动中断位置和 Linux 部署回归。 [正式 API 与进程强退回执](../../tmp/docker-acp-api-15cavaxe/docker-acp-api-result.json)

最终轮另验实际 Kimi 通过 Docker 绝对路径 `/workspace/docker-draft.txt` 申请写入。其他用户提交批准返回 404，批准前文件不存在，本人批准后文件实际落在私有挂载目录，并发布可核对的草稿事件。同一次批准返回原结果。关闭 Docker 模式的既有 Windows Kimi 流程另验了拒绝、批准及取消。最终两组收到 15 次真实 `deepseek-flash` HTTP 200，其中 Docker 10 次、原宿主 5 次，模型模拟 0。Docker 的 10 次 HTTP 200 有 7 次读到上游结束，3 次下游提前结束，回执分别记录；正式可见答复和强退恢复由会话证据分别核对。 [Docker 最终验收](../../tmp/docker-acp-api-lmzynolo/docker-acp-api-result.json) · [原宿主回归](../../tmp/live-acp-approval-kimi-zf7183vr/acp-approvals.json) · [执行源码审查](../../tmp/ff-f58-docker-acp-20261125/review-drafts.json)

原有 11 个运行容器完整 ID 一致，新建的 Docker 执行容器、测试状态卷和中继卷已移除。复用本地镜像，Docker 构建缓存仍为 5.819 GB，回收 0 B。C 盘可用由 40,677,580,800 B 变为 40,672,436,224 B。 [Windows 起始](../../tmp/ff-f58-docker-acp-20261125/disk-before.json) · [Windows 结束](../../tmp/ff-f58-docker-acp-20261125/disk-after.json) · [Docker 结束](../../tmp/ff-f58-docker-acp-20261125/docker-after.txt)

## 算例文本与草稿发布

Docker ACP 工作台可附加当前编辑器中已保存的文本。一次最多选择 12 个相对路径，总大小不超过 256 KiB。提交时核验算例权限，并以每个文件的 SHA-256 记录来源。容器内以只读方式挂载至 `/workspace/case`，草稿仍写入独立工作目录。停止容器后清理本轮副本，强退恢复沿原所有者回执执行同一清理。 [算例快照](../server/app/services/acp_case_files.py) · [Docker 传输](../server/app/services/docker_acp.py)

OpenCode 对工作目录内的文件按相对路径匹配读取规则。容器允许读取 `case/*`，并维持其他路径的默认拒绝。Kimi 在有附加文件时开启原生 Read 工具，写入仍走一次性审批。 [OpenCode 权限规则](https://opencode.ai/docs/permissions) · [两种工具的配置](../server/app/services/acp_configuration.py)

批准原生写入后，已完成回合的草稿可以单独发布。发布只接受本回合选择的普通算例文件，并按快照中的摘要核对当前内容。期间如有其他编辑，服务器返回冲突并保留当前内容。重复提交同一草稿和目标返回原发布回执。正式写入复用算例文件事务和组织审计。 [接口](../server/app/routers/acp.py) · [工作台](../web/src/panels/AcpChatPanel.tsx)

Windows 本地 Docker 使用所选 Docker context 的命名管道读取守护进程 ID，避免通过 CLI 枚举插件。核对值与原 `docker info` 一致，其他 Docker context 仍可使用 CLI 查询。 [引擎 ID](../server/app/services/docker_engine_identity.py)

2026-11-25，本地文件存储的正式 API 已验 Kimi 和 OpenCode 读取实际算例文件、重新启动后的同线程续聊、拒绝跨账户访问以及原 API 强退后的容器与快照回收。实际 Kimi 获批写草稿后，算例原件保持原内容。单独发布后正式文件才改变，晚到发布拒绝覆盖已有编辑，重复发布只留下一个事件。Edge 已跑通附加、审批和发布。Docker 的模型回执记录 14 次真实 DeepSeek HTTP 200，其中 12 次读完响应，2 次提前结束。原宿主 Kimi 回归另有 5 次真实 HTTP 200，均无模型模拟。 [Docker 本地文件存储](../../tmp/docker-acp-api-vkvxj46h/docker-acp-api-result.json) · [Edge](../../tmp/docker-acp-api-vkvxj46h/workbench-browser.json) · [原宿主](../../tmp/live-acp-approval-kimi-dycsj8ky/acp-approvals.json)

RustFS 的实际 S3 后端也通过 Kimi 原生工具读回、批准草稿、发布前后 S3 文件内容、并发编辑冲突、幂等回执和强退恢复。最终版 Edge 工作台验证发布后编辑器同步内容，并核对 880 像素视口中的发布回执。S3 轮记录 10 次真实 DeepSeek HTTP 200，其中 9 次读完响应，本轮三个通过的专项共 29 次真实 HTTP 200，模型模拟 0。Codex 正式容器入口和其他执行场景继续验收。 [S3 回执](../../tmp/docker-acp-api-9ir3ih03/docker-acp-api-result.json) · [窄工作台](../../tmp/docker-acp-api-9ir3ih03/workbench-published-compact.png) · [最终源码审查](../../tmp/ff-f58-docker-acp-case-files-20261125/review.json)

隔离验证结束后清理测试容器及测试卷，原 11 个运行容器完整 ID 一致。C 盘可用从 40,667,852,800 B 变为 40,663,429,120 B。复用本地镜像，Docker 构建缓存仍为 5.819 GB，回收 0 B。 [C、D 盘开始](../../tmp/ff-f58-docker-acp-case-files-20261125/disk-before.json) · [C、D 盘结束](../../tmp/ff-f58-docker-acp-case-files-20261125/disk-after.json) · [Docker 结束](../../tmp/ff-f58-docker-acp-case-files-20261125/docker-after.txt)

## 正式 Codex 容器入口

配置同一 Docker 镜像和固定部署 UUID 后，Agent 工作台提供 **Codex · Docker**。正式 Codex 会话请求、回合接口和 SSE 事件复用原有 app-server 协议，平台登记 Docker 引擎、镜像和两个容器的完整 ID。每个账户和模型配置版本使用独立的原生状态卷。容器网络仍为 none，模型请求通过专用 Unix socket 中继到官方 DeepSeek Responses API。 [容器传输](../server/app/services/codex_docker.py) · [会话](../server/app/services/codex_sessions.py) · [强退恢复](../server/app/services/codex_docker_recovery.py)

文本问答和计划通过受平台授权的只读工具读取实际算例。模型容器挂载独立的只读工作目录。恢复原生线程要求与上一回合使用同一个账户、模型配置、Docker 引擎和镜像。Windows 与 WSL 使用各自的原生进程凭据。 [算例工具](../server/app/services/codex_case_tools.py) · [会话绑定](../server/app/services/codex_records.py)

真实 API 首轮通过只读文件的随机标识回读、可见流式文本、同请求重放、外账户拒绝和活进程恢复拒绝。重启 API 后在新容器恢复原生线程。另在原回合运行时强制结束 API，按登记只回收两个所属容器，释放会话后可再次提交。首轮 5 次真实 DeepSeek HTTP 200，其中 4 次读完响应，另 1 次在强退后下游中断，模型模拟 0。 [正式 API 回执](../../tmp/docker-codex-api-nk28257f/codex-docker-result.json) · [首轮源码审查](../../tmp/ff-f58-codex-docker-20261125/review-first.json)

SQLite 实际执行迁移和回滚，保留既有 Windows 会话及退出记录。测试库存在 Docker 会话时，拒绝回滚并保留四条历史记录。原宿主 Windows 的 Codex 只读算例回归也使用了真实 DeepSeek 并通过。 [SQLite 历史记录迁移](../../tmp/ff-f58-codex-docker-20261125/migration-history.json) · [Windows 只读回归](../../tmp/live-codex-permissions-turhs02p/live-result.json)

最终 Edge 专项从 Agent 的常规下拉菜单选择 **Codex · Docker**，实际启动只读计划，核对算例读取工具和可见回复。刷新页面后，重开 Agent 并选择同一会话，检查了 Docker 后端偏好和历史回复。整组收到 7 次真实 DeepSeek Responses HTTP 200，其中 6 次完成读取，1 次随 API 强退提前结束。模型模拟 0 次。 [最终回执](../../tmp/docker-codex-api-pdpe15vs/codex-docker-result.json) · [Edge 重开后的截图](../../tmp/docker-codex-api-pdpe15vs/codex-docker-reloaded.png) · [最终源码审查](../../tmp/ff-f58-codex-docker-20261125/review.json)

另以独立 PostgreSQL 16 容器运行此版本的迁移和回滚，通过数据库约束核对 `docker` 运行时的引入和移除。临时数据库容器已回收。 [PostgreSQL 迁移](../../tmp/ff-f58-codex-docker-20261125/migration-postgresql.json)

共享容器传输改动之后，原 Kimi 和 OpenCode 的读取、恢复以及 Kimi 草稿批准发布再次通过。该 ACP 回归收到 11 次真实 DeepSeek HTTP 200，10 次读完响应，模型模拟 0。原 11 个 Docker 容器完整 ID 一致。 [ACP 回归回执](../../tmp/docker-acp-api-5ywwign4/docker-acp-api-result.json)

## Codex 算例写入与发布

工作副本可写和容器内自动模式在独立目录准备算例。入口固定初始文件内容与来源存储，容器仅挂载工作副本。原生回合使用 app-server 提供的外部沙箱策略，由已登记的 Docker 容器提供实际隔离。只读问答和计划继续使用平台只读工具。 [算例暂存](../server/app/services/codex_docker_stage.py) · [回合策略](../server/app/services/codex_permissions.py)

正常结束且核验容器退出后，发布复用现有三方合并和算例文件事务。存在并行修改时，整批留在副本。回合记录可以查看已保留文件和文本内容，正式文件仍按原路径和版本读取。容器挂载和发布之间保留原有文件模式，新文件按宿主能证明的模式保存。 [发布事务](../server/app/services/sandbox_export_publication.py) · [回合界面](../web/src/panels/NativeChatPanel.tsx)

本地文件存储的实际首轮用 Codex 原生 shell 执行两处文件复制。常规可写会话提交了整个批次。另一容器在并行编辑发生后完成自身写入，服务器拒绝整批发布。原文件保留外部编辑，另一个文件也保持原版本，用户可读回暂存内容。 本地最终复验包含写入期间强退与副本保留，通过 10 次真实 HTTP 200，9 次完整转发。 [最终 FS 回执](../../tmp/docker-codex-api-giha0fd9/codex-docker-result.json) · [首轮 FS 回执](../../tmp/docker-codex-api-7pvr044w/codex-docker-result.json)

实际 RustFS 复验了正式发布、并行编辑导致整批冲突、原件保留以及授权用户回读暂存内容。另在模型调用的 shell 已写入副本后强制结束 API。重启核对原容器退出后可以读取副本，正式文件保留原内容。Edge 在工作台启动只读计划，刷新读回记录，点「批准 Plan」后恢复同一条原生线程并完成正式写入。该轮 15 次真实 DeepSeek Responses HTTP 200，14 次完整转发，1 次随进程强退提前结束，模拟 0 次。 [RustFS 与 Edge 回执](../../tmp/docker-codex-api-egk88ce3/codex-docker-result.json) · [工作台审批后写入](../../tmp/docker-codex-api-egk88ce3/codex-docker-plan-written.png) · [源码审查](../../tmp/ff-f58-codex-writeback-20261125/review.json)

本轮使用已安装镜像，构建缓存 5.819 GB，回收 0 B。原 11 个运行容器完整 ID 一致。C 盘可用从 40,720,424,960 B 变为 40,711,184,384 B，D 盘可用从 170,207,887,360 B 变为 170,039,541,760 B。 [起始磁盘](../../tmp/ff-f58-codex-writeback-20261125/disk-before.json) · [RustFS 验收后磁盘](../../tmp/ff-f58-codex-writeback-20261125/disk-after.json) · [Docker 占用](../../tmp/ff-f58-codex-writeback-20261125/docker-after.txt)

## Codex 图像输入

工作台沿用原生对话的图片附件入口和历史。私有副本按账户、API 配置版本与算例保存，容器将该算例的附件以只读方式挂载到 `/inputs`，原线程恢复时沿用地址。算例中的原始图片仍走现有下载授权。 [附件准备](../server/app/services/codex_images.py) · [容器挂载](../server/app/services/codex_docker.py)

本地文件存储验证三张随机纯色 PNG，真实 DeepSeek 按顺序答对，新的 Docker 容器恢复同一条线程并复述三色。验证文件摘要、授权下载原始字节、其他账户拒绝访问及实际只读挂载。该轮包含 7 次真实 DeepSeek HTTP 200，6 次完整转发，1 次随 API 强退结束，模拟 0 次。 [本地验收](../../tmp/docker-codex-api-9k5hl9gs/codex-docker-result.json) · [源码审查](../../tmp/ff-f58-codex-images-20261125/review.json)

2026-11-26，在实际 RustFS 上复验三张随机数字图像。测试验证原始字节、附件摘要、只读 Docker 挂载、跨账户拒绝读取，以及在新容器中沿同一原生线程复述。实际 Edge 验证了工作台上传、真实模型回答和页面刷新后的三张原始图片。首轮在会话加载时选文件，附件卡片计数为 0。验收脚本等待附件按钮可用后，复验通过。首轮停止回执记录 17 次真实 DeepSeek HTTP 200，最终复验记录 10 次，均使用 `deepseek-flash`，模型模拟 0 次。回执分别位于 `tmp/docker-codex-api-cv9khav3` 和 `tmp/docker-codex-api-iwvc6eo0`。

## 2026-11-26 容器资源计量

CLI 关闭输入前读取所属工作容器的 cgroup CPU 累计用量、内存峰值、CPU 节流次数和 PID 限额触发次数。API 归属恢复时，仍在运行的工作容器也会在停止前采样。仅随已经核验的停止回执保存，并绑定原工作容器 ID。控制器无法读取或容器已退出时省略读数。旧停止回执仍可读取。Codex 会话接口返回 `stop.resource_usage`，ACP 回合接口返回 `resource_usage`。读数对应采样时刻。 [采样实现](../server/app/services/docker_cli_usage.py)

隔离容器运行了实际 CPU 循环并分配 24 MiB，关闭流程记录 CPU 385,953 微秒、内存峰值 35,942,400 字节，原容器停止及回收通过。旧回执通过兼容检查，资源读数绑定其他容器 ID 时被拒。该轮使用实际 Docker cgroup v1，模型调用 0 次。原始回执位于 `tmp/ff-f58-codex-rustfs-edge-20261126/cgroup-counters.json`。

上述 Codex 最终复验的持久化会话中，有 7 条停止回执同时保存了实际 cgroup 读数。

Kimi 与 OpenCode 在同一 Docker 镜像及实际 RustFS 上分别完成算例读取、原生线程恢复、退出核验和后续回合。ACP 接口返回 6 份采样，逐条与持久化停止回执中的工作容器 ID 和用量一致。共记录 11 次真实 `deepseek-flash` HTTP 200，模型模拟 0 次。原 11 个 Docker 服务的容器 ID 一致，清理可再生成的 Docker 构建缓存回收 0 B，C 盘阶段结束可用 39,901,700,096 字节。原始模型和用量回执位于 `tmp/docker-acp-api-k3jcvll8/docker-acp-api-result.json`，磁盘与 Docker 记录位于 `tmp/ff-f58-codex-rustfs-edge-20261126/`。
