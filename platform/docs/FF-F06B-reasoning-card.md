# FF-F06B 推理过程卡

工作台在收到实际推理片段时展开推理卡。回复结束后，历史消息内的推理卡默认折叠。展开按钮以 `aria-expanded` 报告状态，并用 `aria-controls` 指向对应内容。方向标记对辅助技术隐藏，用户可以用空格或回车操作。[组件](../web/src/panels/ChatPanel.tsx#L165)

推理和正文分别保存在助手消息中。复制操作取正文，刷新则从历史读取同一条助手消息的两个字段。纯正文回复没有推理字段，界面据此省略推理卡。既有 FF-F52 已用 DeepSeek 纯正文的批准续跑核对后一种数据形态，FF-F42 已用真实中断回合核对正文与推理的历史恢复。[纯正文实际回执](../../tmp/live-f52-agenthost-order-e9_pdzso/independent-approval-proof.json) · [真实中断回合](checkpoint-window-and-lifecycle.md)

显式验收入口通过正常工作台发送一次真实 DeepSeek 请求。验收在收到第一条真实推理后暂缓读取后续上游数据，留出时间观察活动状态。浏览器完成键盘检查后继续读取真实流，再对照上游、SSE、剪贴板及持久历史。[验收入口](../server/live_llm_reasoning_card_acceptance.py)

第一次打开工作台时，脚本未先展开默认折叠的会话设置，等待推理强度控件超时。数据库中没有聊天事件，上游请求为零，隔离 API 已退出。验收入口改为通过会话设置选择推理强度后收起设置。[首轮回执](../../tmp/live-f06b-reasoning-card-rqf5xvhg/live-result.json) · [独立退出核对](../../tmp/live-f06b-reasoning-card-rqf5xvhg/independent-exit-proof.json)

正式验收使用 Edge 157 和 D 盘隔离 SQLite/FS。首次显示的真实推理片段可用空格折叠、回车重新展开，随后正常响应完成，推理卡默认折叠。用户只看到一条最终回答，复制得到精确正文。刷新后再次核对相同正文和推理，其他账户读取会话为 404。模型的 61 个推理字符和 15 个正文字符与浏览器及持久历史一致。独立数据库读取确认请求和运行均完成，会话归属为空闲。[活动截图](../../tmp/live-f06b-reasoning-card-ofa15838/reasoning-active.png) · [刷新后截图](../../tmp/live-f06b-reasoning-card-ofa15838/reasoning-after-reload.png) · [独立读回与退出](../../tmp/live-f06b-reasoning-card-ofa15838/independent-cleanup-proof.json)

正式验收退出码为 0。一次 `api.deepseek.com` 的 `deepseek-flash` 请求返回 HTTP 200，实际用量 3,261 tokens，模拟调用为零。隔离 API 退出、端口关闭，临时密钥未落盘。[真实上游与浏览器回执](../../tmp/live-f06b-reasoning-card-ofa15838/live-result.json) · [源码和编译产物清单](../../tmp/f06b-reasoning-card-live-review-v3-20261119.json)
