# 输出归档未完成时结束取消

2026-11-10：已实现归档仍为 pending 时的原任务取消收尾。原 Job/attempt、封存输入、物理停止记录、日志指针、已消耗额度和绝对期限保持；不重建缓存、不创建独立校验请求、不启动编译/求解，也不发布未归档成果。主线仍为 37/53，剩16项；资源11/17，剩6块。N5-04四条原要求及295项全功能目标保持。

## 行为与授权

只有原准备凭据 v2 明确采用持久IO协议的任务可以使用此路径。旧无证明记录继续拒绝自动推断。取消凭据 `vibeflow.job-write-checkpoint.v6` 与已归档专业收尾 v5 分开，只允许原任务的 `cancelled/cancel_requested`，不能授权成功发布或其他终态。

原 worker 必须有确切出生身份对应的实际退出记录，所有已登记原生进程必须有原停止证明。收尾IO通过原 CaseStorageOperation 排他登记，且数据库中不存在本任务的独立校验请求或指针。输出复制另核验以下事实，不能用IO登记本身代替复制排空。

| 复制状态 | 取消收尾依据 | 远端与容量 |
| --- | --- | --- |
| 上传尚未准入 | 在与上传准入相同的账户锁内复核预留不存在 | 原 pending 清单保留，不补造写入 |
| 原 worker 在上传中退出 | 预留写者与原 worker 完全一致，引用原实际退出记录 | 未知结果仍按全部字节预留 |
| 补传请求仍存活 | 拒绝收尾，保持取消中；租期过期不授权接管 | 不释放预留 |
| 受保护补传请求实际返回 | 精确复制结束事件绑定写者、清单、预留声明摘要和实际返回结果 | 原 pending 状态及未知容量保留 |
| 独立补传API实际退出 | 在SQL锁外核验精确出生身份退出，锁内复核原预留绑定 | 退出不等于远端上传成功 |
| 预留已 released/materialized | 核验同一预留的已关闭状态，不伪称API退出 | 沿用既有容量结算契约；不升级归档回执 |

取消后不再允许新的显式补传，也不允许迟到请求提交成功归档回执。已完成取消的查询明确返回 `state=cancelled, archive_pending=true, finalization_pending=false`。没有归档成功回执的远端数据继续保留；后续只有既有完整对象核验才可以结算已经实际完成的容量。

## 实际验收

所有故障均作用于真实 Pandoc/LaTeX、SQL、SeaweedFS4.52和实际HTTP请求，没有替换原生结果或模拟模型回复。

| 场景 | 数据库/处理器 | 实际证据 |
| --- | --- | --- |
| 原上传进程退出，双API竞争及重复取消 | SQLite / report | `tmp/professional-finalizing-n_slpxm_/professional-result.json` |
| 活补传拒绝收尾，三次Task取消后等待受保护线程结束 | PostgreSQL / report | `tmp/professional-finalizing-o6oj817x/professional-result.json` |
| 上传准入前实际API退出，无预留仍可结束取消 | PostgreSQL / report | `tmp/professional-finalizing-aqdxt6tg/professional-result.json` |
| 活补传拒绝收尾，实际补传API退出后双API接续 | SQLite / LaTeX | `tmp/professional-finalizing-fykpjhxx/professional-result.json` |

上述四组均保持原ID、输入、原绝对期限、原准备与实际stop、原日志指针及发布头；待归档输出保持pending，已存在的未知容量预留未释放；独立校验请求为0，恢复编译/求解为0，发布状态为failed。重复请求不新增复制关闭或终态事件，迟到补传返回409。未准入场景不创建预留。

补传Task取消的HTTP请求实际返回500，记录保留；其受保护线程实际结束后，另一个显式收尾请求才完成取消。这不是把原500改记为成功。

两份未通过报告原样保留：`professional-finalizing-co_mx8e5` 的重启助手错误等待仍存活的peer出生身份；`professional-finalizing-714_gp_x` 在重启夹具轮换JWT密钥后错误复用新token访问旧peer。修正为重启前读取确切目标身份、各peer保留自身token；不放宽产品退出、归属或令牌校验。二者均不计为通过。

真实模型兼容独立验证原有“实际错误、HTTP取消、跨API恢复、原文件工具读回”流程：`tmp/professional-finalizing-75keau6w/professional-result.json`。通过显式 `live_llm_mixed_finalizer_acceptance.py --source-profile-id 26 --postgres --two-apis` 调用 `api.deepseek.com/deepseek-flash`，2次HTTP200、2份用量回执、8554 tokens；一次真实read_text_file后模型返回原文件内的完整随机串。模拟LLM为0。它是模型兼容证据，不能代替前述取消场景。

只读实际SQLite审计 `tmp/N5-pending-archive-readonly-audit-20261110.json`：v6完整历史有效；更改用量摘要、缺失IO归属、取消凭据授权成功/其他原因、修改复制清单、缺失原退出引用等6项均拒绝。先前已完成v5历史仍有效。两库以mode=ro/query_only打开，前后字节摘要一致。纯协议负向数据不包含模型调用。

独立证据复核：7个原生根各启动一次，四组新取消和一组模型兼容通过，另两组失败保留；23个已观察实际API出生身份退出、7个自建S3 owner实际退出，3个自有临时PG数据库在系统目录中确认不存在。开发库未修改，未运行历史混合mock回归集合。

## 尚未覆盖及后续

原绝对期限不变已验证；本路径与实际期限已过、诊断输出、未知输出额度组合尚待验收。预留已关闭两种状态的定向竞争也未在此批分别覆盖。证据生成/同步或专业校验尚无完整证明时的恢复、两份历史LaTeX缺失物理停止证明的恢复或明确处置，以及其余处理器、FS、legacy、旧staging/恢复目录继续按原N5-04范围推进。不得据此关闭N5-04或将53项估时等同295项全功能交期。

审查登记：`tmp/N5-pending-archive-review-v1..v3-20261110.json`，91份源码摘要；只读审计单独登记于 `tmp/N5-pending-archive-auxiliary-review-20261110.json`。真实模型运行后的源码差异仅是夹具peer token保留，所有已审查产品源码摘要相同。最终证据为 `tmp/mainline-N5-04-pending-archive-evidence/validation.json`，源码包为 `platform/backups/mainline-N5-04-pending-archive-20261110.zip`。


2026-11-10（诊断/未知额度/原期限）：新增真实验收，产品逻辑未修改。实际编译后在已提交样本之外增长原诊断inode、实际API退出，恢复补计8192字节并保留4096未知窗口；32768字节超原总额与60秒原期限自然到期分别终结为failed/resource_limit，无新原生child或prepared发布。原期限已过且补传仍活着的取消，拒绝收尾直至实际线程结束，保留pending和完整未知远端容量。四组通过、两份断言错误运行失败及一份复核脚本失败保留；6原根各一次、23实际API及6自建S3 owner退出，3临时PG独立确认删除。本子阶段真实模型0、模拟LLM0；既有DeepSeek2次/8554tokens仅作原证据引用。主线37/53、剩16，新增整项0；资源11/17、剩6，原N5-04四条remaining及295目标不变。下一步实现证据/日志/清单形成前中断的持久阶段登记与同attempt恢复，继续历史LaTeX缺证明确处置及其余处理器/FS/legacy/staging/已关闭预留竞争。 见[后续验收](finalizing-diagnostic-budget-recovery.md)。
