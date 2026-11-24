# FF-F40 会话停止与运行配置锁

API 发送消息时，同一事务写入用户消息和会话执行归属。并发发送及权限变更在原会话控制锁下检查。流结束前释放归属，连接在首帧前断开时也释放，已受理的用户消息保留。浏览器发送停止请求时附上当前回合 ID，服务器只发信号给匹配的运行。旧客户端可继续发送原来的请求。 [会话 API](../server/app/routers/agent.py) · [工作台](../web/src/panels/ChatPanel.tsx)

Edge 在活动回合期间禁用后端、profile、模型、权限和推理设置。用户可以切换查看其他会话，各回合继续使用已选配置。点击停止即开始两秒计时，服务器回执未到时中止原来的控制请求及输出连接。协作停止显示「已取消」，断线保留的正文显示「已中断」。浏览器重载从持久记录恢复相应状态。 [真实浏览器测试](../server/live_llm_conversation_stop_acceptance.py)

| 场景 | 实际结果 |
| --- | --- |
| API 重复发送 | 旧版两个同会话请求都返回 200、写入两条消息，只执行一个模型回合。新版在 SQLite 与 PostgreSQL 中返回 200 与 409，只写入原请求。活动回合修改权限返回 409。首帧前断开后原归属释放，权限可以修改。 [旧版](../../tmp/ff-f40-control-omlrv0ef/result.json) · [PostgreSQL](../../tmp/ff-f40-control-b9gkt7qx/result.json) |
| Edge 同时运行两个回合 | 两个真实流同时活动，回合 ID 不匹配和跨账户的请求无法停止任何一个。停止选中的会话后保存部分回答，另一会话完成，设置解锁。 [浏览器](../../tmp/ff-f40-control-9z3p4j23/browser.json) · [运行中界面](../../tmp/ff-f40-control-9z3p4j23/active-controls.png) |
| 停止回执阻塞及刷新 | 旧版 2.6 秒后仍持有流。新版约 2.437 秒断开，保留中断正文，刷新后以新输入发起真实模型请求，只提交一个新回答。 [旧浏览器](../../tmp/ff-f40-control-5tqq2m74/result.json) · [最终浏览器及模型记录](../../tmp/ff-f40-control-9z3p4j23/result.json) |

最终 Edge 和 PostgreSQL 各组共 6 次 `deepseek-flash` HTTP 200，4 个完成的回合上报用量。S3 流式停止、断线与续跑兼容回归另有 4 次 HTTP 200。Web 构建通过，定向 lint 返回成功并报告了现有 React 警告。早期验证保留真实 DeepSeek `ConnectError`、临时 API 端口退出过渡，以及将断线检查点误当成正式消息的夹具失败。最终验收按原运行归属、已保存的中断正文、实际送达服务的最后一条用户输入及新回答核验。模拟调用为 0。 [S3 兼容回归](../../tmp/live-deepseek-control-5lfczu2k/live-result.json) · [阶段汇总](../../tmp/FF-F40-conversation-stop-20261121/validation.json)

FF-F39 后续通过关闭及重开 Agent 面板的同步和后台回读验收。N7-01 的部署组合继续核对跨 API 实例的直接取消，跨物理宿主待第二台授权测试机。 [面板恢复](FF-F39-background-conversations.md)
