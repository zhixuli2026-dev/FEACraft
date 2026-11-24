# FF-F50 API 目标续跑

已持久化的目标可从普通工作台会话续跑。`POST /api/agent/conversations/{id}/goal:run` 复用会话执行权，从当前目标开始按轮派发模型请求。目标完成、达到轮次上限、超过工具预算、用户取消或上游失败时停止。取消或断线会关闭流、停止心跳并释放会话执行权。[API 实现](../server/app/routers/agent.py)

工具预算在派发前核对，适用于一个模型回复中的整组工具请求，也适用于一轮中的后续模型请求。请求数量超过余额时，本组工具均未创建或执行，回复记录中没有等待工具回填的悬空调用。目标保留原 ID 并进入 `blocked`，流返回 `goal_tool_budget`。

## 真实模型验收

隔离 API 使用已授权的 `api.deepseek.com` 和 `deepseek-flash`。实际收到 9 次 HTTP 200，7 次读到用量的响应合计 33,459 tokens。主动取消和断线两例均在实际收到响应头后关闭流，未取得这两次的用量。模拟模型调用为 0。[显式验收入口](../server/live_llm_goal_runner_acceptance.py) · [结果](../../tmp/live-goal-runner-_6y_dcrq/live-goal-runner-result.json) · [上游回执](../../tmp/live-goal-runner-_6y_dcrq/upstream.jsonl)

- 真实模型先登记目标。该目标续跑第 1 和第 2 轮，第 2 轮由模型调用 `update_goal` 提交完成。目标 ID 保持，修订升为 2，未派发第 3 轮。
- 剩余工具预算为 1 次，真实模型同一回复请求两个 `read_text_file`。API 在派发前拦截，工具记录为 0，没有留下待回填调用。
- 一轮预算在一次真实请求后进入 `blocked`。同会话的并行启动返回 409，异账户请求返回 404。配额耗尽的请求在上游派发前被拦截。
- 实际取消接口和实际 HTTP 断线分别终止正在接收的响应。两个会话执行权均释放，没有派发下一轮。隔离 API 退出，验收用临时凭据文件已删除。
