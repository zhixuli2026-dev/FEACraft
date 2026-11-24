# FF-F26 整文件提案和只读历史

Agent 从同一次有界读取取得提案基线摘要和历史预览。普通文本至多保留 256 KiB 的原始版本供逐行比较。二进制文件、大文件和未保存原文的旧记录展示提案内容。单条归档接近既有大小上限时保留写入参数。发给模型的对话上下文使用工具结果，历史预览在平台内留存。 [基线读取](../server/app/services/agent_file_reads.py) · [工具记录](../server/app/routers/agent.py)

待审批卡核对当前文件版本。已完成、已拒绝、已取消和已中断的卡片可以展开原提案，查看路径、理由、差异和执行方式。新建提案按空原文生成差异。已有文本提案使用当时的版本，文件后来改动仍能核对原差异。自动应用与人工批准沿用持久工具结果。导入 JSONL 会话只恢复历史事件。 [历史卡](../web/src/panels/ChatPanel.tsx) · [历史状态投影](../web/src/utils/toolCards.ts) · [归档导入](../server/app/services/conversation_import.py)

认证 S3 的实时文件预览可能短时间占用同账户存储额度。手动批准和拒绝先等待既有额度结算，再登记工具状态。文件写入仍由原有算例事务执行，浏览器的重复审批返回 409。SSE 对 Unicode 行分隔符使用 JSON 转义，JSONL 导入按 LF 划分记录，原文件的 Unicode 文本随归档保留。 [审批与事件流](../server/app/routers/agent.py)

## 验收方法

正式验收用 DeepSeek `deepseek-flash` 通过真实工具定义生成提案。Completions 和 Messages 请求均发往 `api.deepseek.com`，以服务回执核对 HTTP 状态。浏览器使用 Microsoft Edge 和生产构建，通过工作台加载待审批会话、点击批准或拒绝，并用键盘展开历史卡片。发送给服务的消息经额外检查，未包含平台留存的原文件快照。 [真实验收入口](../server/live_llm_file_proposal_history_acceptance.py) · [DeepSeek Messages 协议](https://api-docs.deepseek.com/guides/anthropic_api/)

- 手动批准新建文件，核对批准前缺失、批准后精确正文、历史新增一行及重复批准返回 409。
- 拒绝已有文件的提案，核对原文未写入，并在另行编辑后从历史读回原差异。
- 完整访问模式自动应用，核对实际写入、自动应用标识和后来改写后的历史差异。
- 手动批准 `0-caseDict/caseDict` 的完整文本，核对正文和修订号递增一次。
- 导出并导入已应用记录，在 Edge 核对理由和原差异，确认归档没有待审批工具，尝试审批返回 404。

真实文件边界覆盖空原文、Unicode 行分隔符、二进制类型、超限原文、显式不存在、归档预算及 Unicode 快照的 JSONL 往返。该组调用模型 0 次。 [边界记录](../../tmp/proposal-history-boundaries-l04q0m4s/result.json)

## 2026-11-21 验收结果

两种部署和两种接口的最终四组各通过上述五项，合计 28 次 DeepSeek HTTP 200。整阶段共 59 次 HTTP 200，模拟模型调用 0 次。早期记录保留了会话列表刷新、归档预算缺少导入、Unicode SSE 行划分及 S3 并发读取额度冲突的失败和复验。TypeScript 和生产构建通过，Vite 提示现有分块超过 500 kB。19 个隔离 API 监听均已关闭。 [验收汇总](../../tmp/FF-F26-file-proposal-history-20261121/validation.json)

| 存储与数据库 | DeepSeek 接口 | 实际 HTTP 200 | Edge 和归档证据 |
| --- | --- | ---: | --- |
| 文件系统、SQLite | Completions | 7 | [结果](../../tmp/live-file-proposal-history-nbnidcd6/live-result.json) · [手动历史](../../tmp/live-file-proposal-history-nbnidcd6/manual-history.png) |
| 文件系统、SQLite | Messages | 7 | [结果](../../tmp/live-file-proposal-history-f_fzb8z9/live-result.json) |
| 认证 SeaweedFS、PostgreSQL | Completions | 7 | [结果](../../tmp/live-file-proposal-history-m4l7oq2b/live-result.json) · [原差异](../../tmp/live-file-proposal-history-m4l7oq2b/automatic-history.png) |
| 认证 SeaweedFS、PostgreSQL | Messages | 7 | [结果](../../tmp/live-file-proposal-history-enefpevj/live-result.json) · [导入历史](../../tmp/live-file-proposal-history-enefpevj/imported-history.png) |

文件冲突、并发保存与 API 退出前后的原子提交沿用 FF-F31 的实际存储验收。完整文本替换的双协议错误矩阵由 FF-F27 继续。 [原子保存验收](file-transaction-diagnostics.md#2026-11-21-提案基线读取与原子提交复验)
