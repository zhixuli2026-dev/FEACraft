# FF-F13 Codex 容量退避与续跑

原生 CLI 确认回合因模型容量失败后，平台在同一线程按 5、10、20、40、60 秒的安排退避，每次可加最多 0.5 秒抖动。后续请求只提交短续跑指令，并使用独立消息和回合编号。原用户消息仍在原线程。检测到工具执行或审批活动时，容量失败后直接结束，界面提示核对已有记录。 [续跑策略](../server/app/services/codex_capacity.py) · [原生回合执行](../server/app/services/codex_turn.py)

Agent 运行记录显示当前容量等待及续跑次数，停止操作在退避期间继续可用。初始化、等待、下一次尝试、原生终止和关闭回执沿用相同的数据库记录。页面重载可以重新接收该回合的事件。 [原生运行记录](../server/app/services/codex_turn_records.py) · [界面](../web/src/panels/NativeChatPanel.tsx)

## 实测

- 实际 Windows Codex `0.171.0-alpha.13` 接入有一个并发槽的隔离 Responses 网关。由真实 DeepSeek 请求占用该槽，实际拒绝原生请求时返回容量 503。放开后同一线程继续，从容量拒绝到新请求实际相隔约 5.44 秒，原用户请求的登记仅一次，续跑后的真实回复含核验串。两轮分别记录 5 次、6 次 DeepSeek `deepseek-flash` HTTP 200，总计 11 次。两轮模拟 0 次。 [协议验收](../../tmp/live-f13-capacity-cra_f6mm/capacity-result.json) · [最终验收](../../tmp/live-f13-capacity-xa9_fsnf/capacity-result.json)
- 实际原生命令创建测试标记后，后续请求遇到容量拒绝。运行确认只有一个成功命令，平台结束原回合并保留核对信息。退避时主动取消成功，只有第一个原生尝试，进程收尾经过独立验证。 [最终结果](../../tmp/live-f13-capacity-xa9_fsnf/capacity-result.json)
- Edge 在 1440px 和 390px 读取真实退避事件。390px 下从实际界面点击停止，原生回合已取消，等待提示消失。隔离 PostgreSQL 已清理。前端构建通过，现行开发部署继续使用既有镜像。 [浏览器结果](../../tmp/live-f13-capacity-xa9_fsnf/capacity-browser.json) · [桌面](../../tmp/live-f13-capacity-xa9_fsnf/capacity-desktop.png) · [手机](../../tmp/live-f13-capacity-xa9_fsnf/capacity-mobile.png) · [数据库清理](../../tmp/terminal-agent-postgres-17e_febu/cleanup.json)
