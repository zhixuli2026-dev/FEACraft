# FF-F08A 文件提案审批差异

审批卡以待批准提案保存的 `baseline_sha256` 为准。可读文件读回内容摘要后再生成差异。空基线先经文件版本接口确认路径依然不存在，再展示新建内容。外部修改、原文件不再存在和缺少旧提案基线有明确说明。批准执行时继续使用服务端事务内的原摘要检查。[审批卡](../web/src/panels/ChatPanel.tsx) · [审批事务与真实冲突](N7-01-agent-file-conflict.md)

文本增删按完整编辑序列计数。预览折叠不变的区段，并最多呈现 400 行。文件末尾的换行不会增加一个虚假空行，缺少换行的改动有标记。整段替换按后端字面语义处理 `$&` 等字符串。极复杂的差异达到计算预算时提供具体提示和提案参数，当前版本为二进制时展示替换类型及大小。清除所有文本行仍属于文本文件更新，整个文件删除由项目文件操作处理。[差异实现](../web/src/utils/diff.ts)

## 复验

`live_llm_approval_diff_acceptance.py` 是显式真实模型入口。隔离浏览器需要当前生产构建和安装 Playwright 的 Python。入口使用已授权的 DeepSeek profile，测试内容为独立的无害文本。这轮设计最多四个上游请求。提供商结果由实际 HTTP/SSE 流转发，认证密钥不进入验收回执。[复验入口](../server/live_llm_approval_diff_acceptance.py) · [DeepSeek 工具调用](https://api-docs.deepseek.com/guides/tool_calls/)

## 2026-11-19 正式验收

`api.deepseek.com` 的 `deepseek-flash` 实际返回三份工具提案，并在真实批准新建文件后续答。四次官方 HTTP 均为 200，四份上游 usage 合计 19,389 tokens。模拟模型调用为 0。已确认隔离 API 和端口退出，Fernet 加密密钥仅存在于隔离进程内存。浏览器最后刷新关联的普通能力自检保留一条未授予运行身份的准备记录，所有已记录的 Windows 进程身份均已核对退出。[正式验收](../../tmp/live-f08a-approval-diff-dn9gfoz8/live-result.json) · [实际上游](../../tmp/live-f08a-approval-diff-dn9gfoz8/upstream.jsonl) · [独立退出与用量](../../tmp/live-f08a-approval-diff-dn9gfoz8/independent-receipt.json)

真实 Edge 在 6,500 行的文本中显示第 6,201–6,202 行的替换，统计为新增 1 行、删除 2 行。实测增加和删除样式，新增行按原字符显示 `$&`。一次真实外部更新和之后一次带原摘要的实际 `DELETE /file` 都让旧提案显示版本失效，审批卡停止展示增删并禁用批准。经项目文件接口恢复外部内容后拒绝旧提案，最终读回的摘要与外部编辑一致。[长文本差异](../../tmp/live-f08a-approval-diff-dn9gfoz8/approval-long-diff.png) · [外部更新后](../../tmp/live-f08a-approval-diff-dn9gfoz8/approval-stale-diff.png) · [外部删除后](../../tmp/live-f08a-approval-diff-dn9gfoz8/approval-deleted-baseline.png)

新建文件显示新增 2 行、删除 0 行。实际批准执行后核对写入字节与提案一致，并重新打开历史确认结果。非文本路径由文件读取接口实际分类，夹具的字节为无害 UTF-8 测试文本。二进制说明显示文件类型与替换语义，拒绝后原始字节保持。Agent 当前写入提案范围为整文件文字更新和文本替换。整文件删除由独立项目文件入口执行，此轮核对的是删除后旧提案的基线失效。[新建审批](../../tmp/live-f08a-approval-diff-dn9gfoz8/approval-new-file.png) · [非文本说明](../../tmp/live-f08a-approval-diff-dn9gfoz8/approval-nontext.png) · [浏览器结果](../../tmp/live-f08a-approval-diff-dn9gfoz8/approval-diff-browser.json)

差异算法另与独立 LCS 对照核验 14,641 组短文本。6,500 行的远端两处改动保持精确，450 行新增在最多渲染 400 行时仍统计为新增 450 行。生产构建和 Impeccable 检测通过。阶段前后 C 盘可用空间均为 22.11 GiB，Docker 构建缓存为 0 B。[前端构建](../../tmp/f08a-build-20261119.log) · [界面检测](../../tmp/f08a-impeccable-20261119.json)
