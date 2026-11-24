# 托管部署的 Agent 认证决策

2026-11-03 用户补充决定：“没有，这部分暂时不用管”。正式 OpenAI client 注册、真实 HTTPS 域名／回调及认证方式配置的联调暂缓，当前不再索取这些配置，也不将其作为其余主线工作的阻塞。已完成的网站身份代码和隔离验收保留，功能在未配置时关闭；本项未完成真实服务验收，待用户明确恢复后继续。商业／托管 API / Sign in with ChatGPT 的部署选择、网站身份与模型凭据分离保持不变。

2026-11-03，用户明确选择：**对外托管或商业服务，采用 API / Sign in with ChatGPT**。此决定约束后续账户实现，覆盖旧计划中默认采用 CLI 内置 ChatGPT 浏览器/设备码登录的实现假设；完整账户连接、状态、退出及跨环境体验仍需交付。

## 已核对的外部契约

官方 [App Server 认证说明](https://learn.chatgpt.com/docs/app-server#auth-endpoints)区分本地/开源应用的 CLI 管理认证和托管/商业集成。托管方向采用自己的应用授权与 API 凭据。

官方 [网站 Sign in with ChatGPT](https://developers.openai.com/siwc/website)使用注册的 OAuth client、精确回调地址、PKCE、OIDC ID token 验证和应用自己的会话；当前真实接入还需要获得对应 client 注册。网站身份登录本身不能证明用户已授权模型推理。

官方 [ChatGPT plan 与 app-server](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server)描述获授权 token 经独立 Responses provider 的环境变量传入子进程，刷新后重新启动并显式续接。其所属授权产品及额度资格必须单独核对，不将仅有网站身份 token 当作推理凭据。

## 当前实施顺序

API 凭据绑定已在 N4-03.g2b3a 接通并完成双库与实际 CLI 本地验证，见[实现及证据](hosted-cli-credentials.md)。第 3 项网站授权在 g2b3b 已接通持久后端、账户 UI 和隔离 HTTPS 验证，见[网站登录说明](website-sign-in.md)；正式 OpenAI client/公网回调仍待联调。下一单元为第 4 项的 sandbox 与 WSL 原生 CLI；以下保留完整顺序与验收边界。

1. 原生 CLI 使用平台已授权的 ProviderProfile/API 凭据，按用户及凭据版本隔离私有运行目录，明确 Responses 兼容能力。密钥只在执行进程环境中传递，不写入配置、profile、事件、命令行或聊天记录；推理开始、审批、续租和交付均复验访问权与凭据版本。撤销共享或轮换配置后停止旧执行，不换用另一人的凭据。
2. Metadata 会话仍可用于检查程序与 sandbox；未绑定托管推理凭据的会话不能借用服务器个人 CLI 登录执行模型回合。线程恢复须匹配已核验凭据/配置边界。
3. Sign in with ChatGPT 采用网站授权流程，和模型服务凭据分开。实现持久一次性事务、浏览器绑定、PKCE/nonce、签名/issuer/audience/有效期验证、明确账户关联、取消/过期/重复回调处理和退出。真实 client ID/回调部署属于外部联调条件，不能用测试 IdP 代替真实验收。
4. 接着完成 WSL 原生 CLI 的发行版/程序/home/出生边界、Windows sandbox 状态与用户操作流程，再统一到聊天与设置 UI。

## 验收条件

API 路径核对实际 CLI 使用绑定的获授权真实模型服务、撤销共享或轮换后旧执行范围退出，以及公开消息、停止回执、算例、S3 和快照中的凭据隔离。历史本地 Responses 回执保留为接口与生命周期的协议证据。后续推理验收按仓库 `AGENTS.md` 调用真实服务。账户或配额可用性以相应账户服务的实际返回为准。 [实际 Codex 与 DeepSeek](FF-F56-windows-sandbox.md)

网站授权需用可控 OIDC 服务验证真正签名和错误矩阵，并在注册的真实 OpenAI client 环境中完成授权往返。用户的部署选择已经确定，不需反复询问；实际服务、账户和系统操作在准备到可审阅时再说明所需外部步骤。

主线和全部功能仍见 [主线计划](next-stage-mainline-plan.md)、[完整交付计划](full-function-delivery-plan.md)和 [原生回合说明](owned-cli-turns.md)。本决策不将 53/295 的任何任务标记完成。

## Studio 0.11.0 的认证证据

FF-J02 对照本地原版安装包与解包后的 Studio、AgentHost、Worker 可执行文件，记录文件摘要和限定的协议标记计数。Studio 含 `account/login/start`、`account/login/cancel`、`account/read` 以及本机许可对话框标记。配套 Codex 文档明确将浏览器和设备码登录归给选中的 Windows 或 WSL Codex CLI。SCNET 登录文档另列 AK/SK 签名和会话。 [本地发行包证据](../../tmp/ff-j02-studio-oauth-20261124/release-markers.json) · [原版 Codex 说明](../../extracted/share/doc/vibeflow-studio/docs/codex-local.md) · [原版 SCNET 说明](../../extracted/share/doc/vibeflow-studio/docs/scnet-workbench.md)

本批安装包及配套文档未给出 Studio 独立账户的 OAuth 发行方、授权入口或回调契约。字符串核对仅覆盖列出的协议标记。Studio 本机许可由 FF-J03 记录，平台组织 OIDC 由 FF-J10 记录，商业网站 Sign in with ChatGPT 按本页已确认方案推进。FF-J02 保持待验收，需原版启用环境或发行方文档确认自身账户流程。本轮登录请求和模型调用均为零。 [平台组织 OIDC](FF-J10-organization-oidc.md) · [离线许可](FF-J03-offline-license.md)
2026-11-25，用户明确移出 FF-F55 和 FF-J02。本站登录、账户额度和 Studio OAuth 的既有记录在下文归档，当前模型调用继续使用授权的 API 配置。 [当前交付范围](full-function-delivery-plan.md#21-最新范围规则)

