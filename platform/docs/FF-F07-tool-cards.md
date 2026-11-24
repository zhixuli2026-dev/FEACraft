# FF-F07 工具卡状态与图标

工具卡按实际调用 ID 合并状态与结果。排队、运行、等待确认、成功、失败、取消和中断分别显示对应状态。旧浏览器回执记录过“已生成文件修改 · 失败”。现在只有工具确实完成时，标题才使用“已读取文件”“已查看目录”等完成式。类型图标遵循现有工具名、旧别名与旧中文标题映射。[工具卡](../web/src/panels/ChatPanel.tsx) · [状态与类型映射](../web/src/utils/toolPresentation.ts) · [旧版冲突回执](../../tmp/live-deepseek-approval-yyqg2buf/browser-conflict.json) · [无模型映射核验](../../tmp/f07-tool-card-state-matrix-20261119.json)

真实读取的文件缺失时，工具通过已有失败协议报告路径，前端显示失败。读取已有文件或列出目录继续走实际存储。回归检查从公开工具执行结果确认失败标志和原因。[文件执行](../server/app/services/tools.py) · [文件边界断言](../server/agent_file_read_boundary_checks.py)

既有真实模型验收覆盖待审批、批准后真实写入、拒绝旧提案、审批中外部修改导致的失败，以及 API 重启后工具状态恢复。普通读取、目录和搜索已有真实上游结果。该项的新增验收聚焦实际模型选择只读工具时的排队、运行、失败及成功图标和历史重载。[审批与拒绝](FF-F08A-approval-diff.md) · [真实审批失败](N7-01-agent-file-conflict.md) · [启动恢复](agent-startup-reconciliation.md) · [正式读取工具](agent-storage-read-tools.md) · [显式验收入口](../server/live_llm_tool_cards_acceptance.py)

## 正常工作台复验

Edge 使用正常 API Agent 会话。DeepSeek 在同一批次实际选择列目录及读取一个不存在的文件。夹具在生产工具已提交排队、运行两种状态后短暂停留，让浏览器检查卡片，然后通过原执行器完成实际操作。目录工具的文件夹图标对应“已查看目录 · 已完成”，结果含仅由隔离夹具随机生成的实际文件名。缺失文件的文件图标对应“读取文件 · 失败”，路径缺失原因为 `FileNotFoundError: 文件不存在`。该卡在三个状态中保持同一 DOM 节点；刷新后两张卡和结果一致，其他账户读取返回 404。已有文本原字节保持，缺失路径始终返回 404。[排队截图](../../tmp/live-f07-tool-cards-63926hbw/tool-queued.png) · [运行截图](../../tmp/live-f07-tool-cards-63926hbw/tool-running.png) · [刷新后结果](../../tmp/live-f07-tool-cards-63926hbw/tool-history.png)

从 `platform/server` 运行以下入口，使用已授权的 profile。将前端构建目录和安装 Playwright 的解释器换成本机实际位置。

```powershell
.venv/Scripts/python.exe live_llm_tool_cards_acceptance.py --source-profile-id 26 --dist ../../tmp/ff-f07-tool-cards-20261119/web-dist --browser-python C:/ProgramData/anaconda3/python.exe
```

正式验收共两次 `api.deepseek.com` 的 `deepseek-flash` 请求，均返回 HTTP 200，报告 8,777 tokens。模拟调用为零。48 个 SSE 帧和 SQLite 的工具状态及真实结果一致，回合及两次请求均完成。独立读回还核对了既有真实 F08A 的待审批和取消状态，以及既有真实审批重启后的中断状态。验收退出 0，隔离 API 和端口均已关闭，临时密钥未落盘。[正式回执](../../tmp/live-f07-tool-cards-63926hbw/live-result.json) · [独立数据库与退出核对](../../tmp/live-f07-tool-cards-63926hbw/independent-tool-state-proof.json) · [审查清单](../../tmp/f07-tool-cards-live-review-20261119.json)

通过审查登记后，九项实际文件系统检查全部通过，覆盖空文件、字符及字节限制、文件缺失、路径校验、目录过滤和搜索上限。此轮检查调用实际工具，模型调用为零。[文件边界回执](../../tmp/agent-read-boundaries-b_f079zo/result.json)
