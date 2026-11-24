# FF-F29 工具审批卡与一次性决定

工具审批卡提交时保留原卡片，并禁止同一会话同时提交第二个决定。收到真实的工具结果或拒绝回执后，页面才收起当前审批。遇到回执中断或服务器拒绝，页面读取当前会话的持久化审批状态。服务器仍在等待时可重试，服务器已处理时显示实际工具结果。服务端继续以同一工具调用的待审批状态为条件执行决定。切换会话时，旧会话的对账只影响原选择仍在当前页面的情况。 [浏览器处理](../web/src/panels/ChatPanel.tsx) · [审批路由](../server/app/routers/agent.py)

- 实际 Edge 用真实 DeepSeek 生成三个提案。对批准和拒绝先中断请求发送，再丢弃已落库的实际回执。两种动作双击时各发出一次请求。未送达时两张卡片都可重试，回执丢失时分别恢复为已完成和已拒绝。重复批准和重复拒绝均返回 409。真实文件只发生一次目标写入，被拒绝文件字节不变。已偏离版本的旧提案也可正常拒绝，刷新后可读回批准记录。 [Edge 回执](../../tmp/live-f08a-approval-diff-y6wsjchq/approval-diff-browser.json) · [待审批重试截图](../../tmp/live-f08a-approval-diff-y6wsjchq/new-decision-retry.png) · [恢复终态截图](../../tmp/live-f08a-approval-diff-y6wsjchq/new-decision-reconciled.png)
- 实际非特权 WSL Codex、PostgreSQL 和真实 DeepSeek 验证批准竞争以及在等待审批时取消回合。批准只向原生进程交付一次决定，真实读取原隔离文件。取消时审批过期，迟到回复拒绝，所属执行范围完成清理。安装的 CLI 在第一次隔离运行缺少显式路径，尚未启动模型即返回 503；补上已有的 Linux 程序路径后完整通过。 [原生回执](../../tmp/live-cli-approval-ynu3_mjh/live-result.json) · [数据库回收](../../tmp/cli-approval-postgres-phd08a_a/result.json)

API Agent 有 6 次真实 DeepSeek HTTP 200，其中最终通过组 4 次，首次验收因真实模型在二进制扩展名提案的参数加入控制字符而终止。补充明确的 ASCII 内容要求后通过。原生 CLI 有两个实际回合，其中批准回合有用量回执。模拟调用 0。TypeScript 和 Vite 生产构建通过，Vite 仍有现有的大分包提示。 [阶段验收](../../tmp/FF-F29-one-time-approval-20261121/validation.json) · [真实浏览器入口](../server/live_llm_approval_diff_acceptance.py) · [真实原生入口](../server/live_llm_cli_approval_acceptance.py)
