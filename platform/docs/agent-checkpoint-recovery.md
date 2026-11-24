# 真实Agent checkpoint、退出执行者与会话续聊

2026-11-20，字典提案的文件见证与工具结果共用 SQL 提交。在文件系统与 SQLite、认证 SeaweedFS 与 PostgreSQL 上分别完成提交前后两次实际 API 退出验证。启动先结清文件日志，再恢复会话，保留中断或成功结果及对应的字典修订。恢复期间无需重新执行工具或调用模型。[Agent 字典提案](file-transaction-diagnostics.md#agent-字典提案)

2026-11-18 更新：启动恢复现按会话 ID 分页，覆盖第一批 64 个之后的执行者。另一个 API 接手工具或继续真实模型输出时，启动清理会保留其工作。[多进程启动恢复验收](agent-startup-reconciliation.md)

后续状态：2026-11-09，N5-06完整联动与审批/阶段策略已按原条件验收，主线36/53、剩17。见[最终组合验收](engineering-recovery-acceptance.md)。以下保留本子阶段当时的范围、原证据及失败记录。

本批是N5-06的真实模型checkpoint恢复子阶段，主线35/53、剩18，4进行中、14待开始；全部295项目标继续。待审批工具重启、审批写入提交前后中断、活动新回合期间旧恢复隔离、错误门控与其他工程失败阶段仍须完成，不据本批关闭整项。

## 两个缺口与实现

原先启动对账只把started历史标记interrupted，没有释放已退出API留下的ConversationExecution锁；正式续聊仍会收到recovery_required。模型消息转换也忽略run_checkpoint，已落库的正文片段无法用于同会话续聊。

新增GET /api/v1/conversations/{cid}/recovery、POST /api/v1/conversations/{cid}:recover。请求绑定expected_version和expected_evidence_sha256；校验原生出生身份且必须确认原API/审批/Goal执行者实际退出，不以租约超时推定退出。CLI执行使用原有独立恢复入口。启动时尝试恢复最多64个明确API归属；未知身份、未知原生工具、未停止原生命令和待处理暂存继续拒绝释放。

恢复在算例文件事务边界先对账原文件事务，再处理元数据。回执保留原归属快照、资源ID、版本及摘要、实际退出观察和checkpoint关联，然后将原锁置idle；旧请求按确定性回执幂等返回，不能再次修改当前锁。重放时重新校验原快照与请求摘要。快照保留的是数据库原有控制令牌摘要，不包含原令牌或模型密钥。

历史interrupted标记按算例控制锁串行复核活跃执行者，避免两个启动者重复追加终态。新推理上下文加入明确标注的“系统恢复记录”，仅引用已保存正文末尾最多32KiB，不把未完成片段伪造成完整assistant消息，也不将reasoning当正文。完整原checkpoint保留在原事件历史。

待审批工具不被自动批准。普通新消息在尚有pending工具时返回409，须先批准或拒绝。实现保留安全工具中断回执，并对未知原生执行要求独立处置；这些审批中断分支仍需下一子阶段的真实模型故障验收。

## 真实模型验收

每种后端先实际运行注册CadQuery导出流程，并停在A、等待B确认。真实DeepSeek自行生成MODEL_NOTE_标识并输出说明，标识具体值未在用户请求中提供。私有夹具只在真实正文checkpoint的SQL提交后暂停，不提供或改写模型回复、流式分片或工具选择。读取真实API进程出生身份、父链、应用和端口后终止同一进程。

新API核对原执行者退出、保存唯一恢复回执并释放旧锁。原始assistant消息尚未保存，模型续聊从已恢复正文找回它自己生成的标识；实际出站请求观察确认该标识来自恢复记录。活执行者接管拒绝、旧恢复重放不改变完成后的状态也通过。注册工作流及其步骤保持不变，工程阶段仍为A，只有随后明确确认B才能完成。

| 组合 | 最终证据 | 真实HTTP |
|---|---|---:|
| FS + PostgreSQL | tmp/live-cli-startup-_gbb84it/agent-checkpoint-recovery.json | 4 |
| S3 + PostgreSQL | tmp/live-cli-startup-g4kqczgq/agent-checkpoint-recovery.json | 4 |
| 正常批准/拒绝/陈旧提案，FS + PostgreSQL | tmp/live-deepseek-approval-36ywxbar/live-result.json | 5 |

每个checkpoint组合4次中，2次是真实中断/续聊推理，另2次是独立文件摘要工具回读。正常审批5次由真实模型生成propose_file_update提案并继续；批准前文件不变、拒绝不写、陈旧基线不覆盖、重复审批409且不新增推理均通过。这些正常路径不代表审批中断验收已经完成。

本轮含先行验证和失败共22次真实api.deepseek.com / deepseek-flash HTTP200；其中最终checkpoint两组合8次、先行checkpoint两组合8次、正常审批5次、失败尝试1次。已报告72122tokens，另5个被中断流没有最终usage，不能计作零消耗。模拟LLM调用0。6个临时PG库已删除、2个自建S3服务已停止，API/自有peer退出；开发数据库只读凭据来源、未修改。

先行f1utg72m/z41sg98t已通过checkpoint恢复，归档审查发现回执只有摘要、清锁后缺原归属详情；补原归属快照及重放完整性校验后，最终两组合重新通过，明确核对原PID、资源ID和原摘要绑定。

首轮yn1wd5pa保留为失败：200行长生成任务的正文checkpoint约在HTTP返回头后150秒才出现，超过夹具60秒等待，随后异常处理等待了暂停期限。实际请求1次、最终usage缺失，不能称模型未调用或零消耗。改用短说明任务，保留相同模型自产标识、实际SQL落库、实际API终止和续聊校验，并修正夹具异常路径先释放暂停、取消真实流，不回退到模拟。

## 复现、审查与剩余

从server目录执行.venv/Scripts/python.exe -X utf8 -B live_llm_agent_recovery_acceptance.py --source-profile-id 26 --postgres，可加--s3。复验正常审批使用live_llm_approval_acceptance.py --source-profile-id 26 --modes approve,reject,stale --storage fs --database postgresql。

已审查新入口和私有边界、live_llm_acceptance、live_llm_cli_startup_acceptance、live_llm_job_repeat_acceptance（仅增可选configuration工厂）、live_llm_failure_fallback_acceptance（仅复用真实进程核验/等待）、testsupport、workflow_migration_smoke（只调用alembic）、s3testsupport、cli_startup_deadline_checks（默认场景未执行）。正常审批入口与其S3观察器也已审查；本批普通审批仅FS。未运行历史模拟回归，未用固定模型回复或模拟CLI推理。

下一组严格保留原要求：待审批提案跨重启继续明确审批；批准领取/文件写入/工具结果提交各处实际中断不重放；原恢复请求不能释放新活动回合；真实Agent不能通过文件提案绕过workflowState/错误门控；失败阶段/注册工作流与文件时间线继续联动。其他provider、完整16KiB及更大正文/历史窗口矩阵继续FF-F42与全功能最终验收。
