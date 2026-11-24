# 托管原生 CLI 的 API 凭据绑定

2026-11-03，N4-03.g2b3a。根据用户确认的[托管部署决策](hosted-agent-auth.md)，原生 CLI 的推理凭据来自平台 ProviderProfile。网站 Sign in with ChatGPT 的身份登录另行实现，不能拿身份 token 代替推理授权。

## 产品接口和选择语义

`POST /api/v1/codex/sessions` 新增 `provider_profile_id` 和 `provider_wire_api`。例如：

```json
{
  "case_id": 1,
  "conversation_id": 2,
  "provider_profile_id": 3,
  "provider_wire_api": "responses"
}
```

选择必须是当前用户拥有或仍可共享访问的配置，其所有者必须处于 active。OpenAI 官方基础地址可以省略协议字段；其他兼容服务必须明确选择 `responses`，并实际提供 Responses API。Messages 协议、不安全的 HTTP 地址、嵌入凭据或查询参数的 URL、完整 `/chat/completions` 等请求路径及无效密钥在启动前拒绝。声明协议不等于服务已通过真实推理验收。

Session 显式选择自己的 provider，不修改 Conversation 的选择；同时冻结 Conversation 当时的 provider ID。后续修改会话选择使旧 Session 失效。模型取 Conversation.model，空值时取绑定配置的 default_model。没有显式绑定 provider 的 Session 仅供环境、模型目录和 sandbox 等检查；原生回合接口返回 409，不借用服务器个人登录。

会话及账户响应增加 `credential`，包含 `mode`、`inference_configured`，已绑定时还包含 provider ID、provider 名和 wire_api。`inference_configured` 只表示配置已绑定，不证明网络、额度或模型资格可用。已有会话历史继续可读，旧版缺少绑定的记录按 unbound 显示。

## 凭据生命周期

`codex_credentials.py` 保存不含密钥的描述：profile/owner ID、服务、基础地址、默认模型、协议及 revision。revision 对密文与执行配置计算摘要；重新加密同一密钥也生成新的边界，修改显示名称不改变边界。

私有 home 按用户、Windows runtime、profile ID、revision 隔离。owner marker 与配置必须一致；已存在但被改动的配置不会被覆盖或注入新凭据。CLI 从 `VIBEFLOW_CODEX_PROVIDER_TOKEN` 环境变量读取密钥；配置文件和命令行只含变量名。固定配置同时通过 CLI overrides 传入，工具 shell 的环境过滤显式排除此变量，且禁止 login shell。该配置方法遵循[官方配置说明](https://learn.chatgpt.com/docs/config-file/config-advanced#shell-environment-policy)，实际安装版本的 shell 隔离已经验证。

初始选择、实际根/bridge 放行、线程/回合启动、审批、5 秒续租和最终交付复验访问权及 revision。共享撤销、owner 禁用、密钥轮换、基础地址/默认模型变化或 Conversation 选择变化都会使旧执行失效。续租发现变化后关闭所属进程；后续审批和交付也独立复验，不等待续租才检查。不会自动换用另一配置或重发原始请求。

显式 resume 仍要求之前回合完成且所属进程已核验退出，并匹配程序、配置、home、来源、权限、模型及凭据描述。凭据版本变化目前要求新线程。恢复拒绝路径修复了清理任务与执行任务互相等待的问题，由执行任务的 finally 统一退役。

API 密钥仅作为当前执行的临时脱敏值，不写入秘密问答的持久脱敏 vault；公开文本、流、审批、账户/模型元数据和初始化摘要均处理已知密钥。涉及实际协议身份的 thread/turn ID 若包含该密钥则拒绝。进程退出核验后清除服务端保留的环境变量值。私有 CLI 的原生历史仍属于敏感运行数据，不能作为公开文件导出。

## 验证与证据边界

- `codex_credentials_smoke.py --postgres`：SQLite/PostgreSQL 各 21 项，使用产品 HTTP 接口、真实所属进程和可控协议 worker。覆盖未绑定/外人拒绝、协议和 URL、无效 header、根登记后轮换阻止 payload、环境注入、跨 delta/规范历史脱敏、显式续接、秘密问答 vault、元数据脱敏、配置篡改、轮换停止/旧审批拒绝/新 home、改名、改端点、会话换配置、跨用户私有目录、撤销共享和禁用 owner。
- `codex_turn_actual_smoke.py`：实际安装 Codex CLI、本地 Responses 服务 7 项。密钥经产品 ProviderProfile 加密保存并由实际请求的 Bearer header 核对；含计划文本、显式 resume、问答、实际 shell 无凭据继承、失败、取消、公开回执及私有 CLI 文件不含测试密钥。没有远端推理或个人 ChatGPT 登录。
- 原回合双库各 34、会话各 34、暂存各 8、双 API owner 各 6，以及协议辅助 7/7 通过。暂存仍是带内容 ETag 的文件式 S3 替身，不是远端 S3。

汇总位于 `tmp/mainline-N4-cli-credential-evidence/`，来源及测试产物路径在 `evidence-origin.json`。本批不改变数据库 schema；head 仍为 `f97e54a8d310`，30 个业务表。未迁移或重载开发数据库，未重跑全量 offline/writes/CAE、浏览器和 core。

## 剩余交付

继续网站 Sign in with ChatGPT 的持久 OAuth 事务、浏览器绑定、PKCE/nonce、签名和 issuer/audience/有效期检查、明确身份关联、取消/过期/重复回调/退出。真实联调需要注册的 client ID 和精确部署回调；测试 IdP 不代替真实授权验收。

随后继续 WSL 原生 CLI、Windows sandbox 状态与设置、账户/额度及统一 UI、凭据刷新与受控历史迁移、专业执行入口和 N4/N5。每用户 OS/容器隔离、KMS、网络出口和远端 S3 仍未完成；这里的私有目录及环境过滤不能证明硬隔离。主线 53 项、全功能 295 项、24 故障场景和 12 历史决策的范围保留，本单元不关闭任何完整功能。
