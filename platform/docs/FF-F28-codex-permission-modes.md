# FF-F28 API 与 Codex 权限模式

| 模式 | API Agent | Codex Windows 与 WSL |
| --- | --- | --- |
| 问答 | 文本回合，服务端拒绝结构化工具调用 | 只读沙箱，可调用当前算例的读取、列表和搜索工具 |
| 只读计划 | 允许只读工具，拒绝危险调用 | 使用原生 Plan 和只读沙箱，服务端拒绝权限升级 |
| 审批 | 危险工具待用户逐次批准 | 工作区内可写，请求超出沙箱的操作时按原生回执审批 |
| 全自动或完全访问 | 允许在工具各自约束内执行 | 原生完全访问，使用 `never` 审批策略 |

原生模式在线程启动、恢复和分叉沿用同一映射。Windows Codex 对本机 `Get-Content` 实际要求审批。工作台使用 App Server 工具接口，把读取、列表和搜索交还平台现有的算例读取池，按回合、账户和算例绑定限制范围。原生回合仍以实际沙箱策略运行。 [权限映射](../server/app/services/codex_permissions.py) · [算例读取工具](../server/app/services/codex_case_tools.py) · [Codex App Server](https://learn.chatgpt.com/docs/app-server#dynamic-tool-calls-experimental)

完成只读计划后，工作台在会话设置之外显示批准操作。用户批准进入工作区可写模式。满足线程环境和收尾凭据要求时，Codex 可以沿用计划的原线程，并以默认协作模式执行后续任务。实际 Windows `thread/resume` 有时回执仍为只读。平台在每次 `turn/start` 发送当前会话的沙箱和审批策略。S3 只读暂存完成或恢复时清理本地副本，权限升级请求仍由当前回合的固定权限校验。 [线程与暂存](../server/app/services/codex_sessions.py) · [回合策略](../server/app/services/codex_turns.py) · [退出恢复](../server/app/services/codex_staging_recovery.py) · [Agent 权限控件](../web/src/panels/ChatPanel.tsx)

WSL 私有目录有文件在枚举和读取属性之间被原子替换时，重新扫描同一目录。重读沿用原期限和累计条目额度，失败一轮的正文大小不计入最终用量。重定向、目录身份变化和第三次枚举失败仍会拒绝。 [私有目录测量](../server/app/services/cli_native_inventory.py)

## 验收证据

- 实际 API 问答验证普通文本回复，以及服务端拒绝模型返回的结构化写入请求。实际 API Plan 回合验证只读工具、结构化违规调用拒绝、其他进程的活动权限切换拒绝和浏览器批准后续写入。 [API 问答](../../tmp/live-plan-permission-t2tcvh7q/live-plan-permission-result.json) · [API Plan 与 Edge](../../tmp/live-plan-permission-jlqa3a5p/live-plan-permission-result.json)
- WSL 文件系统通过实际只读读取、用户批准后原线程续跑及文件写入，另有真实的 Ask 请求权限升级失败和完全访问写入。 [WSL Plan](../../tmp/live-codex-permissions-wutzw8qq/live-result.json) · [WSL 其他模式](../../tmp/live-codex-permissions-y68t3lry/live-result.json)
- Windows 的 App Server 确认只读沙箱，实际算例读取通过平台工具完成。Edge 批准 Plan 后，原线程收到 `workspaceWrite` 回合策略并完成实际写入。另一组真实模型通过超出沙箱的请求拒绝和完全访问写入。 [Windows 只读读取](../../tmp/live-codex-permissions-c_0lcszp/live-result.json) · [Windows Plan](../../tmp/live-codex-permissions-zixv1ivb/live-result.json) · [Windows 权限结果](../../tmp/live-codex-permissions-jeje8t03/live-result.json)
- 认证 SeaweedFS 和 PostgreSQL 上，实际只读回合正常结束和 API 实际退出后的恢复均保留公开算例的原数据。隔离验收向私有暂存注入真实文件差异，检查它没有上传或被重复恢复。Edge 批准后的新执行在同一线程写入，公开 API 从认证存储读回目标文件。 [正常关闭](../../tmp/live-codex-permissions-sm9s91wm/live-result.json) · [实际 API 退出](../../tmp/live-codex-permissions-3stww1uu/live-result.json) · [S3 计划和发布](../../tmp/live-codex-permissions-h1xbb8sj/live-result.json) · [批准界面](../../tmp/live-codex-permissions-h1xbb8sj/native-plan-approved.png)

API Agent 本阶段有 14 次真实 DeepSeek HTTP 200。原生 CLI 记录到 27 个不同的已接纳回合，其中 22 个有模型用量回执。汇总保留早期 WSL 入场超时结果和重跑后的 11 项原生通过场景。模拟 LLM 调用 0。22 个隔离 API 监听端口均已关闭。 [验收汇总](../../tmp/FF-F28-codex-permissions-20261121/validation.json) · [原生实际验收入口](../server/live_llm_codex_permissions_acceptance.py)

本地安装的两种 CLI 分别生成协议定义，核对了四种权限的线程和回合字段，以及算例工具响应结构。TypeScript 和 Vite 生产构建通过，构建仍报告现有的大分包提示。 [版本协议核对](../../tmp/FF-F28-codex-permissions-20261121/schema-check.json)
