# Codex 分叉与文字交接

2026-11-22，FF-F44 验收完成。Windows 和非特权 WSL 在文件系统组合通过。非特权 WSL、PostgreSQL 和认证 SeaweedFS 通过原生分叉、旧回答交接和运行中交接三个浏览器场景。 [签收回执](../../tmp/FF-F44-native-browser-20261121/s3-validation.json)

在原生聊天的“原生运行记录”选择最后一个已完成并已核验收尾的回合。当前运行环境与来源一致时，可以使用“原生分叉”创建独立线程。分支从已提交的回答继续，首条消息默认使用“恢复原生线程”。原生线程边界、凭据版本和环境由服务端复核。 [原生分叉协议](native-cli-forks.md)

从早期回答继续时，在该回答下选择“从这条回答交接”。要在原生回合中途打开另一条讨论，可在运行记录中选择“交接至本轮提问”。交接截止于服务端已提交的回答或提问。中途交接带上本轮已提交的提问，尚在流式输出的文字仍留在原回合。已有原生或导入的完整回答可按事件 ID 定位。

文字交接新建会话，并保存最多 180,000 字符的近期文字。其范围是选中位置以前的提问、完整回答和此前同类文字交接。两会话使用同一算例，工具调用、工具输出、审批、目标和停止状态各归原会话。交接后首次发送会启动独立原生线程。服务端用 `context.handoff` 校验这次实际提供给线程的文字长度和摘要值。

对于 S3 算例，原生分叉在专用私有工作目录复制线程历史。该会话只接受分叉操作。分支第一次发送消息时启动普通会话，下载当前算例快照并恢复已登记的线程。两步沿用同一算例和凭据的核验。

## API 与恢复

- `POST /api/v1/conversations/{cid}/handoff` 接受 `head_event_id` 或 `before_answer_turn_id`。回答可来自当前会话可见的祖先记录，原生回合提问必须属于当前用户和来源会话。
- 越过可见历史或缺少访问权限时返回 404。可见事件尚未成为完整回答时返回 409。两个位置同时提交时返回 422。只提供 `keep_chars` 的旧 API 入口仍按原方式处理摘要。
- 文字交接在同一数据库事务保存新会话和文本，审批仍可在原会话继续处理。原生分叉沿用持久请求 ID 和结果查询恢复。
- 本次使用现有数据库表。回退时可恢复本次路由、历史读取、会话上下文及前端代码，原生分叉记录和已保存的文字快照可由既有会话历史读取。隔离验收使用临时数据库与存储。

## 验收证据

- 实际 Windows Codex、SQLite、文件系统和 Edge 跑完五个 DeepSeek `deepseek-flash` 回合。原会话只执行一次批准过的文件读取。浏览器在待审批时完成文字交接，原审批仍待处理。原生分叉和两个文字分支各有独立线程，三个续聊都无新增命令和审批。 [Windows 回执](../../tmp/live-native-fork-browser-mzv8edxx/browser-result.json)
- 非特权 WSL、PostgreSQL、文件系统和 Edge 跑完同样的五个真实回合。WSL 沙箱允许直接读取本案文件。两套环境均拒绝明显无效、越界和跨账户交接。 [WSL 回执](../../tmp/live-native-fork-browser-ykpq2jm5/browser-result.json)
- 单独核验了 180,000 和 1,000 字符边界、可见祖先中的旧回答和 API/原生上下文投递。测试复用了实测落库的回答，运行期间没有请求模型。 [边界回执](../../tmp/FF-F44-native-browser-20261121/bounded-history.json)
- WSL 复验发现会话关闭与调用方结束互相等待。关闭流程改为等待启动函数完成，随后核验已登记进程的实际收尾。专项用已完成真实 stdio 初始化的进程触发启动中关闭，调用方退出且没有建立模型线程。 [启动关闭专项](../../tmp/FF-F44-native-browser-20261121/startup-close-race.json)
- 认证 SeaweedFS 的早期复验在准备分叉会话时达到 45 秒期限。修复后，实际 Edge 同一流程完成分叉及首次续聊。分叉启动约 41.25 秒。回执核对专用目录、独立会话和续聊时的实际算例快照。DeepSeek 在之后的源会话新消息中断流，记录保留。另一次复跑在约 36.06 秒完成分叉启动，模型续聊触发断流，已核验停止。 [分叉和续聊证据](../../tmp/live-native-fork-browser-lae7ht4s/browser-result.json) · [复跑回执](../../tmp/live-native-fork-browser-_9wn86zd/browser-result.json) · [原超时记录](../../tmp/live-native-fork-browser-avhyyl2c/browser-result.json)
- 私有额度准入等待同账户的活动远端写入结算，再复核实际用量和额度版本。实测持有真实 S3 额度期间另一笔数据库写入仍可提交。原额度结算后准入成功，当时活动远端额度为零。 [额度等待专项](../../tmp/FF-F44-native-browser-20261121/private-credit-wait.json)
- 认证 SeaweedFS 的旧回答交接验证较新回合留在原会话，独立线程收到了精确的历史快照。中途交接专项验证分支只收到截止本轮提问的文本，来源回答仍可完成，分支在独立线程真实续聊。五类越界、非法或跨账户请求均被拒绝。 [旧回答回执](../../tmp/live-native-fork-browser-kqaha_3q/browser-result.json) · [中途交接回执](../../tmp/live-native-fork-browser-7k5nseaq/browser-result.json)

两套文件系统组合合计 **10 个通过的真实 CLI 回合**。认证 S3 的三份签收回执合计 **7 个通过的真实 CLI 回合**，保留对照运行中的断流和超时记录。测试按 CLI 回合计数，模拟 LLM 为 0。 [文件系统回执](../../tmp/FF-F44-native-browser-20261121/validation.json) · [S3 签收回执](../../tmp/FF-F44-native-browser-20261121/s3-validation.json)

使用当前 Web 构建、安装的 Codex 和本机授权 DeepSeek 配置运行复验。入口参数 `--runtime` 选择 Windows 或 WSL，`--postgres --s3` 使用临时 PostgreSQL 和认证 SeaweedFS。默认覆盖三个场景，`--scenario handoff` 和 `--scenario mid` 可复验文字交接或单独复验中途交接。回执放在 `tmp`。 [真实验收入口](../server/live_llm_cli_fork_browser_acceptance.py)
