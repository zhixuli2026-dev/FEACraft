# 独立校验准备阶段中断的恢复

2026-11-10（北京时间）。本段承接[运行中API退出恢复](validator-owner-recovery.md)，处理独立校验child已经登记、业务进程尚未放行时的API退出。状态：Windows五处及WSL九处准备边界的SQLite/PostgreSQL矩阵28/28已通过；已封存此范围，未将它等同于整个N5-04完成。主线37/53、剩16项；N5-04及全功能295项目标保持原范围。

## 登记与启动的空档

真实反例`tmp/professional-finalizing-fzdngkxu/professional-result.json`在实际child登记提交后暂停，再执行API退出79。旧请求没有完整启动或用量证明，新API拒绝接续，原Job/attempt保留。夹具转发了真实报告构建、S3上传及SQL登记，没有模型或编译结果替身。

新`finalizing_native_preparation.py`把启动契约、实现摘要和读取额度，与child登记放在同一事务。独立校验必须先提交范围计划，才允许创建分配助手或守护进程；Windows业务启动须先提交guardian证明，WSL业务放行还须提交同出生预检。普通Agent/native请求保持原来的UPDATE优先登记路径，准备契约仅适用于`job.finalize`。

旧API实际退出后，恢复区分两种情况：

- child已经登记实际进程身份：沿用原有Windows成员关闭、Linux内核范围回收及实际停止检查。
- child没有登记实际进程身份：必须有该版本启动契约，核验业务未获放行；对已知助手出生逐项观察真实退出，对已分配或准备中的内核范围执行实际retire或abandon。缺少完整出生记录的固定只读bootstrap保持“未观察到”，不推断其退出，不控制未知PID，也不补造旧stop。

准备结算证书绑定原请求、child、启动契约和完整范围历史。原worker、根进程、输入及成果来源不改写。来源校验准入会核验这个证书，不能只凭phase或租约过期排除旧写者。

## 输出额度与历史兼容

child登记同时持久保存4096字节读取窗口，消除登记提交与下一次用量写入之间的空档。只有确切“业务未放行”证书才允许结算未使用的窗口；已有累计观察量和历史未结算量不能倒退。

运行中API退出的未知读取仍保留为`unsettled_output_bytes`，不算成观察到的字节。未放行时的零新增量有独立证明，不是把未知用量猜成零。diagnostic发生与未放行证明冲突的增长时拒绝结算。

旧协议缺少启动契约时保持拒绝自动接管。`tmp/N5-preparation-legacy-proof-audit-20261110.json`用两个真实隔离SQLite库做只读核验：历史缺契约记录拒绝升级，新证书能核验，旧child身份/stop和数据库记录均保持不变。这属于协议检查，不是模型验收。

## 验收入口与保留的失败

`attempt_professional_finalizing_acceptance.py --handler report|latex --terminal-boundary before --preparation-boundary STAGE [--postgres]`在真实登记或准备步骤提交后暂停，执行API退出并重新接续，检查原Job/attempt、截止时间、原发布ID与实际HTML/DOCX/PDF内容。原构建worker只放行一次。Windows覆盖reserved、plan、guardian、created、host_ready；WSL另外覆盖prepared、allocated、admitted、native_ready。

`attempt_preparation_matrix_acceptance.py`只运行源摘要已审查的上述实际入口，最多并行三个隔离算例。遇到真实失败后停止新场景准入，并等待已启动进程收尾。目标28个部署/边界组合；仅复用源摘要与当前完全一致的已有通过记录，额外调试轮次不计成新功能或主线任务。

原始SQLite锁失败保留在`tmp/professional-finalizing-_9cen3p_/professional-result.json`：旧准备记录已回收，随后真实LaTeX编译也实际停止，但NativeRequest终态UPDATE返回database is locked。精确持锁者尚未记录；后续重验未复现，不能据此声称已修复锁问题。

`attempt_finalizer_post_failure_acceptance.py`直接恢复这份失败记录：只恢复已停止的自建S3，在原endpoint/bucket下核验原数据不变。原截止时间已经实际过去，新API按原Job/attempt提交failed/resource_limit，不启动新原生校验或solver。原失败报告字节不变，旧API日志及result另存原件，旧新请求关系和停止证明保留。证据为同目录`post-failure-recovery-result.json`。

调试期间一次修改误将登记专用变量放进heartbeat，导致后续三轮NameError/ChildExecutionError。已按函数上下文修正并检查变量引用，修正后的plan/prepared重验通过；这些失败与原SQLite锁失败分开保存。事务诊断只记录语句类别、表名和连接生命周期，不保存SQL参数、环境变量或凭据。

当前生产代码的真实模型兼容性单独运行`live_llm_native_shell_deadline_acceptance.py --source-profile-id 26`，实际服务`api.deepseek.com`、模型`deepseek-flash`，2次chat-completions、2份usage回执、9727 tokens。模型选择真实Shell工具、命令按原期限超时并实际停止、模型随后报告结果。证据为`tmp/live-shell-deadline-sjz_7ljb/live-result.json`及同目录`independent-model-and-exit-receipt.json`。模拟LLM调用0，开发数据库只读。

源审查为`tmp/N5-validator-preparation-review-v1..v9-20261110.json`。最终矩阵为`tmp/validator-preparation-matrix-z3hitd8m/matrix-result.json`，28/28通过；复用21个生产代码及辅助代码完全一致、夹具历史版本保留的通过场景，另7轮使用修正退出核验的夹具。初轮矩阵及所有失败均保留，完整v7审查源码在`tmp/N5-preparation-reviewed-source-v7-20261110`。

一次清理时的历史API身份重复查询返回mismatch，finally异常又遮盖了原始错误；旧报告仍是失败，不能据后来的通过断言其原功能已成功，也没有记录到当时完整身份差异。后续独立核验观察到三个API确已退出，原S3 owner也已退出、端口已关闭。新夹具在API存活时保留查询/等待句柄，按同一实际进程对象核验退出并保存见证，确保清理失败仍落盘；这不等于产品已交付任意历史身份不匹配的自动恢复。

源码及完整证据校验见`tmp/mainline-N5-04-validator-preparation-evidence/validation.json`；源码包为`platform/backups/mainline-N5-04-validator-preparation-20261110.zip`。

## 保留的原始剩余范围

恢复HTTP取消、连续多次独立恢复中断、实际非零diagnostic增长及未结算额度耗尽、专业校验之后的期限组合仍须验收；当前过期失败记录恢复不代替全部组合。原证明形成前的证据生成/同步中断、原两处LaTeX历史停止证明或明确处置、其他注册处理器/legacy/FS及并发/输入变化、旧缓存/staging和唯一未归档副本的处置继续。N5-04原四条remaining、其余主线及295项全功能目标不删减。
