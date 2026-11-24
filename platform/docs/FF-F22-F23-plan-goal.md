# FF-F22 / FF-F23 三态计划与会话目标

2026-11-19。计划条只在成功的 `update_plan` 回执到达后更新。服务端核验标题、`pending`、`in_progress`、`completed` 和最多一个进行中步骤。成功后的步骤从同一事务写入的工具记录恢复，接口的 `current_plan` 独立于消息分页返回。原有成功工具记录沿用同一读取规则。空的成功计划可以清除计划条。[工具参数校验](../server/app/services/tools.py) · [会话读取](../server/app/routers/agent.py) · [计划和目标面板](../web/src/panels/ChatPanel.tsx)

`update_goal` 沿用会话内追加的目标事件。目标、验收条件、轮次预算、工具预算和停止原因由事件持久化，公开接口返回 `current_goal`。当前会话收到更高版本后，晚到的旧状态不会把显示退回。只更新目标时仍在普通消息入口完成当前回合，专用目标执行按钮及自动续轮属于 FF-F50。

## 真实验收

Edge 发送三条普通消息，DeepSeek 自行选择工具。初次发出三种计划状态和目标第 1 版，刷新后仍可读。实际隔离 API 退出并冷启动，在只取一条历史事件的请求中仍能读取两项完整状态。新的真实上游请求确实收到系统提示中的持久目标和 4 轮／12 次工具预算。

第二条消息要求模型原样发出两个进行中步骤及无停止原因的完成目标，工具实际拒绝两次并保留原计划和第 1 版目标。第三条消息省略原目标内容，模型使用恢复的目标提交完成状态。正式会话事件仅含同一目标 ID 的第 1、2 版，显示第 2 版、原验收条件和精确停止原因。切换到独立会话后两条状态均为空，切回恢复。另一个账号读取返回 404。普通消息仅启动三个回合，未发起目标执行请求。[真实浏览器和持久化回执](../../tmp/live-plan-goal-57d0r468/live-plan-goal-result.json) · [活动目标画面](../../tmp/live-plan-goal-57d0r468/plan-goal-active.png) · [完成画面](../../tmp/live-plan-goal-57d0r468/plan-goal-completed.png)

实际 `api.deepseek.com` / `deepseek-flash` 共 6 次 HTTP 200，实际工具调用 6 次，上游报告 31,951 tokens，模拟模型调用 0。验收环境为 SQLite、文件系统和独立 Edge，使用已授权配置临时建立隔离 profile。两次 API 出生身份均核验退出，端口关闭，验收用临时 vault 密钥已移除。[上游元数据及清理核验](../../tmp/live-plan-goal-57d0r468/live-verification-summary.json) · [显式真实验收入口](../server/live_llm_plan_goal_acceptance.py) · [DeepSeek 官方 API](https://api-docs.deepseek.com/)
