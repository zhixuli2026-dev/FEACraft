# HTTP断连后的独立校验恢复

后续已复现并修复“先发生错误、收尾再取消”的终态回滚问题，增加保留原错误的v2取消证明并保持v1读取；四种实际组合及真实模型回读见[混合错误与取消](validator-mixed-cancellation.md)。下文保留本段原时点的证据和未完成范围。

2026-11-10（北京时间）。承接[准备阶段API退出恢复](validator-preparation-recovery.md)，推进原N5-04的HTTP取消与同一Job/attempt接续。当前源码9个命名取消场景、2个API退出兼容场景、普通S3完成及真实模型兼容性通过，封存对账以`tmp/mainline-N5-04-validator-cancellation-evidence/validation.json`为准。主线37/53、剩16项；资源11/17、剩6块；295项范围保持。整个N5-04尚未完成。

## 实际反例

`tmp/professional-finalizing-ckytg5ji/professional-result.json`保存真实报告编译和S3归档后的反例。客户端向正式finalize-outputs入口发送完整HTTP请求，在实际受监督编译助手完成并暂停后关闭TCP。API出生身份在断连前后均实际匹配，旧校验请求完成取消及物理停止。再次调用正式恢复入口，原作业却结算为failed/publication_failed，只有原取消请求，无法接续。

反例通过是指复现这一错误，不能计为产品验收通过。旧API退出、S3停止和助手实际退出都有回执，模型调用0、模拟LLM0。

## 修改与证明

新`finalizing_request_cancellation.py`在原请求提交cancelled终态的同一事务保存不可变请求摘要、输出用量和各child最后读取窗口。各窗口独立分块落盘，避免把整棵进程树塞入单个256KiB事件。取消提交先在独立已结束的读事务取不可变case绑定，再在写事务首个DML取得case锁；成功提交及其他native类型保持原UPDATE优先。关闭已有写者不依赖新的内容写入准入。

终态请求的原有启动栅栏会永久拒绝新投递。此栅栏仅证明旧请求不能继续放行工作；实际Windows guardian关闭、WSL内核范围回收和已知出生退出仍分别核验，不能用cancelled字段代替物理停止。

child尚未登记业务身份时，准备证明v2绑定这一持久取消栅栏，保存业务未放行、实际已知助手退出和范围回收结果。它不要求API退出，也不制造API退出见证；缺失出生仍明确保持未观察到。原v1历史证明继续按旧条件核验。历史取消请求缺少新栅栏时拒绝自动升级。

后续显式恢复追加cancelled_drained证书，再创建关联原取消请求的新NativeRequest；不修改旧请求、child或stop。新请求继承累计观察量、已保留未知额度和原绝对截止时间，原构建worker及solver不重放。实际运行后已停止且输出通道封存的额度可准确结算；准备阶段未使用窗口必须有独立未放行证明。

该入口仍核验原Job/attempt未被用户取消、输入和原归属没有变化。用户取消整个作业不授权校验重试。

## 验证进度

实际入口为`attempt_professional_finalizing_acceptance.py --handler report|latex --terminal-boundary before --interrupt-validation|--preparation-boundary STAGE --disconnect-validation [--repeat-disconnect 1] [--postgres]`。首轮SQLite报告运行中证据`tmp/professional-finalizing-hwa453a3/professional-result.json`保留为历史版本，最终当前源码范围见下表。旧请求保持cancelled，新请求完成真实HTML/DOCX/PDF发布，原根只启动一次，原截止时间不变。

源及导入辅助代码审查：`tmp/N5-validator-cancellation-review-v1..v8-20261110.json`。最终执行队列为`tmp/N5-cancellation-final-queue-v2-20261110.json`，13个实际入口均退出0。旧失败队列、未执行位置及历史源码版本保留，不把新算例通过写回旧失败结果。

## 原范围仍待完成

继续准备/运行取消、连续中断、并发恢复与真实diagnostic/未知额度/期限组合，以及证明形成前的证据生成和同步中断、原两处LaTeX缺失物理停止证明的明确处置、其他处理器/FS/legacy和旧缓存/staging。原N5-04四条remaining不删减。原SQLite完成锁失败的精确持锁者仍未定位，不以本次通过宣称其已修复。

## 当前源码的命名场景

| 环境与取消位置 | 数据库 | 连续取消数 | 证据目录后缀 |
|---|---|---:|---|
| Windows请求登记提交后 | PostgreSQL | 2 | 60uuprd5 |
| Windows child登记后 | SQLite | 1 | ffgvhcbr |
| Windows guardian登记后 | SQLite | 1 | lb3dwwv3 |
| Windows实际编译运行中 | PostgreSQL | 1 | g76_5v2d |
| WSL范围计划提交后 | SQLite | 1 | bayo39f1 |
| WSL分配器准备完成 | PostgreSQL | 1 | xn5kfrmm |
| WSL范围分配完成 | PostgreSQL | 1 | aorwqokg |
| WSL native_ready提交后 | SQLite | 2 | t1__4v9v |
| WSL实际编译运行中 | PostgreSQL | 1 | 9uer4jo2 |

目录均为`tmp/professional-finalizing-后缀/`，回执`professional-result.json`。9场景共11次实际请求取消，最终保留原Job/attempt和publication ID；旧请求/停止字段不变，原worker只启动一次，原截止时间未更新，最终重复请求幂等。此为命名场景，不是全部数据库/故障排列。

