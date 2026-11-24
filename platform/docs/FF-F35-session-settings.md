# 独立会话与设置保存

Agent 的「保存此会话」将模型配置、模型名和权限模式一起保存到当前会话。表单用同一套控件创建新会话，操作结果由「已保存当前会话设置」反馈。后端按算例和会话核验授权，并在修改前检查正在运行的回合和待审批工具。越权请求或不可访问的模型配置返回 404，运行中返回 409。[API 契约](api.md) · [实现](../server/app/routers/agent.py)

## 2026-11-22 实测

- 旧版在 Edge 修改已有会话的模型配置后，刷新会恢复旧值。窄面板的下拉框当时只剩箭头。新版在 1450 和 960 像素窗口中可见配置名称和保存操作，按 Enter 保存后能看到结果反馈。Impeccable 对改动文件的机械检查返回空发现列表。[旧版回执](../../tmp/live-f35-session-settings-abw8zziu/settings-result.json) · [新版窄面板](../../tmp/live-f35-session-settings-ntzq4scp/settings-saved-narrow.png)
- 两条独立会话分别保存并恢复 provider、模型与权限。隔离 API 实际退出并重启后，界面与数据库仍读回同一配置。两条会话各发起一次真实 DeepSeek 请求。计划模式使用 Messages 协议、`deepseek-v4-flash` 兼容模型名和只读工具，问答模式使用 Chat Completions、`deepseek-flash` 且工具数为 0。两次均返回 200，正文只保留在发起会话。模型名测试使用官方兼容别名。[真实设置回执](../../tmp/live-f35-session-settings-ntzq4scp/settings-result.json) · [DeepSeek 模型说明](https://api-docs.deepseek.com/zh-cn/api_samples/chat_curl/)
- 实际模型回合运行中更新设置返回 409。跨账号保存和引用另一账号的模型配置均返回 404。超长模型名返回 422，原配置保持。初次自动化因带候选项的模型输入框被错误按 `textbox` 定位而超时，修正选择器后复跑通过。初次失败发生在模型调用之前。[首次自动化回执](../../tmp/live-f35-session-settings-o89kswlf/settings-result.json)

真实计划与目标复验中，DeepSeek 发出合法三态计划与目标第 1 版。API 重启后仍可读回。非法更新被拒绝，原计划和目标保持，下一轮提交目标第 2 版并完成。切换独立会话时计划和目标为空，切回恢复。设置验收与本次计划目标回归合计 8 次真实 DeepSeek 调用，模拟 0。会话摘要和交接的真实压缩、刷新与续聊沿用独立回执。[计划与目标复验](../../tmp/live-plan-goal-nlxgzifb/live-plan-goal-result.json) · [摘要验收](FF-F37-context-compression.md) · [阶段汇总](../../tmp/FF-F35-session-settings-20261122/validation.json)

构建前后的 C 盘可用空间为 17,454,682,112 B 和 17,439,854,592 B。09:00 Docker 维护内部回收 0 B，维护当时 Windows 可用空间相同。两套服务健康检查均为 200，下次维护为 15:00。[磁盘和服务记录](../../tmp/FF-F35-session-settings-20261122/disk.json)
