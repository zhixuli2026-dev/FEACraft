# 首次完整收尾前取消与版本化恢复协议

2026-11-10（北京时间）。承接[收尾IO归属](finalizing-io-ownership.md)，处理输出已归档、尚未开始完整收尾且没有历史IO归属时的取消。新记录可按确切协议完成取消；旧无证明记录和归档仍pending的取消分别保留明确诊断。本阶段不把这两种诊断当作已完成取消。

主线仍为 **37/53，剩16项**；资源预算11/17、剩6块。N5-04原四条remaining和295项全功能目标不变。证据与源码核验见`tmp/mainline-N5-04-versioned-protocol-evidence/validation.json`。

## 版本隔离

原worker在上传前写入`vibeflow.attempt-output-preparation.v2`，带`recovery_protocol=vibeflow.finalizing-io.v1`。实际停止、原输入、worker、child、输出清单和原已用信用仍逐项绑定，原始PREPARED事件必须唯一且内容完全一致。

显式归档后使用`vibeflow.attempt-output-recovery.v3`，再绑定该原准备证明摘要与相同IO协议。两个入口都必须升级：旧准备读取器会拒绝v2；旧版对已归档记录会跳过准备检查，因此归档回执也必须用旧归档读取器不接受的v3。旧API无法先下载缓存再发现协议不支持。

旧v1准备证明继续按原格式验证，产生v2归档回执；原v1/v2归档历史不改写为v3。新版原生校验和IO归属摘要仅对新准备证明增加绑定，既有legacy v5完整收尾历史的原摘要保持。

| 原准备证明 | 归档回执 | 无历史IO时取消 |
|---|---|---|
| v2，声明当前IO协议 | v3，绑定原准备摘要 | 原worker停止与归档均核验后完成取消 |
| v1 | 原v1/v2 | 需要实际已登记IO结束证明；缺少时人工核查 |
| v2 | 尚无归档完成回执 | 保留取消中的状态，核验补传操作后再收尾，当前未实现此取消闭环 |

新旧格式不能混用。未支持的协议、与原捕获事件不符的降级、准备和归档版本错配都会拒绝。归档前和归档后的读取均核验原准备及停止历史，不凭对象前缀存在判定成功。

## 已归档记录的首次取消

在真实原构建、上传中断、原API实际退出、明确归档和整个执行缓存丢失后，正式取消原Job；此时没有NativeRequest，也没有IO操作历史。新协议证明后续恢复必须先登记IO归属，恢复API按原冻结绑定取得排他操作。

取消专用NativeRequest实际记录为failed/OriginalJobCancelled，child为空、prepared为空，native_execution_started=false。保留原Job/attempt、原已用信用和绝对截止时间，v5引用原实际stop，发布预留失败，当前发布指针不变。执行缓存继续不存在，没有新增编译器或求解器执行。重复请求及第二个实际API得到相同取消终态。

这不是模型、CLI推理或专业校验的固定成功结果；该操作只记录实际发生的取消收尾，校验成功与产物发布均未发生。

## 旧记录和未归档记录

旧准备/归档格式、首次完整收尾前已取消且无可信IO历史的记录，返回409/legacy_finalizing_io_unverified。保存与原Case/Job/attempt/head快照绑定的`finalizing_io_unverified`恢复通知，next_step为manual_review。重复请求不创建NativeRequest、不升级旧格式，也不会由一次被拒绝的请求自行产生授权；相同快照的通知event_id保持。

人工核查应保留原数据与未知容量，列明旧API、原worker和已知IO/原生作用域，取得实际退出或实际请求结束证据，或采取明确的隔离/放弃处置。不能通过重写证明版本、清空原stop、仅等待租约过期或自动重算解决。完整人工处置入口和旧缺失停止证明仍在原未完成范围；本阶段交付诊断与保留行为。

任务在输出归档完成前取消时，GET输出恢复返回requires_attention/cancelled_output_archive_pending，完整收尾入口返回409/cancelled_archive_pending。attempt仍为cancelling，write_checkpoint为空；实际数据、原S3 pending状态与容量保留。该情况不再被提示为可直接恢复上传，也不被算作取消已完成。下一步先实现实际补传请求结束/归属退出核验后的取消闭环。

