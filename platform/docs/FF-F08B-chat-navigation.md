# FF-F08B 消息复制与导航

用户消息和助手消息的复制反馈跟随原消息。加载较早历史时保留已选消息和工具的高亮及当前浏览位置。切换会话后清除高亮，并丢弃属于旧会话的迟到复制反馈。用户处于列表底部时继续跟随最新消息，手动浏览较早内容时保留位置。点击“回底部”只滚动消息列表。[聊天面板](../web/src/panels/ChatPanel.tsx)

修改摘要跟随导航工具条显示，选择提案后定位到真实工具卡。窄聊天面板会将操作折行，长目录项或用户消息预览仍可同时与分页、摘要及回底部操作使用。[原审批与实际写入](FF-F08A-approval-diff.md)

专项验收用真实 FF-F08A 会话的 SQLite 和文件存储副本，通过正常登录访问原消息、实际审批结果和已写文件。分页所需的附加内容只有用户笔记。真实 Edge 在此副本上操作系统剪贴板和导航，验收期间的模型调用为零。[验收入口](../server/chat_navigation_browser_acceptance.py)

首轮实际行为检查通过，分页前后同一节点保持在距消息列表顶部 66 像素处。复看截图时发现窄面板仍可横向滚动并裁掉文字，因此限制工具条和摘要的布局宽度，并补充实际控件坐标与水平滚动量的检查。[首轮回执](../../tmp/chat-navigation-f08b-is__pifn/chat-navigation-result.json) · [原审批与文件独立核对](../../tmp/chat-navigation-f08b-is__pifn/independent-navigation-proof.json)

最终 Edge 验收在 340 像素宽的消息面板核验首次加载、分页后和展开摘要后三个状态。滚动内容宽度始终为 340 像素，水平偏移为 0，所有操作均在面板内。实际历史请求完成前禁止发送；加载后定位到最新消息。复制原用户消息和助手正文时实际写入系统剪贴板，Windows 的 CRLF 按换行规范核对。分页后同一目标仍在 66 像素处，高亮和迟到的复制反馈都留在原节点。选中摘要后定位到真实已完成的写入，文件与原已审批文本摘要一致。跨账号获取会话和文件均返回 404。[分页截图](../../tmp/chat-navigation-f08b-xc7zbfzb/chat-navigation-pagination.png) · [摘要截图](../../tmp/chat-navigation-f08b-xc7zbfzb/chat-navigation-approved-summary.png)

正式验收退出码为 0。独立数据库检查确认保留 36 条原历史事件，新增 320 条仅含用户内容的分页笔记，真实和模拟模型调用各 0 次。原始证据文件保持精确摘要，隔离 API 和端口已退出。[正式回执](../../tmp/chat-navigation-f08b-xc7zbfzb/chat-navigation-result.json) · [独立读回与退出](../../tmp/chat-navigation-f08b-xc7zbfzb/independent-navigation-proof.json) · [审查清单](../../tmp/f08b-chat-navigation-source-review-v3-20261119.json)
