# 作业逐项对账与持久处理结论

状态：本批专项与兼容验收完成。N5-02 仍进行中，主线 11 完成、19 进行中、23 待开始，剩 42 项；全功能 295 项的完整验收范围不变。

原启动对账仅返回分类计数：未投递、未知所有者或待收尾的任务在 API 重启后没有持久的处理结论。现在逐项检查后保存 `job.recovery_required` 事件及同事务 outbox，并在已鉴权的 Job 详情中增加 `recovery`。该字段说明观察时间、原因和下一处理环节，不授予重放、接管、停止或发布权限。

| 记录情况 | 处理分类 | 本批行为 |
| --- | --- | --- |
| 旧 Job 没有 attempt | legacy_unverified | 保留记录，提示核对旧历史 |
| 活动尝试链不匹配 | lineage_unverified | 保留记录，提示人工核对 |
| queued 且尚未 claim | unclaimed_delivery | 标记需要投递对账，不直接运行 |
| 旧/损坏 owner、其他主机、出生不匹配或暂不可核验 | owner_unverified | 要求核验原实例，租约超时不等于已退出 |
| 原实例确切退出，处于 finalizing | publication_recovery_required | 保留产物和发布记录，等待收尾恢复 |
| 原实例确切退出但登记缺失 | registration_recovery_required | 保留不确定性，交给启动登记恢复 |
| 原实例确切退出但阶段不满足自动中断条件 | state_recovery_required | 保留状态，人工核对 |
| 原实例仍匹配 | owner_live | 不改 Job/attempt，不启动第二个执行者 |

观察绑定 case、Job、attempt、活动 head 的完整字段摘要，包括输入、输出、取消、心跳与租约。原始 claim 等私有字段只参与摘要，不写入事件或公开投影。重复和多实例检查在同一 case 控制锁下去重；输入或状态变化后，旧结论不再作为当前提示返回，历史事件仍保存。

暂时无法读取 OS 身份也会保留待核验结论。后续精确核验原实例仍存活且记录未变时，用 `job.recovery_cleared` 审计解除旧提示；不修改 Job/attempt，不删除原观察，也不在每次检查时追加重复解除事件。

操作系统身份核验在事务外进行。回到短事务后重新读取并比较完整摘要；期间发生取消、心跳、重试链或产物变化时，返回 `recovery_changed`，不写入旧观察得出的结论。已退出的 claimed/running 仍按原协议记录 interrupted 和 owner-exited 审计，不伪造实际停止回执。

启动登记窗口和终态排空继续使用各自已有协议。批次的 queued 受并发额度约束，工作流的 queued 受依赖和确认约束，旧直接入口还可能没有完整执行规格。因此本批的 `delivery_reconciliation` 不是“已经安全重新入队”。后续需要持久投递意图、调度约束与启动确认一起接通；不能用本批分类代替队列或 finalizing 恢复。

验收入口：`platform/server/job_reconciliation_notice_smoke.py --postgres`。边界阶段为明确标注的数据库夹具；原实例出生及退出使用真实 OS 观察。测试覆盖双 API、重复对账、同事务 outbox、旧结论失效、未来租约不覆盖退出证据、核验阻塞期间心跳/取消仍可提交，以及跨用户读取拒绝。真实 Windows/WSL payload 的重启兼容由 `job_owner_startup_smoke.py --postgres` 补充。

未迁移开发数据库，未重载端口 8000，未进行正式 OpenAI 登录联调。自动投递、finalizing 产物补交和人工恢复操作仍待实现。


本批结果：N5-02逐项对账结论已持久化：旧/未知owner、未投递、缺登记和待发布收尾保存绑定完整状态的审计及outbox，Job详情显示当前原因；锁外身份观察后复验，取消/续租不阻塞，重复结论去重，暂时核验失败解除后保留审计。专项双库各17、真实Windows/WSL所有者兼容各21及真实API重启通过。安全投递意图、批次/工作流约束、终态缺证与finalizing实际补交继续；主线11完成/19进行中/23待开始，剩42，全功能295严格验收仍0。

规范证据：`tmp/mainline-N5-reconciliation-decisions-evidence/`。先前专项各15项及第一轮所有者各21项与最终结果重叠，不累加。源码953文件/Python571；app249/路由227/边界350；注册套件all277/offline105/writes30。未运行全量offline/writes或前端回归；未恢复新归档重跑。迁移head6a4fc1b5e087、36业务表未变。
