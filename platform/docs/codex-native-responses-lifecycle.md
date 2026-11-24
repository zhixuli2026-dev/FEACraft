# Codex 原生 Responses 会话验收

2026-11-17。复用已有 Codex app-server、平台会话/回合接口和本地授权 profile 26，实际调用 `api.deepseek.com` 的 `deepseek-flash`。本轮没有修改产品权限或增加隔离层；原生会话已有回环 NO_PROXY，无需重复修复。新增真实验收入口 `server/live_llm_codex_native_responses_acceptance.py`。

## 已验证的流程

使用 Windows 原生 Codex `0.167.0-alpha.4.1`，隔离 SQLite 和算例，按已授权的 full_access 执行：

1. 模型通过真实 PowerShell 工具读取随机文件，回答必须与文件内容相同；提示中不提供随机值。
2. 停止并重启 API，保留数据库和私有 CLI 历史，重新认证同一测试账户。
3. 从已完成回合原生分叉，分支与原线程分别续聊，不再次提供文件内容、不重新调用工具，均回答原随机值，并保持各自原生 thread ID。
4. 真实模型请求进入转发器后取消运行中的回合。回合记录为 cancelled，执行状态恢复 idle，重复提交原请求返回同一个回合。
5. 在取消后的同一对话创建新 session，实际模型再次回复新随机标记，完成并清理。

原生 fork 和续聊请求也核验重复提交不创建新执行。最终轮启动 **5 个真实模型回合**，观测到 **6 次 Responses HTTP 200**，模拟调用 **0**；fork 创建另一个管理会话，但不是额外模型回合。6 个管理会话均有通过校验的持久停止凭据，根与 bridge 进程实际退出，重启前后 API 也已独立核验退出。

取消回合对应的底层 session 按现有语义记录为 `failed / NativeTurnInterrupted`，停止原因为 `cancelled`、`verified=true`；没有改写为正常完成。其余 5 个 session 为 closed。这里证明本地执行已停止，不声称远端服务取消计算或节省计费：被取消请求的实际 HTTP 200 仍计入记录。

## 两轮证据

| 运行目录（项目 tmp 下） | 结果 |
| --- | --- |
| `live-codex-native-responses-qhw_0619` | 四组功能检查通过、6 次真实 HTTP 200；Windows 取消后产生 ConnectionAbortedError，观察器误列为上游失败，整轮 false 原样保留 |
| `live-codex-native-responses-nok8975c` | 补齐观察器对客户端连接中止的分类后重跑，四组检查、6 次 HTTP 200、6 份停止凭据全部通过 |

两轮合计 12 次真实 HTTP 200、10 个真实模型回合；不能把第一轮整套验收算作通过。观察器仅在已经保存实际上游响应之后单独记录客户端断开，不制造模型回复或修改协议。最终审查 `tmp/N7-05-codex-native-responses-review-v2-20261117.json` 校验 661 份来源及本机 CLI 二进制摘要；导入的 continuation helper 已审阅。

各目录保存 `native-result.json`、`upstream.json`、真实 SSE 事件及 `independent-exit-checks.json`。汇总为 `tmp/N7-05-codex-native-responses-evidence/validation.json`。CLI 的 thread usage 含共享历史，不把分支与原线程累计量相加当作账单。

## 验收范围

本轮是实际基础设施与真实模型的组合验收，没有模拟 LLM 或另行运行历史模拟集合。转发器完整读取真实上游响应再送回 CLI；平台 SSE 事件内容已验证，但这不证明公网逐 token 首字延迟或传输背压。服务停启是有序重启，不是宿主断电。未改前端，未重复运行无关构建。

Windows Codex 原生主流程现在有本轮明确证据。其余 harness 的深度协议接入、原权限/跨环境组合、DeepSeek 模型元数据 fallback，以及旧偶发 SQLite 关闭问题仍按既有计划处理。主线 **48/53，剩 5 项**；资源 **11/17，剩 6 块**；全部 **295 项**交付目标保留，不将局部流程通过算作整项交付。
