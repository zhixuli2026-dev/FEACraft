# FF-F51 Plan 只读、批准与恢复写入

Plan 模式只向模型提供读取及计划所需工具。API 也在派发前检查实际工具调用。拒绝的整组调用不会新建工具记录，不消耗目标工具预算，也不会作为等待回填结果的模型消息写入会话历史。模型读入旧失败回合时会过滤当时被拒绝的整组调用。原证据仍可读取，已完成的合法工具及结果继续配对保留。[API 派发](../server/app/routers/agent.py) · [旧历史投影](../server/app/services/agent_recovery.py)

修改权限时，服务端在既有算例控制锁内重新检查授权、持久执行归属和待处理工具。因此，即使从另一个 API 进程提交，正在运行或存在待批准写入的会话也会拒绝修改。工作台「批准 Plan」只切到逐次审批模式，写文件需再点击该提案的「批准执行」。[会话权限接口](../server/app/routers/agent.py#L427) · [工作台](../web/src/panels/ChatPanel.tsx)

## 真实 DeepSeek 与 Edge

- 原代码对照使用一次真实 DeepSeek HTTP 200。Plan 的生产工具清单未提供写工具。隔离夹具使用 DeepSeek 的命名工具选择生成真实 `propose_file_update` 回包，服务端拒绝执行，文件字节保持，但持久消息中留下了没有结果的调用。另一个 API 进程在该活动回合接受了权限切换。对照原外层统计在 `[DONE]` 后等待记录，未生成流统计；只读核对原 SQLite 确认真实工具名、失败终态、工具记录 0 和已经改变的许可。两个 API 进程均已退出。[旧版上游记录](../../tmp/live-plan-permission-x08vnl3p/upstream.jsonl) · [旧版实际状态](../../tmp/live-plan-permission-x08vnl3p/baseline-readonly-observation.json) · [DeepSeek Tool Choice](https://api-docs.deepseek.com/api/create-chat-completion/#request)
- 最终验收共六次真实 HTTP 200。隔离夹具保留生产工具清单观测，只在两个权限负例向官方服务分别指定 `propose_file_update` 和 `update_memory`，原回包逐行交给 API。两个工具均被拒绝，文件和记忆保持原字节，会话仍正常执行真实 `read_text_file`。并行 API 实时切换权限返回 409，待审批时尝试切换也返回 409。异账户和工作区只读成员返回 404。[最终回执](../../tmp/live-plan-permission-rqbuyrp3/live-plan-permission-result.json) · [官方上游记录](../../tmp/live-plan-permission-rqbuyrp3/upstream.jsonl)
- Edge 在原会话点击「批准 Plan」后，真实模型第一次提出写入但原字节保持。点击「批准执行」后，工作台收到实际成功工具回执，文件内容和请求完全一致。原 API 与并行 API 的出生身份已确认退出，端口关闭。夹具仅在进程内存中保存隔离加密配置的解密密钥。[批准 Plan](../../tmp/live-plan-permission-rqbuyrp3/plan-resumed.png) · [批准写入后的工作台](../../tmp/live-plan-permission-rqbuyrp3/plan-write-applied.png) · [并行 API 退出回执](../../tmp/live-plan-permission-rqbuyrp3/peer-exited.json)
- 从旧对照复制 SQLite 和文件，使用真实 DeepSeek 继续同一历史会话。将历史过滤进一步限定为权限失败的原整组调用后，再次复制原库复验。两轮各收到两次 HTTP 200。正式会话仍能读回旧拒绝记录，上游仅收到合法配对工具及结果，实际读回随机文件内容，旧数据库摘要保持。[初次旧历史复验](../../tmp/live-plan-permission-legacy-g5z_vlmn/live-plan-permission-result.json) · [最终旧历史复验](../../tmp/live-plan-permission-legacy-j7ds56jt/live-plan-permission-result.json)

本轮合计十一次真实 HTTP 200。完整验收六次，两轮旧历史复验各两次，这十次合计 49,971 tokens。旧版对照未捕获用量。两次独立夹具启动问题分别是初始文件保存临时 409 和并行 API 就绪等待过短，均发生在向模型派发请求前。模拟调用 0 次。[显式真实验收入口](../server/live_llm_plan_permission_acceptance.py)
