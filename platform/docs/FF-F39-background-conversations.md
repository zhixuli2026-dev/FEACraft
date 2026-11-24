# FF-F39 多会话后台运行与面板恢复

每条输出流始终绑定原账户、算例、会话和客户端运行实例。关闭 Agent 面板时取消该视图的订阅，浏览器仍接收原回合。重开面板时恢复本算例最近查看的会话，从该回合缓冲显示已有内容并接收后续事件。桌面端关闭后可从工作台顶部点击「打开 Agent」，小屏继续使用面板选择器。 [会话视图](../web/src/panels/ChatPanel.tsx) · [工作台入口](../web/src/shell/Shell.tsx)

发送响应返回已提交的消息事件 ID，供客户端合并正在显示的消息和持久历史。答复完成时，已有的完成前历史请求失效，随后读取最新记录。停止只作用于原回合。在浏览器连接实际断开时，仍按现有回合恢复规则保存中断检查点。 [消息提交与事件 ID](../server/app/routers/agent.py) · [真实模型测试](../server/live_llm_conversation_stop_acceptance.py)

旧版在真实 DeepSeek 回合完成后，重开的面板仍显示旧片段并保留停止状态。最终 Edge 二次重开后接收到后续正文及完成事件。故意晚送实际完成前读取的历史响应，界面仍保留唯一的已提交回答。另一次回合在面板关闭期间完成，重开直接读回完整回答。两个回合均核对了原运行 ID 和数据库回答记录。 [旧版对照](../../tmp/ff-f39-panel-3dq7z0nu/result.json) · [最终 Edge](../../tmp/ff-f39-panel-9lyv6ibz/result.json) · [最终界面](../../tmp/ff-f39-panel-9lyv6ibz/panel-final.png)

两路并行、会话切换、指定回合停止、跨账户拒绝、真实断线和恢复续跑兼容回归通过。最终面板专项有 2 次真实 `deepseek-flash` HTTP 200，2 次均报告用量。并行与停止兼容另有 5 次真实 HTTP 200。模拟调用为 0。前端编译及定向 lint 通过，lint 仍报告已有 React 警告。 [并行及断线回归](../../tmp/ff-f40-control-dihayk_b/result.json) · [阶段汇总](../../tmp/FF-F39-background-conversations-20261121/validation.json)
