# 失败阶段回退的持久意图与恢复去重

## 2026-11-20 锁外准备复验

执行器、传统作业收尾和独立补偿共用 `settle_failure_fallback`。这三个入口先保留已提交的失败终态及回退意图，再在写事务外读取和暂存历史。最终安装时核对原意图、实际进程停止证据、算例修订和文件前像。回执已提交后，文件日志还可短暂处于结清中，验收等待公开读接口恢复，然后核对唯一事件、字典版本和更晚的确认。

本地文件系统与 SQLite、认证 SeaweedFS 与 PostgreSQL 分别通过三个实际 API 退出位置，覆盖文件安装前、文件安装后且 SQL 提交前、SQL 提交后。失败确由 CadQuery 进程产生，恢复保留同一失败作业和时间线，随后成功的 B 确认可保留。两组证据回读共 4 次真实 DeepSeek HTTP 200，模型 `deepseek-flash`，模拟 0 次。认证 SeaweedFS 的首次验收曾在回执已提交但普通读仍返回 409 时停止，调整为等待文件清理完成后，逐一核对读回内容并复验通过。[本地退出恢复](../../tmp/live-cli-startup-k8lprbow/failure-fallback.json) · [认证 S3 退出恢复](../../tmp/live-cli-startup-chyjnafk/failure-fallback.json) · [本地模型回读](../../tmp/live-cli-startup-k8lprbow/live-result.json) · [认证 S3 模型回读](../../tmp/live-cli-startup-chyjnafk/live-result.json) · [远端首轮](../../tmp/live-cli-startup-lymz9p72/live-result.json)

后续状态：2026-11-09，N5-06完整联动与审批/阶段策略已按原条件验收，主线36/53、剩17。见[最终组合验收](engineering-recovery-acceptance.md)。以下保留本子阶段当时的范围、原证据及失败记录。

本批完成N5-06的几何失败回退命名场景，整项仍进行中。主线35/53、剩18，4项进行中、14项待开始；全部295项交付继续。注册工作流/错误门控/其他阶段与真实Agent checkpoint及审批的跨重启联动仍按原N5-06验收。

## 实现

先前作业失败状态提交后才回退，API在空窗退出会遗漏回退；反复调用也会再次写时间线。现在Job失败的同一SQL事务保存workflow.failure_fallback_required，确定性事件ID绑定算例和原Job，保存原attempt、类型、退出码及观察到的工程阶段/修订。适用的旧终态提交入口同样记录意图，不自动给任意历史失败补造原意图。

独立恢复循环只扫描显式现代意图，不通过通用outbox消费者重放业务。处理前仍检查失败Job和attempt归属、原进程停止与写入排空证明；单凭终态、无认领记录或缺失证明不能回退。旧未知历史继续保留供人工核对，不猜测安全。若工程阶段/修订已经变化且本次仍需回退，拒绝自动套用旧决定；较新正式产物继续抑制旧失败回退。

阶段与时间线继续使用已有caseDict文件事务；workflow.failure_fallback_applied与其提交凭证在同一SQL事务落库。SQL回滚时恢复原文件，SQL提交时保留新文件。重复处理返回原决定和replayed=true，不重复回退或追加该Job的时间线；不会重新启动Job。恢复循环逐批最多64个候选，独立于原生作业执行和模型推理；该批次上限不代替N7负载/响应性能验收。

## 实际验收

FS/PG与S3/PG均各使用三个独立算例。先由真实CadQuery成功预览并明确确认到B，随后同一已封存脚本因隔离外部条件真实抛错。验证失败原生进程已停止，再核对并终止实际API进程的Windows出生身份、父链、应用和端口。每个失败原生程序计数始终为一次。

| 终止位置 | 终止前SQL阶段 | 终止前文件阶段 | 回退回执已提交 |
|---|---|---|---|
| 回退开始前 | B | B | 否 |
| 文件已写、SQL未提交 | B | A | 否 |
| SQL提交后、文件事务清理前 | A | A | 是 |

前两种由新API的真实恢复循环补交，第三种保留原提交结果。最终均为A，每个失败Job只有一条workflow.fallback文件时间线。两API并发重复处理不改变修订或时间线；随后实际几何成功并重新确认到B，再处理旧失败也不回退。FS/S3共6个实际失败原生作业、6次真实API终止；不是模拟CLI结果。

缺失排空回执和未认领尝试是单列的自有SQL协议故障，两种后端均拒绝且不改阶段；检查在新API启动前进行，以免已有停止证据恢复服务合法重建v2排空证明。故障字段和ORM时间戳随后恢复。最后另在FS/PG、S3/PG复验旧v1迁移：当前CadQuery STEP体积8、旧历史、显式A/B确认和同键重放继续通过。

证据：FS tmp/live-cli-startup-bv7aa0bm/failure-fallback.json；S3 tmp/live-cli-startup-e5hk5o4d/failure-fallback.json；迁移兼容 tmp/live-cli-startup-sgb_m0hi 与 tmp/live-cli-startup-oc54upvo。

本轮含先行/单项验证共14次真实DeepSeek HTTP200、60464 reported tokens，服务api.deepseek.com，实际响应模型deepseek-flash，模拟0。最终失败回退与迁移兼容各两种后端合计8次，其余6次是先行或单项回读。模型真实读取实际证据JSON摘要，不能据此声称模型自主执行工程操作或审批恢复。11个自建临时PostgreSQL库已删除、6个自建S3服务已停止；开发库仅作为只读凭据来源。

## 先行失败保留

pr10i3x4：新API启动已合法重建v2排空证明，测试却仍期待缺失证明拒绝；将独立拒绝检查置于新API启动前。

jdw_giym、uf38prav：先行S3整轮在after-commit暂停点超时。uf38prav的追加诊断及边界记录证明主API已完成真实回退，SQL为A、回执存在，主API清理已返回；夹具此前错误假定提交者一定是执行者。jdw_giym没有同等诊断，不将相同原因推断当作已证实。私有夹具现对其他API的扫描/投递在业务锁外等待，并仅约束本次故障源Job；最终完整FS/S3复验通过。暂停点明确在SQL提交后、清理前。

djtua9tj：夹具投递等待错误覆盖同算例后续正常预览，导致30秒HTTP超时；改为仅等待所选故障源Job。四轮均模型0、原失败记录保留，不改成成功，也不放宽客户端期限。

## 复现与边界

显式入口platform/server/live_llm_failure_fallback_acceptance.py --source-profile-id 26 --postgres，可加--s3；从server目录使用.venv/Scripts/python.exe -X utf8 -B。默认运行三位置；VIBEFLOW_LIVE_FALLBACK_FOCUS=after-commit仅用于单项诊断，不冒充完整三位置通过。

已审查入口、私有故障代码及导入辅助：live_llm_job_repeat_acceptance、live_llm_cli_startup_acceptance、live_llm_acceptance、testsupport、workflow_migration_smoke（只调用alembic）、s3testsupport、cli_startup_deadline_checks（默认场景未调用）。包装只暂停真实协议边界和记录真实模型请求，不提供模型/原生回复。没有执行历史模拟回归。

保留原N5-06全部remaining；下一步仍需真实Agent checkpoint/审批恢复、错误门控与注册流程的联动，其他失败阶段完整对照不由几何命名场景替代。陈旧工程状态及未知历史的可见人工处置继续N5-07，最终性能与完整环境矩阵继续N7。
