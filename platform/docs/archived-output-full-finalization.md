# 已归档执行结果的完整收尾接续

2026-11-10（北京时间）。承接[补传归档](attempt-output-upload-recovery.md)和[原缓存恢复](attempt-output-cache-recovery.md)，本段把具有原始证明的S3执行结果接回原Job/attempt的终态、专业校验、不可变发布及原兼容别名流程。主线仍37/53完成、剩16项；N5-04保持进行中，295项全功能目标不变。

## 接续依据与接口

原runner在根进程及worker子执行已实际结束、原回调与证据步骤返回后，将当时原日志以独占创建、流式摘要及刷盘写入私有run。输出清单包含该文件，`finalizing_source`及唯一`attempt.finalizing_source_ready`事件绑定原Job、attempt、输入、worker、原状态/退出码和日志摘要。缺少该历史证明时不按空日志补造。

`POST /api/v1/jobs/{job_id}:finalize-outputs`要求原attempt与清单摘要。先完成归档补传，再由该入口核验完整归档、必要时冷恢复原输入/run，接续原日志和专业收尾。重复调用读取原终态，不重新执行原构建程序。当前入口用于这些S3早期证明；不能据此宣称FS或所有历史记录的恢复已完成。

原本地日志使用`.vibeflow/jobs/job-N.log`，既有上传器会把SQL读指针切换到`.vibeflow/job-logs/job-N.log`。接续现在区分原日志来源和已验证的远端指针，后者必须有原Job/attempt/存储绑定及摘要匹配的物化记录。原预算到期时不申请日志上传额度，失败结算改为引用本次归档内已核验的原日志；不释放任何未知远端写入的旧预留。

启动恢复曾把有完整早期停止/清单证明的注册报告标记为interrupted并放弃发布预留。现在共享`verify_preparation`检查原证明、累计输出、实际根/子执行停止及唯一事件，保持该原attempt等待明确补交。缺失或无效历史证明继续原有中断/人工核验规则，不给它生成成功或排空凭据。

## 独立专业校验与资源约束

新API以单独的`NativeRequest(kind=job.finalize)`记录自身身份、租约、实际校验子进程及结果，原worker、根进程和原child journal不改写。配置绑定原case/Job/attempt、输入/输出、处理器参数及原资源限制；放行和心跳重新检查原权限、当前attempt、取消及绑定。

报告使用真正的Pandoc解析与独立HTML/DOCX编译校验；LaTeX使用真正的独立编译及PDF/SyncTeX等原校验。它们不重新运行原solver或原文档构建worker。返回值分别说明`native_validation_started`和`solver_execution_started`，不把校验编译说成没有原生调用。

原LaTeX监督命令缺少显式`--user nobody`，被既有内核范围包装在放行前拒绝；实际错误栈定位到`native_execution._prepare`。命令身份已修正，原内核监督及登记校验保留。

接续预算仍从原durable claim时间计算绝对截止时间，恢复已花费的输出及diagnostic累计量，HTTP恢复期限不刷新原native预算。校验取消/超时先停止并排空实际子执行，再等待受保护的调用返回。已经到期、或在后续bundle准备时到期，进入真实资源失败结算，不再为成功发布申请新bundle额度。

只有原日志可核验、校验请求的实际子执行已停止、原输入及清单仍匹配，才形成区别于原worker v1的`vibeflow.job-write-checkpoint.v5`及`attempt.finalizing_recovery_drained`事件。最终提交再次验证该凭据、独立校验结果和原归属，沿用原来源检查、发布/终态/outbox事务及别名修复。恢复API没有授权重跑solver。

新增迁移`a08c65df2197`仅增加`job.finalize`允许类型，前置`f9d4a10735bc`。已有该请求或新排空事件时拒绝回退，避免旧代码丢失证明。仅在隔离临时库执行迁移；开发库未迁移。

## 实际验收范围

