# Claude 结构化协议客户端

2026-11-17。新增 `server/app/services/claude_stdio.py`，通过真实 Claude Code 的 stream-json 双向协议工作，底层复用现有 `OwnedStdio`。保留原始 Claude 事件、session ID、工具请求及结果，不转换成假的 Codex RPC。参考 [Anthropic 官方 Python SDK 协议实现](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/query.py) 和[官方 SDK 参考](https://code.claude.com/docs/en/agent-sdk/python)，并核对本机 CLI 帮助。

## 实现及使用边界

客户端支持初始化控制握手、消息发送、原生流式事件、工具许可回复、interrupt、原生 resume/fork 和执行范围关闭。使用独立 CLAUDE_CONFIG_DIR，复用现有提供方环境生成；不读取或改写个人 Claude 配置。启动器构造函数接收实际原生程序路径和模型，不经过 shell 拼接。权限回复绑定尚未结束的原始请求 ID，发送后不能再次回复。

客户端沿用已有流大小限制、进程管理及启动登记回调。它尚未成为公开 API 会话注册器：本轮登记回调保存的是验收目录中的实际进程凭据，没有伪称写入平台 NativeCliSession 数据库。平台服务必须在回调中接入已有持久归属，并在运行期间保存事件和审批状态。

`command()` 提供平台权限模式到 CLI 参数的映射；本轮实际验证的是 approve_writes/manual。上层仍须落实平台 ask/plan 的只读约束，不能将 CLI manual 直接当作平台只读授权。没有改变现有产品权限默认值。本轮未验证其他模式或 WSL。

## 真实验收

入口 `server/live_llm_claude_stdio_acceptance.py`，审查文件 `tmp/N7-05-claude-stdio-review-20261117.json`，校验 663 份来源及本机 CLI 二进制摘要。使用已授权 profile 26 的实际密钥与 `deepseek-flash`，通过真实 Claude Code 2.1.218 调用官方 `api.deepseek.com/anthropic`。完整请求与响应原样转发，无生成回复、模拟 provider 或模拟 CLI。

实际证据目录 `tmp/live-claude-stdio-_t9kud3k`，七项检查全部通过：

1. Read 工具读取随机文件，真实工具结果和最终模型回答均包含该文件内容，产生原生 stream_event。
2. Write 实际请求许可，接受原始输入后创建 allowed.txt，内容完全匹配；重复回复被拒绝。
3. 拒绝另一个 Write 请求，denied.txt 未创建，模型收到拒绝后结束。
4. 真实上游请求开始后发送 interrupt，CLI 返回 `error_during_execution / aborted_streaming`，没有将中断算成成功回答。
5. 同一个进程、同一个 native session 中再次发起真实回合，回答新随机标记。
6. 关闭原进程，启动新进程原生 resume，不提供旧随机答案、不再调用工具，正确回答旧文件内容且 session ID 相同。
7. 再启动进程原生 fork，同样正确回答旧内容，session ID 与原会话不同。

合计 **7 个真实模型回合、10 次 Messages HTTP 200、模拟 0 次**。3 个实际执行范围都有通过校验的停止凭据，根及 bridge 进程独立探测均已退出。中断请求对应的远端 HTTP 200 仍计入，不声称远端计算或计费已取消；CLI 的中断 usage 为零也不代表服务端实际费用为零。

事件和原生 usage 保存在各 `*-events.json`；`stdio-result.json` 包含检查、身份、停止凭据和上游回执。观察器完整读取真实响应后转发，所以这轮不证明公网逐 token 延迟。没有运行历史模拟回归。产品前端未修改，无需重复前端构建。

## 剩余交付

这次完成了 Claude 协议客户端及实际验证，尚未完成 Claude 的平台持久会话、事件/审批路由、Agent 面板选择和对应端到端验收。接下来复用现有会话归属、停止与恢复机制接入这些功能；不新增权限层或扩大故障退出条件。OpenCode/Kimi 深度协议和原矩阵继续。

主线 **48/53，剩 5 项**；资源 **11/17，剩 6 块**；295 项全功能目标保留。FF-F57/J25 保持实施中，不因客户端通过而整项关闭。
