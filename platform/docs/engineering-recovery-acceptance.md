# N5-06 工程失败与真实Agent恢复验收

2026-11-09按原条件“失败只回退不推进；恢复不绕开确认、错误门控或重复执行审批工具”关闭N5-06。原三条remaining逐条保存在tmp/mainline-N5-06-closure-evidence/original-requirements-audit.json。主线36/53完成、剩17项，3项进行中、14项待开始；全部53项和295项全功能目标继续。295项最终完成标记仍为0，不表示所有功能都未实现。

## 本次修复

真实模型在一条assistant消息中提出多个危险工具时，原审批接口批准第一个后就继续调用模型，其他提案尚无tool_result；目标执行入口也可能在待审批时先追加用户续轮消息。现于普通模型回合和目标续轮的入口检查pending提案，等待剩余审批，不新增推理或续轮消息。改动仅涉及agent.py的_run_loop、_goal_run_loop两个函数。其余十个阶段/迁移/checkpoint/文件事务核心模块与已归档验收版本逐字节相同。

## 完整真实工程链

两种后端均先实际运行CadQuery预览并明确确认B，再运行已知v1旧mesh流程的真实Python argv，一次exit23。显式迁移创建新注册流程、正式回A；本次geometry.export实际执行含故障的输入并失败，停在A，每个失败Job保留唯一文件时间线与实际停止/排空凭据。

真实DeepSeek先用read_text_file读取失败脚本，再自行生成MODEL_NOTE标识和分析。夹具仅在真实正文checkpoint提交后暂停；核验并终止本次API实际进程。新API恢复原会话，模型从恢复正文复述它自行产生的标识，并实际调用propose_text_replacement提出最小修复。文件在明确审批前不变，批准后写入；重复批准409不重放。

按当前修复输入显式recompute创建第三代运行，保留v1旧运行→迁移后失败运行→重算新运行的来源链和旧运行全部原始历史。实际导出STEP体积为0.9999999999999998，误差标准1e-9；旧argv一次、故障注册脚本一次、修复后注册脚本一次，没有偷偷重跑旧Job。

FS下模型拒绝受保护阶段提案；S3下模型实际提出阶段修改，批准时共享workflowState硬门禁拒绝。两者的专用普通字典保存接口也拒绝阶段绕过。新运行仍停A等待B；加入真实阻断错误、刷新确认凭据后，明确B确认因“未结清/阻断错误”被拒绝，不能把陈旧版本409当作错误门控通过。只有显式核验错误、再次刷新并明确确认B才完成。重放旧迁移、重算和Agent恢复请求不再改变B、旧历史或时间线。

## 审批中断与多提案

上一轮尚未归档的真实提案三边界，FS/PG与S3/PG均通过：待审批API退出后提案仍须明确批准；批准已写文件但SQL未提交时实际终止API，重启回滚文件、将旧工具标中断并拒绝旧批准，新提案需重新明确审批；SQL已提交而清理未完时实际终止API，重启保留唯一结果。旧恢复请求不能释放新活动回合。两组合共18次真实调用，其中14次提案/重新提案/续聊、4次独立回读。

新增双提案重启也在两种后端通过。目标入口在pending时不追加用户消息、不调用模型；批准第一个只应用第一个文件，结束于awaiting_approval，不调用模型；批准第二个才真实续聊一次。两个已批准工具都拒绝重复批准，两个工具结果各一条。

## 阶段策略与证据类型

7类Job为geometry、mesh、solve、postprocess、convert、report、udf。每个后端各14个实际Python故障进程，覆盖初始A与明确开发态V：A不会因失败被推进，V只回到对应更早阶段，目标V则保持V；每个进程真实一次exit29、真实出生/停止/排空凭据、唯一文件时间线和重复回执不变。

这28项是带真实进程和存储的类型/阶段故障协议验收，不冒充28次真实CAE求解；V也不代表工程证据已齐。真实CAE链由上述两轮CadQuery及既有几何失败三处API终止证据支持。其他求解器完整中断矩阵继续原N7-01/N7-03，资源与关闭继续N4-05/N4-07，人工处置继续N5-07，界面继续N6；未移除任何原53/295要求。

| 组合 | 完整工程联动 | 双提案重启 | 14项阶段策略 | 三处审批边界 |
|---|---|---|---|---|
| FS + PostgreSQL | g3_gonfx（7次真实调用） | osv3w3j5（4次） | pzb33n_p（2次独立回读） | 4y18f3lm（9次） |
| S3 + PostgreSQL | _9dy1vx5（8次真实调用） | 193n_g5j（4次） | 8zj_2wc9（2次独立回读） | 60nap3g0（9次） |

各缩写的完整目录为tmp/live-cli-startup-加缩写，内含对应场景JSON、真实SSE、upstream.jsonl和live-result.json。三处审批的范围标签是当轮快照；完整工程与阶段策略由后续独立证据组合补齐，不改写旧报告。

本次合并归档58次真实api.deepseek.com / deepseek-flash HTTP200，含42次提案/上下文/分析/修复/续聊及失败请求、16次独立证据回读；已报告271341tokens，5个中断流无最终usage，不能记为零消耗。上轮审批18次加本轮40次，模拟LLM调用0。13个临时PG库均已删除、4个自建S3服务已停止；只读进程复核无本批测试API/驱动残留。开发库只读取授权profile26凭据，未修改。

## 保留的失败

4xcvbto_是上一轮夹具语法失败，0次模型。9803w8ju误用旧命令reprepare可用性当作注册处理器排空条件，0次模型；改为直接校验真实排空凭据。ni_gi7wp已真实修复并重算，但断言只认中文而未认confirmation_required错误码，4次模型。bdfwbo8_已真实修复并重算，但生成阶段测试的单字符替换不唯一，4次模型；改为模型接收完整合法字典。ezw_i4ff把禁止普通file写字典的400误当作共享阶段门禁409，5次模型；改用正式caseDict保存接口。原失败保持失败，不放宽工程断言、HTTP期限或模型条件。

## 复现与审查

从platform/server目录使用.venv/Scripts/python.exe -X utf8 -B，入口为live_llm_engineering_recovery_acceptance.py、live_llm_pending_approval_acceptance.py、live_llm_failure_stage_policy_acceptance.py及live_llm_agent_approval_recovery_acceptance.py；均传--source-profile-id 26 --postgres，S3另加--s3。这些是显式付费真实模型入口，阶段策略的核心不调用模型、仅包装的证据回读调用。

已审查新入口及导入的live_llm_agent_recovery_acceptance、live_llm_workflow_plan_migration_acceptance、live_llm_failure_fallback_acceptance、live_llm_job_repeat_acceptance、live_llm_cli_startup_acceptance、live_llm_acceptance、testsupport、s3testsupport、workflow_migration_smoke（只使用alembic）、cli_startup_deadline_checks（默认场景不执行）。包装只观察/暂停真实提交、读取实际数据或设置初始输入，未替换模型回复、流分片、工具选择或CLI/原生推理。没有执行历史模拟回归。

完整源码包为platform/backups/mainline-N5-06-closure-20261109.zip，SHA及全量Python AST核验见tmp/mainline-N5-06-closure-evidence/validation.json。FF-F42完整750ms/16KiB/历史窗口/provider验收仍保留，未凭本主线关闭全功能项。下一项N3-07保留MPI/批次/UDF原完整桥接与原S3 UDF单次409归因要求。