Windows运行中/PostgreSQL的API退出兼容为`professional-finalizing-m_lcxjkk`，继续保留4096未知额度；WSL准备阶段/SQLite为`professional-finalizing-tg1ok_p9`，仍核验原v1 API退出见证与未放行证明。普通S3完成`attempt-output-normal-v6hg0qhr`保持v1作业checkpoint和v2用量结构。

## 关键时序修正

请求创建、原输出指针关联和commit由native scope的受保护owner工厂接管。取消发生在commit返回前，也先完成登记、取得明确责任再提交取消终态；未创建child的请求不遗留为active。

来源校验准入核验取消栅栏、实际关闭与结算证书，避免在已排空旧请求上反复等待。未形成完整证书的历史写者仍阻止准入，原Job取消或输入/归属变化也仍拒绝投递；完整交错验收继续保留。

准备未放行时，处理器可能已清理临时诊断目录。仅在独立未放行证明成立、从未观察到诊断文件/字节时，才能结算零新增量；存在已观察文件或字节则拒绝，不能凭文件不在了猜测额度。

WSL登记前取消曾有实际已核验stop、但每次仍保留4096未知窗口的情况。实际记录显示：身份提交前不能验证sealed_stop，提交后才能验证。现顺序是物理关闭、真实child身份/stop提交、读取窗口封存、请求终态。未核验停止或未完成持久化仍保留窗口；旧8192额度不回写、不清零。

实际SQLite双连接WAL实验复现读后转写的SQLITE_BUSY_SNAPSHOT 517；首DML写事务等待真实竞争写者后成功提交。取消写事务不再保留前置读快照。旧HTTP锁失败的精确持锁者仍未知，不能据后续通过声称所有SQLite争用已解决。

夹具在真实TCP断连后先观察实际operation.cancelling()，再解除所选步骤暂停。编译及准备标记写完、fsync后原子替换，避免只见文件存在却读到空JSON。夹具转发真实方法，不替代模型或编译结果。

## 真实模型、协议与基础设施分账

本子阶段三轮真实模型累计6次官方`api.deepseek.com` chat-completions、29164 tokens，模拟LLM0。当前最终源码轮次为`live-shell-deadline-xeseyrmt`，模型`deepseek-flash`，2次HTTP200、2份usage、9663 tokens。真实模型选择实际PowerShell工具，命令按原期限超时并实际停止，再由真实模型报告。前两轮`live-shell-deadline-g8q0nq9r`与`live-shell-deadline-iilahgmp`保留历史源码版本。各目录的`independent-model-and-exit-receipt.json`独立核验实际API出生退出。

两份真实SQLite历史库的SELECT-only证明/篡改拒绝检查、实际WAL争用实验、实际停止记录提交前后的封存验证，均属于纯协议/故障检查，模型调用0。见`tmp/N5-cancellation-readonly-protocol-audit-20261110.json`、`tmp/N5-cancellation-SQLite-read-upgrade-protocol-20261110.json`、`tmp/N5-cancellation-seal-order-protocol-20261110.json`。

本段共37轮真实专业文档算例和1轮普通作业，原根各启动一次。独立核验81个实际API出生退出、38个自建S3 Linux owner退出、19个临时PG库删除后确实不存在；开发库未写入。运行清单为`tmp/N5-cancellation-run-inventory-20261110.json`，独立对账为`tmp/N5-cancellation-infrastructure-audit-20261110.json`。这些基础设施检查不计为模型调用。

## 九份失败保持原结果

| 目录后缀 | 实际事实与后续处置 |
|---|---|
| 1pjzzmy_、mlf1757d | 旧取消请求和新校验已结束，来源准入仍等待旧无stop写者，HTTP超时；诊断轮保存等待位置与SQL快照。补证书准入后命名场景通过。 |
| 38_qzo0c | 未放行业务的诊断目录已清理，原恢复仍要求读取而409；补未放行且从未观察诊断的独立核验。 |
| pya8zoc1 | 两次取消后实际SQL已成功，夹具对合法空child文档报KeyError；改夹具，旧报告仍失败。 |
| 88nm07pa、9olt351v | 实际发布成功，两次取消却保留8192未知窗口，零未知额度断言失败；修正实际身份/stop登记与封存顺序，历史额度保留。 |
| 4s07j7vz | 实际发布成功，部分准备有完整范围关闭但无child身份，夹具读取native.uid时TypeError；改为要求独立未放行证明，不补造身份。 |
| 8zd3e6gy | SQLite终态case锁返回OperationalError，正式接口500；保留堆栈，实际WAL实验确认新读后写路径有风险，改事务顺序。历史持锁者仍未知。 |
| ibcp4elr | 夹具读到尚未完成的标记JSON，JSONDecodeError；标记改原子提交，旧失败保留。 |

部分失败的临时库已按隔离规范删除；没有完整持久原生范围回执的历史，不据API/S3退出或事后新算例通过补造停止。审计明确保留`all_failed_history_native_kernel_disposition_proven=false`。后续须提供明确处置，并完善失败前的完整SQL快照保留。

## 下一段未完成事项

继续原取消与故障/额度/期限交错。源码复核发现：先有非取消错误、再在收尾收到取消，可能触发当前仅接受CancelledError的证书限制，须实际复现并处理，尚未计为验证通过。另保留原Job取消交错、diagnostic增长、未知额度耗尽、过期及多API恢复竞争，以及上文原四条remaining的全部范围。所有53项主线及295项全功能目标继续有效。