本段真实模型0、模拟LLM0。以下为实际OS进程、真实HTTP/SQL/S3和真实专业工具；故障注入转发原操作，使用实际API退出79、真实目录删除和真实SQL提交边界。没有模型回复、流、工具选择、Agent/CLI推理或native结果替身。

最终代表场景及源文件摘要以`tmp/mainline-N5-04-output-finalization-evidence/validation.json`为准：

| 隔离场景 | 证明范围 |
| --- | --- |
| 通用作业SQLite提交前退出、PG提交后退出 | 同一Job/attempt、原日志与实际停止证明保留；重启后成功终态只提交一次，原生程序只调用一次。 |
| 真实报告SQLite提交前退出、PG提交后退出 | 上传中断、整个attempt缓存实际删除、冷恢复、独立Pandoc校验与原HTML/DOCX发布；原发布ID、current及正文回读成立。 |
| 真实LaTeX SQLite提交前退出、PG提交后退出 | 同样的上传/冷缓存/提交边界，实际独立LaTeX编译、原PDF与原发布ID回读成立。 |
| 实际原15秒截止时间到期 | 原程序已成功退出，但接续不续期；提交failed/resource_limit、原截止时间不变，并能读取已归档的原日志。未把资源失败写成成功。 |
| 普通SQLite/PG完成 | 新增日志来源记录保持原成功、完整排空和额度物化流程；没有恢复事件。 |

最终四个文档场景的原构建worker每轮只放行一次；恢复阶段另有报告各4个、LaTeX各1个实际校验子执行。相关HTTP响应明确区分这些调用与原构建重放。最终原截止时间验收只覆盖通用作业，专业校验之后到期的分支未单独实测，不扩大声明。

临时PG还分别核验普通旧Job、attempt、事件、日志和native记录的回退/升级摘要不变，以及存在实际新恢复记录时的回退拒绝。它不是N7-02全部迁移矩阵完成。所有临时库、S3停止记录和观察到的API出生身份/端口另行核验。

失败原件保留：最初四轮通用补交失败，后两轮错误栈明确定位到日志键合法转换，首轮SQLite只读现场也验证同一不匹配；首轮PG仅保存通用拒绝码，不补造其具体异常。首轮注册报告被旧启动中断策略终止；两轮LaTeX探测失败，第二轮原生栈明确定位运行用户检查，第一轮不补造缺失的原生栈；首轮到期处理拒绝上传却未提交失败。另发现并补齐SQL类型约束，但不将它冒充日志键拒绝的实际原因。原失败报告、源码版本、仅类型/栈位置的诊断与修正后结果分别保存。

审查链为`tmp/N5-full-finalization-review-v1..v3-20261110.json`及`tmp/N5-professional-finalization-review-v1..v6-20261110.json`。不同历史入口/生产版本保留各自摘要；只有最终当前源码场景按当前摘要验收，不把早期通过或失败报告改写为新版本证据。

## 原N5-04范围仍需完成

原四条remaining原文保留，并增加逐条兑现映射。本段没有关闭以下要求：

1. 在早期输出/原日志证明形成前的证据生成、同步或校验中断，以及恢复校验本身运行中API退出的处理。活动/未知独立校验记录当前拒绝再分配新native额度；不能把未知消耗当0重编译。
2. 原`native_execution_smoke`两处LaTeX历史物理停止证明缺失的恢复或明确处置。新命令身份修复和新成功算例不能代替原记录证据。
3. 其余已注册处理器、legacy、FS及相关取消/输入改变/并发接续场景的匹配验收；当前代表性报告/PDF成功不等于全部矩阵完成。
4. 历史已提交执行缓存、旧staging/恢复目录及未知/缺失唯一未归档副本的处置，保留原Job/attempt且不自动重算。

源码包：`platform/backups/mainline-N5-04-output-finalization-20261110.zip`；完整源码/AST、原始结果、版本及清理核验见上述validation。主线37/53、资源11/17及全功能295项的原剩余范围继续有效。
