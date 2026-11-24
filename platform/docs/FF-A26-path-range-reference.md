# FF-A26 算例文件的 `@path` 行列范围引用

已存在的算例路径带有格式错误的行列后缀时，Agent 在输入框旁提示附件错误。列号必须落在每一条选中行内，哪一行越界会显示在提示中。格式错误、倒序行号、行越界及列越界均在浏览器发送消息前拒绝，并保留草稿。Node 回归同时覆盖无范围、单行、多行、多行列切片、短行和路径文本格式。[范围实现](../web/src/utils/pathRefs.ts) · [Node 复验](../tools/path-refs-smoke.mjs)

2026-11-19 在真实 Edge 中提交 `@Ref/path-range-proof.txt:2-3:22-39`。验收从浏览器发出的消息和实际 HTTPS 请求核对同一个正文摘要。DeepSeek 请求携带所选两行的精确列内容。选中行两侧的四个标记和相邻行的两个标记均未出现在实际请求中。模型回显了两个随机校验码，实际 SSE 回复与持久会话的回复一致。四项拒绝用例的消息 POST 均为 0，正式引用的消息 POST 为 1。[浏览器回执](../../tmp/live-path-range-0uoeo89d/browser-path-range.json) · [请求范围回执](../../tmp/live-path-range-0uoeo89d/upstream.jsonl)

使用用户授权的 DeepSeek Profile 26，实际服务 `api.deepseek.com`，模型 `deepseek-flash`。正式推理 1 次，HTTP 200，共 3,690 tokens，完成 ID `fcb4239a-525a-4137-afe6-daa7253989ad`，模拟模型调用 0 次。隔离 API 原进程已退出，临时密钥已清理。前端构建和验收数据均在 D 盘，C 盘前后可用空间四舍五入均为 22.21 GiB。[真实模型与退出回执](../../tmp/live-path-range-0uoeo89d/live-result.json) · [D 盘构建日志](../../tmp/a26-path-range-build-20261119.log)

真实模型复验入口为 [live_llm_path_range_acceptance.py](../server/live_llm_path_range_acceptance.py)。
