# 先发生错误、收尾再取消的独立校验恢复

后续原 Job 取消与跨 API 竞争的实际进展见[独立校验取消收尾](validator-job-cancellation.md)；本页历史反例与证据保持。

2026-11-10（北京时间）。承接[HTTP断连恢复](validator-http-cancellation.md)，处理已完成实际专业校验、随后发生OS错误，再在受保护收尾中收到HTTP取消的交错。报告/LaTeX与SQLite/PostgreSQL四种组合、两种v2纯取消兼容和真实模型回读均通过，封存对账见`tmp/mainline-N5-04-mixed-cancellation-evidence/validation.json`。

主线37/53、剩16项，资源11/17、剩6块。N5-04原四条remaining与295项全功能范围保持，不把本段当作整项完成。

## 实际反例和原因

`tmp/professional-finalizing-ad81z4tn/professional-result.json`在真实报告原构建、S3归档、空缓存恢复和独立编译完成后，执行验收注入的sidecar读取；该隔离文件故意缺失，真实open返回FileNotFoundError。随后在实际native scope收尾处暂停；客户端真实断开TCP、观察ASGI operation.cancelling()后解除暂停。没有替代任何编译或模型结果。

原scope保留最先的FileNotFoundError，同时记录收尾收到取消。旧证书却只接受CancelledError，终态事务ValueError回滚，请求仍是active；API实际存活，再次恢复返回409/recovery_required。该记录通过的是反例断言，不是产品成功。原根只启动一次，实际编译/停止不由固定结果替代，模型调用0。

## v2证明和历史兼容

`finalizing_request_cancellation.py`的新证书版本为`vibeflow.finalizer-cancelled-recovery.v2`。同一终态事务明确保存`cancellation.observed=true`和原`primary_error`，row.error与result.error也保留原错误。结束时间、完整请求摘要、原用量与child窗口继续绑定，原记录不改写。

该证明只用于实际cancelled的请求，不能把普通failed、active或仅租约过期当作取消。实际Windows/WSL关闭与额度结算仍由原独立证书核验；没有API退出时不制造退出见证。显式恢复才创建关联的新校验请求，原Job/attempt、输入、publication、截止时间与累计用量保持，原worker/solver不重放。

v1按原UUID事件身份与原限制读取，不接受改换原错误；v1、v2同时出现则拒绝歧义。channel和drained均按所属fence版本读取，历史无fence仍拒绝自动升级。两个真实历史SQLite库`professional-finalizing-t1__4v9v`和`professional-finalizing-bayo39f1`的v1记录在SELECT-only连接中验证通过，更换错误的内存副本被拒绝，数据库字节不变，见`tmp/N5-mixed-legacy-readonly-audit-20261110.json`。这属于协议检查，不是模型验收。

## 当前实际验收

| 场景 | 数据库 | 证据目录后缀 |
|---|---|---|
| 报告实际文件错误后取消、明确接续 | SQLite | n1wutqk1 |
| LaTeX实际文件错误后取消、明确接续 | PostgreSQL | owr40ugf |
| LaTeX实际文件错误后取消、明确接续 | SQLite | zadf_9i6 |
| 报告实际文件错误后取消、明确接续与真实模型回读 | PostgreSQL | w5eig49_ |

目录为`tmp/professional-finalizing-后缀/`，回执`professional-result.json`。每轮旧请求保存FileNotFoundError与实际取消，独立新请求完成原ID的HTML/DOCX/PDF发布；完整旧记录、停止证明、原截止时间与用量不变，重复最终请求幂等。前两轮保留原夹具版本，生产模块相同。

实际模型使用显式`live_llm_mixed_finalizer_acceptance.py --source-profile-id 26 --postgres`入口。真实`api.deepseek.com`、`deepseek-flash`先选择一次read_text_file读取原`E-report/report.md`，再原样回复只存在于该实际源文件中的随机RECOVERED_串；2次HTTP200、2份usage、8550 tokens。真实HTTP/SSE均转发原响应，不提供固定回复。模型事件见同目录`actual-recovered-model-events.json`，上游回执见`upstream.jsonl`，密钥只从本地授权profile读取并保存在隔离加密配置，不输出。

纯取消兼容：请求刚登记处连续两次取消在PostgreSQL的`professional-finalizing-7a30_wcy`通过；WSL计划阶段SQLite的`professional-finalizing-uturlyxi`通过，实际未放行证明绑定新的v2取消fence，未知bootstrap出生仍不推断退出。源码和导入辅助代码审查为`tmp/N5-mixed-cancel-review-v1..v4-20261110.json`，包括实际文件工具、FileRead和读取额度路径。

四种混合错误组合、两种纯取消兼容与修复前反例合计7轮实际原根启动，各一次；修复前反例与v1记录不写回为v2。各API/S3 owner退出、3个临时PG库删除及独立查询确认、源码摘要与Python AST的最终对账见本段validation。开发库没有写入。旧阶段缺失历史原生停止证明的未完成范围继续保留，没有据本轮通过补造。

## 未完成范围

继续原Job取消与多API恢复竞争、diagnostic增长/未知额度耗尽/过期组合、证据生成和同步在证明形成前中断，以及历史缺失停止证明的明确处置、其他处理器/FS/legacy和旧缓存/staging。旧偶发SQLite锁失败的精确持锁者仍未知。53项主线和295项全功能全部目标继续有效。