## 实际验收

目录前缀为`tmp/professional-finalizing-`、回执`professional-result.json`；独立缓存使用`tmp/attempt-output-recovery-`、回执`output-result.json`。

| 场景 | 数据库 | 后缀 | 结果类别 |
|---|---|---|---|
| 原实现首次完整收尾前取消停在cancelling | SQLite | lmhnwz8d | 修复前反例，保留原状态 |
| 报告新协议、归档后无历史IO取消 | SQLite | 4g1q9q00 | 取消完成 |
| LaTeX新协议、归档后无历史IO取消 | PostgreSQL | v_5zxdpe | 取消完成 |
| 实际原生/S3使用旧证明格式、拒绝无证明取消 | PostgreSQL | b997ptwy | 人工核查诊断通过，取消未完成 |
| 原上传未完成时取消 | SQLite | 4c3p4w0i | pending诊断通过，取消未完成 |
| 新协议独立缓存、双API竞争与损坏拒绝 | PostgreSQL | f4jdkefd | 兼容通过 |
| 新协议实际错误/HTTP断连、双API接续与真实模型 | PostgreSQL | 9601q7k6 | 兼容通过 |

旧格式场景只选择兼容的证明格式，真实编译、S3、实际退出和停止证据原样转发，没有替换任何模型/CLI推理结果。

显式`live_llm_mixed_finalizer_acceptance.py --source-profile-id 26 --postgres --two-apis`使用授权真实`api.deepseek.com` / `deepseek-flash`。一次真实read_text_file读取原恢复源文件，真实后续流式回复返回仅存在于文件的随机验证串；2次HTTP200、2份usage、8634tokens，模拟LLM0。原HTTP/SSE转发，上游与事件在该算例目录。

`tmp/N5-output-protocol-readonly-audit-v2-20261110.json`从已核验的上一源码包提取原旧读取器，对真实新记录执行旧准备/归档读取，两者均拒绝；新读取器拒绝未知协议及与原事件不符的降级。三个真实SQLite库均以mode=ro/query_only核验、固定原存储配置，数据库字节不变；旧v1/v2记录和既有完成的legacy v5完整历史继续有效，不做升级。这是4项负面协议检查与历史兼容检查，模型0、原生执行0。

7轮实际原构建各一次，20个已观察实际API退出、7个自建S3 owner退出、4个临时PostgreSQL库删除并独立确认；开发数据库未写入。原生/S3验收、模型证据和只读协议检查分别计数，历史模拟LLM集合未执行。源码审查为`tmp/N5-no-history-protocol-review-v1..v5-20261110.json`，覆盖导入辅助代码。

## 继续推进

优先补齐取消时归档仍pending的实际copy排空与容量保留，再继续原diagnostic增长/未知额度/原期限到期组合、证据生成和同步在完整证明前中断，以及历史缺失停止证明的明确处置和其他处理器/FS/legacy/旧缓存staging。原四条remaining不删减，N5-04不标完成；完整53项主线和295项功能交付目标继续有效。


2026-11-10（归档pending取消）：取消专用v6绑定原worker确切退出、版本化IO归属及精确复制请求结束/owner退出；无预留在同一账户准入锁内复核。四组真实场景通过：原上传API退出、活补传阻止收尾后实际线程排空、补传API退出、准入前退出；双API及重复请求保持原ID/输入/期限/实际stop/日志/额度，不建缓存、不创建校验请求、不编译/求解/发布，未知远端保持pending及完整预留。另真实模型兼容2次HTTP200、8554tokens，模拟LLM0；两库只读6项负面及旧v5历史通过。两份夹具失败原样保留。7原根各一次、23个实际API及7自建S3 owner退出，3临时PG独立确认删除。主线37/53、剩16，新增整项0；资源11/17、剩6，原N5-04四条remaining及295目标不变。继续实际过期/诊断/未知额度组合、证据生成/同步前中断、历史缺失停止证明处置及其余处理器/FS/legacy/staging。 见[后续实际交付](pending-archive-cancellation.md)。
