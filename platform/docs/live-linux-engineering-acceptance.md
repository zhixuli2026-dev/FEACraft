# Linux 上真实模型驱动的工程流程

2026-11-18：使用用户授权的 DeepSeek profile 26（`api.deepseek.com`、`deepseek-flash`），在已部署的 Linux API、PostgreSQL 和 S3 上执行工程算例。模型实际选择工具，调用 SU2、ParaView 和 Pandoc，读取运行结果后生成说明。没有模拟模型回复、模拟工具选择或模拟 CLI 推理。

## 完成范围

隔离账户与算例通过公开 API 创建。模型依次读取工程说明、运行已有的工程脚本、读取结果 JSON，然后引用数值和运行时随机 proof。proof 由真实执行生成，未预置在模型提示中。

工程脚本执行 SU2 8.5.0 的 NACA0012 算例（2 个线程），读取实际 CSV 和 VTU；ParaView 5.11.2 读取求解结果、计算压力比、提取声速线并离屏渲染；Pandoc 将数值与图片生成 DOCX。验收逐个下载并核对 7 份成果的长度及 SHA-256，核对 DOCX 内嵌 PNG 与实际渲染图片相同。实际数值包括 5233 个点、10216 个单元、密度对数残差约 -8.002322279、最大 Mach 约 1.3839086294、声速线 148 个点，压力比公式误差为 0。

工程执行使用 Agent 的 `run_shell_command` 和 S3 staging，同步回算例后核验原生执行请求成功及子进程范围停止。它证明模型驱动的工程流程可用；不代表规范工作流状态迁移、审批流、不可变作业发布或实验精度验证已完成。

## 实际发现与修复

- SU2 的 CSV 标题带空格，初次真实运行在读取 `rms[Rho]` 时失败。读取时去除标题两端空白，保留原始失败记录。
- Linux 工具提示仍混入 Windows runtime 选择，真实模型曾尝试 `findstr` 和 PowerShell。工具与系统提示现在使用实际宿主系统；Linux 默认 `/bin/sh`，不向模型提供 Windows runtime 参数。Windows 的既有参数枚举保持，另有纯 schema 检查；本阶段没有宣称 Windows 模型验收。
- 新建算例上传后，S3 写入尚在结算会返回 `storage_quota_busy`。模型流被接受前等待已知的短暂存储结算；执行所有者启动使用已有的有界 admission。没有重试模型回复或重复执行不确定的原生命令。
- 模型连接曾在约 5 秒后返回空白错误。网关现在保留异常类型，实际捕获 `ConnectError`。同一容器的无密钥网络对比显示：代理路径 6 次中 4 次失败，包含 `proxy.start_tls.failed`；直连 6 次均到达官方服务并获得预期的未认证 401。该探测属于实际网络证据，不是模型验收。
- 已部署 S3 项目的 API/worker 在保留原代理及内部服务排除项的基础上，将 `api.deepseek.com` 加入 `NO_PROXY`。实际 HTTPX 路由确认使用直接连接池。随后真实模型完成中断会话的结果读取与说明，未重跑已经完成的 Shell；再使用新账户、新算例验收完整流程。

网关的异常类型记录不等于网络修复。此次修复针对这台机器的代理路径；其他部署应根据实际网络决定 `NO_PROXY`，不能把本机代理地址复制到生产配置，也不要全局关闭 TLS 校验。

## 部署与恢复

S3 API/worker 使用 `vibeflow/api:live-linux-engineering-v7-20261118`。镜像应用源码与审查记录逐项匹配；代理路由调整只修改这套部署的环境变量，保留数据库、对象存储、密钥和卷。重建后健康检查以及三份历史 TeX PDF 的逐字节校验通过。

对需要代理的部署，可在 API 和 worker 的环境中配置 `HTTP_PROXY`、`HTTPS_PROXY`，并保留内部服务的 `NO_PROXY`。只有经过实际直连验证的模型域名才加入排除项。配置后重建相关容器，检查实际连接路由并调用真实模型；仅检查 HTTP 健康接口不能证明模型网络可用。

已运行成功但模型连接失败的算例，通过 `--finish-from` 验证已有成功 Shell 和成果后，仅让真实模型读取结果并完成说明。`--resume-from` 仅用于没有成功模型回合、没有工具执行的失败算例。两种恢复均保留原始事件和失败记录。

## 证据和限制

- [阶段验收汇总](../../tmp/mainline-live-linux-engineering-evidence/validation.json)：真实模型请求去重计数、成功及失败记录、部署与发布包。
- [代理与直连对比](../../tmp/live-linux-engineering-stage-20261118/network-route-comparison.json)、[修复后的实际路由](../../tmp/live-linux-engineering-stage-20261118/route-after.json)。
- [部署与历史成果保留](../../tmp/live-linux-engineering-stage-20261118/deployment-result-direct-deepseek.json)。
- [中断后完成结果说明](../../tmp/live-linux-engineering-539a1303b9d5/result.json)、[直连后的全新算例](../../tmp/live-linux-engineering-f5aba3a05728/result.json)。
- [明确的连接失败](../../tmp/live-linux-engineering-4cb7d6995dc7/events.json)、[最初的 CSV 与 Linux 命令失败](../../tmp/live-linux-engineering-acf2a39c93bb/result.json)。

模型回合数来自实际持久化的 `request_state` 并按 request ID 去重；这是应用请求 ID，不冒充上游请求 ID 或 token 账单。单独的官方连接诊断保存了实际上游 ID 和 usage。短期通过不证明长期网络稳定或生产并发容量。

主线仍 49/53、剩 4 项；资源预算仍 11/17、剩 6 项；全功能仍 2/295、剩 293 项。完整权限变体、工作流状态推进、多主机与故障矩阵，以及 Node/npm 和非 Claude CLI 的部署缺口继续按原计划推进。Claude 已取消；正式 Sign in with ChatGPT 联调按用户决定暂缓。
