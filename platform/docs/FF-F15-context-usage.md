# FF-F15 会话上下文用量

## 行为

API 会话的设置面板显示最近一次模型请求的服务商用量。悬停或展开后可查看总输入、未缓存输入、缓存命中、缓存写入、总输出与推理输出。新请求开始后显示等待状态。请求返回用量后，服务端在同一个会话内保存原始计数投影，经事件流发送给当前会话，刷新页面或重启 API 后仍可读回。模型接连执行工具时，读数跟随最近一次模型请求。

输入包括缓存，输出包括已报告的推理 token。服务商省略的细项显示为“未提供”，明确报告的零仍显示为 0。实际费用取决于服务商账单。界面同时保留当前可见正文、工具结果及草稿的字符数估算。外部 JSONL 可以导入历史内容，导入时会丢弃它声明的服务商用量字段。

Codex 原生面板跟随 `thread/tokenUsage/updated` 保存的快照，显示最近模型请求、线程累计和服务商提供的上下文窗口。原生面板支持悬停和键盘展开。[原生用量](native-cli-usage.md)

使用了 Chat Completions 流的最终 `usage`，以及 Messages 的初始输入计数和后续累计输出计数。对于 OpenAI 官方 Chat Completions 地址，请求 `stream_options.include_usage`。[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/) · [DeepSeek Messages](https://api-docs.deepseek.com/guides/anthropic_api/) · [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)

## 2026-11-22 实测

| 入口 | 总输入 | 缓存命中 | 总输出 | 合计 | 上下文窗口 |
| --- | ---: | ---: | ---: | ---: | ---: |
| DeepSeek Chat Completions | 645 | 511 | 49 | 694 |  |
| DeepSeek Messages | 645 | 384 | 63 | 708 |  |
| Windows 原生 Codex / DeepSeek | 10,919 | 8,576 | 26 | 10,945 | 258,400 |
| WSL 原生 Codex / DeepSeek | 9,676 | 7,936 | 24 | 9,700 | 258,400 |

API 验收共发出 2 个真实 DeepSeek 请求，均为 HTTP 200。逐项比对上游流、SSE、会话快照和重启后的 API 读数。Chat Completions 报告推理输出 41。Messages 明确报告缓存写入 0，推理细项保持未知。Edge 验证了会话切换、空会话估算、键盘展开和跨账户拒绝。[API 回执](../../tmp/live-f15-usage-qumxa2jx/f15-result.json)

Windows 和非特权 WSL 分别跑通 1 个实际 Codex 回合，借助 DeepSeek Responses 返回验收字符串。API 的原生用量与 CLI 原始事件一致，Edge 折叠视图读回相同数据。[Windows 回执](../../tmp/live-cli-inference-c4bltl70/live-result.json) · [WSL 回执](../../tmp/live-cli-inference-dw06y05y/live-result.json)

最终构建从已保存的真实回执复核 1450 像素深色和 960 像素浅色界面，窄面板内可滚动查看费用说明。纯协议验收覆盖安全整数边界、空值与零的区分、计数一致性和导入来源。以上复核没有发起模型请求。[浏览器回执](../../tmp/FF-F15-native-usage-browser-20261122/api-browser.json) · [协议回执](../../tmp/FF-F15-native-usage-browser-20261122/protocol.json)

开发期间一轮 Chat Completions 返回 HTTP 200 后，测试观察器未能及时落盘，异步生成器退出路径修复后才通过双协议验收。另两轮浏览器验证在提交前失败。WSL 首轮隔离进程未指定本机安装路径，创建会话时返回 503，未启动模型回合。复验给隔离进程指定了已安装的 Linux 工件。[验收汇总](../../tmp/FF-F15-native-usage-browser-20261122/validation.json)
