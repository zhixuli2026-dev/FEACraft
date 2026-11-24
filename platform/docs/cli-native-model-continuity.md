# CLI 重启、分叉与续聊保持所选模型

2026-11-15。修复实际使用中的问题：新建 CLI 对话会传入所选模型，但原生 `thread/resume` 和 `thread/fork` 未传入。真实 DeepSeek 续聊因此回落到 CLI 默认的 `gpt-6.1-sol`，服务明确拒绝该模型。现在三条路径都使用对话所选模型，缺省时使用该 provider 的默认模型。

协议依据是[官方 App Server 文档](https://learn.chatgpt.com/docs/app-server)及当前安装 CLI 实际生成的 JSON Schema；本地 `ThreadResumeParams`、`ThreadForkParams` 均支持可选 `model`。Schema 生成记录在 `../../tmp/cli-schema-review-6_lmf4e1/result.json`。没有增加协议转换器，也没有改变执行权限。

## 实际 Windows 验收

[完整验收](../../tmp/live-cli-inference-y3kvfy1l/live-result.json)使用本地授权 profile 26 的 DeepSeek / `deepseek-flash`，真实安装的 Windows CLI、隔离 SQLite/文件目录和正式 session/turn/fork API。

1. 模型执行 PowerShell 读取文件；提示词不包含文件中的随机验证字符串。实际审批只接受该文件的单次精确读取，命令退出码为 0，最终答案与文件内容完全一致。
2. 停止并重新启动隔离 API，复用数据库及 CLI 私有历史，重新认证同一测试账户。
3. 从已完成回合原生分叉，生成不同 thread ID；重复提交相同 fork 请求返回同一结果。
4. 分叉会话及原会话分别真实续聊，均在不重新提供答案、不调用新工具的情况下回答原字符串；保持各自 thread ID，无平台文字交接替代。
5. 两个续聊回合消息落库、执行状态 idle；重复提交不产生新回合。全部 session 清理验证通过，隔离 API 已退出。

该成功运行共启动 **3 个真实 CLI 模型回合**。没有逐次观测 CLI 内部 HTTP 请求，不能把回合数称作 API 请求数。原生 usage 保留在报告中；分叉和原会话的累计 usage 包含共同历史，不相加作为账单用量。模拟 LLM 调用为 **0**。

## 保留的失败

| 证据 | 结果 |
| --- | --- |
| [首次 Windows 读取](../../tmp/live-cli-inference-v5qdsrlz/live-result.json) | 实际模型选中读取工具，但测试未接受 CLI 的 PowerShell 包装形式，已取消；后续仅修正精确命令匹配。 |
| [单独读取通过](../../tmp/live-cli-inference-a6y8zgmi/live-result.json) | 真实工具、模型答案与清理通过，尚未包含续聊。 |
| [首次重启测试](../../tmp/live-cli-inference-n8ctvq0d/live-result.json) | 原回合成功；重启测试失败，收尾 HTTP 错误遮蔽了最初异常，无法确证第一原因。后续保存日志改为复制，并保留原异常。 |
| [实际模型名错误](../../tmp/live-cli-inference-myxstsj8/live-result.json) | 原回合及原生 fork 成功，分叉续聊被 DeepSeek 拒绝；模型名为 `gpt-6.1-sol`。修复前真实反例，未改写为通过。 |
| [WSL 程序缺失](../../tmp/live-cli-inference-2bjsfuzj/live-result.json) | session 创建返回 503，真实模型回合数为 0。历史程序位于已移除的旧 VS Code 扩展目录；当前 Windows 扩展旁没有 Linux CLI。 |

## 范围与后续

WSL 运行依赖已修复：依据[官方安装文档](https://learn.chatgpt.com/docs/codex/cli)使用官方 `@openai/codex` Linux x64 包，固定为与当前 Windows CLI 相同的 `0.167.0-alpha.4.1`。核验 npm 分发 SHA-512 完整性及本地程序 SHA-256，保存于 [安装记录](../../tmp/N4-05-linux-cli-install-20261115.json)。当前本机程序位于 `.tools/codex/0.167.0-alpha.4.1/linux-x64/bin/codex`，本地服务器私有 `.env` 已显式配置 `VIBEFLOW_CODEX_WSL_PATH`；隔离验收通过同名进程环境变量指定。以 WSL nobody 身份执行 `--version` 成功。该机器路径不作为其他部署环境的默认路径，程序包也不混入源码快照。

安装过程曾错误假定包内路径为 `codex/codex`，实际为 `bin/codex`，首次提取因此失败；随后对已下载完整包重新核验并按实际布局提取。多余断点续传请求返回 416，没有修改包。进程停止尝试发现下载进程已退出，没有终止其他进程。以上是安装脚本问题，不计为模型失败。

[WSL 完整验收](../../tmp/live-cli-inference-5mr77nm6/live-result.json)随后通过同样的真实读取、API 重启、原生分叉、分叉续聊及原会话续聊流程，启动 3 个真实 CLI 模型回合。原会话续聊期间 CLI 报告网络重连，随后在既有期限内完成；没有增加超时、替换模型回复或重跑整个流程。Windows 与 WSL 两份成功报告合计 **6 个真实 CLI 回合**，不代表 6 次 HTTP 请求。全部会话清理、持久消息、请求幂等和 API 退出均通过；失败和重连事件保留原记录。

上述是 Windows/WSL + FS/SQLite 的完整后端使用流程，补齐 FF-F41、FF-F44 的部分验收；不是整个功能或所有运行环境完成。浏览器完整交互、复杂审批、S3 staging、启动/恢复提交中断和原资源预算剩余条件继续按原计划推进。主线 **48/53，剩 5 项**；资源 **11/17，剩 6 块**；295 项全功能目标保留。
