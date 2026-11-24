# 对话流与旧会话兼容

FF-F02 复验当前流式 Agent、非流式正式接口、JSONL 读写和旧记录在工作台中的显示。既有的导出、分叉和旧 Codex 缓存边界见 FF-F49 的交付记录。 [会话历史边界](conversation-export-and-legacy-cache.md)

隔离算例在实际 DeepSeek `deepseek-flash` 上完成工具读取与最终回复，按请求核对 SSE、运行状态、启动与完成时间和持久事件。主动断开真实 HTTP 客户端后，运行和请求事件均收尾。JSONL 回导逐条校验 16 个事件，并复验两个旧运行使用相同 request ID 时，各自的开始与终态仍独立。导入的 `reported_usage` 由生产解析器剥离，历史文件不能作为提供商用量凭证。本轮非流式 `/llm/translate` 返回「传热系数」。最终一轮真实 DeepSeek 请求 6 次，全部 HTTP 200，包含验收凭据回读。模拟 LLM 0 次。 [生命周期回执](../../tmp/live-cli-startup-gpesazvp/jsonl-lifecycle.json) · [实际模型回执](../../tmp/live-cli-startup-gpesazvp/live-result.json)

使用上一轮实测的消息和工具记录生成旧格式文件，在采用当前构建的实际 Edge 中导入并读回原模型回答。浏览器回导本身不启动推理、不分配模型配置。早期两次验证卡在用量字段的过时断言，保留原报告。 [Edge 回执](../../tmp/ff-f02-legacy-edge-8h47iqqg/legacy-browser.json) · [历史页面](../../tmp/ff-f02-legacy-edge-8h47iqqg/legacy-history.png) · [早期回执](../../tmp/live-cli-startup-fr2oe6bp/live-result.json)
