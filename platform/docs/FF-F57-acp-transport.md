# FF-F57 OpenCode 与 Kimi 原生协议进展

2026-11-24，实际安装的 Windows OpenCode 2.0.32 与 Kimi Code 2.6.2 使用同一个 ACP v1 客户端。键入、回复、审批和取消走逐行 JSON-RPC。原生进程由现有 OwnedStdio 注册与关闭。协议握手和两次进程退出核验模型调用 0 次。 [ACP 初始化](https://agentclientprotocol.com/protocol/v1/initialization) · [OpenCode ACP](https://opencode.ai/docs/cli/acp) · [Kimi ACP](https://moonshotai.github.io/kimi-code/en/reference/kimi-acp) · [实际握手](../../tmp/f57-acp-protocol-8ymfw4k4/protocol-preflight.json)

两款 CLI 分别用真实 DeepSeek 流式输出一段随机会话标识。停止原进程后，用新进程加载同一会话并取回标识。在收到实际上游 HTTP 200 后暂缓交付响应，调用原生 `session/cancel`，收到 `cancelled` 后同会话再次准备回复并恢复原标识。两款工具合计 10 次实际 Chat Completions HTTP 200，进程范围全部为空。 [OpenCode 流式与恢复](../../tmp/live-acp-opencode-mmawwsoj/live-result.json) · [Kimi 流式与恢复](../../tmp/live-acp-kimi-6qomfjv8/live-result.json)

真实模型选择文件写入时，两款 CLI 均发出 ACP 权限请求。先返回 `reject_once`，对应文件保持不存在。随后对另一隔离文件返回 `allow_once`，内容与随机值一致。两款工具各 4 次真实 DeepSeek HTTP 200，关闭时再次核验进程范围。四组共 18 次真实推理请求，模拟 0。 [ACP 权限协议](https://agentclientprotocol.com/protocol/v1/tool-calls) · [OpenCode 审批](../../tmp/live-acp-permissions-opencode-z9myuxql/live-permissions.json) · [Kimi 审批](../../tmp/live-acp-permissions-kimi-u5_nxgnl/live-permissions.json)

复跑同一客户端重构、私有启动配置和实际文件审批后，首阶段累计 36 次真实 HTTP 200。随后两款工具均接入 Agent 工作台。Windows PostgreSQL 两 API 和 Linux SQLite 两 API 使用新进程恢复原生会话，重复请求复用同一回合，另一 API 发起取消，等待进程范围核验为空后续聊。Linux Kimi 的原生会话锁在强制关闭后曾保留。现发送输入 EOF 让 CLI 保存并退出，超时继续走原有进程停止与核验流程。 [Windows OpenCode](../../tmp/live-acp-api-opencode-13vkdeqn/acp-platform.json) · [Windows Kimi](../../tmp/live-acp-api-kimi-xk2d31au/acp-platform.json) · [Linux OpenCode](../../tmp/live-acp-linux-opencode-eg_i_3ko/live-acp-api-opencode-6iq7kuye/acp-platform.json) · [Linux Kimi](../../tmp/live-acp-linux-kimi-0qjtl_ga/live-acp-api-kimi-5vuqo1xa/acp-platform.json)

## 工作台草稿审批

“草稿审批”允许 OpenCode 和 Kimi 申请写入私有线程里的单个 TXT/Markdown 草稿。审批归属当前回合及使用者，另一个 API 可以记录一次允许或拒绝。拒绝、取消和请求标识失效时保留原文件状态。运行范围核验完成后显示实际保存的草稿内容。OpenCode 提供正文预览。Kimi 提供文件名，存在正文差异时也提供预览。模型配置和算例契约的恢复见 FF-J25。 [配置与备份](FF-J25-cli-profile-restore.md) · [工作台审批实现](../server/app/services/acp_approval.py)

- 双 API 各自运行 Linux 和 Windows 版本。两款工具在两个环境分别通过实际文件拒绝、允许、等待审批时取消，以及过期和外账户拒绝。相同审批重复提交返回原决定。批准的内容从实际私有工作目录核对。4 轮各 5 次实际 DeepSeek HTTP 200，共 20 次。Linux OpenCode 有 2 次、Kimi 有 1 次连接失败后重试。 [Windows OpenCode](../../tmp/live-acp-approval-opencode-dqpih0bt/acp-approvals.json) · [Windows Kimi](../../tmp/live-acp-approval-kimi-0m9d9yw9/acp-approvals.json) · [Linux OpenCode](../../tmp/live-acp-linux-opencode-approvals-io2a3hb_/live-acp-approval-opencode-ceubdxor/acp-approvals.json) · [Linux Kimi](../../tmp/live-acp-linux-kimi-approvals-dyzxo9ka/live-acp-approval-kimi-xwqi8f2t/acp-approvals.json)
- Edge 真实覆盖两个工具、刷新后续聊、设置备份恢复、Codex 终端凭据配置、两种文件审批与成功后读回。该轮 11 次 Chat Completions 和 2 次 Responses HTTP 200，390px 未出现横向溢出。 [浏览器记录](../../tmp/live-acp-edge-s53fnbh8/browser.json) · [模型状态](../../tmp/live-acp-edge-s53fnbh8/acp-edge.json) · [Kimi 审批画面](../../tmp/live-acp-edge-s53fnbh8/approval-kimi-390.png)
- 独立真实文件系统策略检查允许受控文件名，拒绝越界路径、链接、设备名和契约文件等 9 种情形。该检查模型调用 0 次。Windows Codex 原生跨 API 停止复验用 1 次真实 Responses HTTP 200，清理范围为空。 [路径检查](../../tmp/ff-f57-draft-policy-5y2788z2/draft-policy.json) · [Codex 停止](../../tmp/live-f34-native-owner-rm4vhesh/native-peer-stop-result.json)

模型验收模拟 0。Linux 容器都使用本机已有镜像并自动移除。工作台问答与草稿、原有终端交互分别沿对应入口运行。部署新版使用迁移 `8da5cff5b71b`，真实 SQLite 和 PostgreSQL 均由上版完成升级。第二台物理宿主仍按 N7-01 安排，容器硬隔离仍由 FF-F58/FF-J16 验收。 [真实验收入口](../server/live_llm_acp_platform_acceptance.py) · [双库迁移](../../tmp/ff-f57-acp-migrations-4ggnonkq/migration-result.json)
