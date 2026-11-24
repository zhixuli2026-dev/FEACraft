# N5-05：缺失运行目录、重复续算与类型化命令

版本说明：本页记录原归档时的策略与证据。S3部分封存损坏曾拒绝；最新显式重试/续算已改为独立可信归档输入视图，详见[归档输入恢复](archived-input-recovery.md)。历史11类负例只绑定原源码，不作为最新策略的执行入口。

2026-11-08。主线 **34/53 完成、4 进行中、15 待开始，剩19项**；295项全功能交付范围保持，N5-05整项仍未完成。

## 封存输入与运行缓存

`attempt_workspace.verify_inputs` 单独核验原封存：SQL清单归属、实际文件集合/目录、大小/SHA/执行位及磁盘清单都必须吻合；S3还核验绑定的远端原封存。原 `verify` 继续要求运行目录存在。复制原输入创建新尝试只依赖前者，不会因为旧工作目录缺失而伪造或重新运行旧目录。

S3续算使用已验证的原输入和独立验证的实际归档输出视图。只丢失旧run目录时不再强行访问它；本轮完整续算、发布、fresh比较后旧run仍未重建。`inspect_runtime` 对实际完成捕获仍要求真实运行目录存在，不能凭封存输入制造原生输出。

部分原封存缓存被修改或删去时，当前实现仍拒绝并保留现场，即便远端封存存在，也不会拿当前工程文件补造原字节。本批验证了这种拒绝，不宣称已实现部分输入缓存的独立归档视图恢复；这项后续范围保留。

## 两次取消后继续计算

真实400单元icoFoam首次写出0.005后取消；第一次续算写出0.01后再次真实取消，等待停止及写入排空。第三个同根attempt从0.0105计算至0.5并正式发布，保留前两个取消记录和父尝试关系。再从续算作业发起fresh rerun，使用新根、number=1；最终U/p与第三次尝试的最大绝对差均为0。当前这条双取消链使用本地FS和临时PostgreSQL；S3重复续算及崩溃窗口继续验收。

## 类型化用户命令

`command.convert/geometry/mesh/pvpython/solve` 的rerun由服务端保留原命令、运行方式和预算，再准备当前输入。API不再要求客户端提供内部case前缀等完整执行配置；非空替换命令参数拒绝，改命令仍使用新提交入口。其他专业处理器仍要求其明确准备参数。

首轮实际失败后，重试被错误判断为输入变化。保存现场的逐文件对比确认只有平台维护的 `0-caseDict/case-events.jsonl` 追加了失败事件，caseDict文本及工程内容没有改变。曾尝试的caseDict语义归一化改动已撤回。

最终实现保留时间线作为封存输入：只有原前缀的字节SHA、长度和执行位保持，且追加记录是带平台schema标识、整数case ID及当前case归属的有效JSON时，来源比较才还原原前缀记录。截断、改写原字节、无效追加或其他case记录仍拒绝。命令在私有运行目录中改写或删除时间线时，发布校验拒绝，活动工程历史继续由平台追加；不将旧时间线发布回去。caseDict及普通输入的变更仍完整参与比较。

这些是用户命令处理器的实际执行和版本发布验收；它们的 `professional_workflow_proof=false`，不替代真实CAD、网格或求解器的专业验收。

## 真实证据

| 场景 | 证据 | 验证内容 |
|---|---|---|
| 缺失旧run后的原输入重试 | `tmp/live-cli-startup-o4e9c9d1/job-repeat-policy.json` | 实际Python取消，旧run实际移走；双API同key只执行一次；旧run不重建；输入损坏/缺失拒绝；PostgreSQL |
| 双取消、第三次尝试续算 | `tmp/live-cli-startup-u1hanrqu/solve-resume.json` | 真正的两次取消，attempt=3，同根；0.0105到0.5；与fresh的U/p差0；PostgreSQL/FS |
| 仅S3旧run缺失 | `tmp/live-cli-startup-uc7soqdf/solve-resume.json` | run移走、inputs保留；11类原生/归档/部分封存拒绝检查；续算发布、fresh比较U/p差0，原120秒预算 |
| 5种类型化命令 | `tmp/live-cli-startup-z348tyqd/typed-repeat.json` | 20个真实Python进程；失败/重试/当前输入重跑；版本及历史保留；普通文件、工程字典变化和替换命令拒绝 |
| 时间线保护 | `tmp/live-cli-startup-8ihiv3vr/typed-timeline.json` | 4个真实Python进程；4类前缀/追加/归属篡改在执行前拒绝，含布尔值不能冒充整数case ID；有效失败事件追加允许重试；原生命令覆盖/删除仅影响私有尝试且发布拒绝 |

每个成功运行结束后，真实api.deepseek.com / deepseek-flash Agent调用read_text_file读取绑定场景报告的哈希标识；模型与工具回路独立于原生命令、HTTP、文件和数值断言，不宣称模型选择了这些操作。模型调用次数和用量由本轮验证清单汇总，模拟调用0。先行6类时间线版本 `fsuow_pc` 另有2次真实回读，作为先行代码证据单独统计。

入口为 `live_llm_job_repeat_acceptance.py --policy --postgres`、`live_llm_repeated_solve_resume_acceptance.py --postgres`、`live_llm_solve_run_cache_acceptance.py --s3 --postgres`、`live_llm_typed_repeat_acceptance.py --postgres`、`live_llm_typed_timeline_acceptance.py --postgres`，均加授权profile参数。导入的真实服务、只读凭据提取、实际上游透传、临时库迁移和显式场景覆盖已审查，没有执行历史模拟回归集合。

写入及模型回读种子遇到明确的 `storage_quota_busy` 或 `validation_busy` 时，只等待尚未准入的请求；修订/内容冲突等其他错误继续失败，不撤销占用或放宽校验。

## 失败与剩余范围

`8pj30fvb` 的run缺失续算已经成功，随后fresh比较在与本地双取消链并行时触发timeout/deadline；该轮模型调用0、整轮失败，仍保留原始记录。随后单独运行的 `uc7soqdf` 整链路通过。这里只区分观测条件，不声称已经证明并行负载是唯一原因；并发性能仍归既定N4/N7范围。

`jstcp0ak`、`axbopxny` 均在类型化命令重试前误判来源变化，模型0；实际文件对比后撤回无效的caseDict改动，改为验证时间线追加前缀。`nui54px_` 已实际通过首类重试/重跑，后续编辑遇到真实存储结算忙；`9r4hlg3p` 在负例请求中收到结算忙而非目标来源冲突，未计通过。`z7zoq6vg` 的6类时间线功能检查通过，但模型种子写入收到结算忙，仍计整轮失败、模型0。上述目录前缀均为 `tmp/live-cli-startup-`，无失败回退到模拟通过。

注册处理器逐项证据与缺口见 `job-repeat-handler-acceptance.json`。命名场景通过不等于该处理器所有模式已完成；其余专业处理器、旧固定argv、S3重复续算/崩溃窗口、部分输入缓存恢复、真实工具链变化及原专业适用矩阵继续。N5-06阶段/工作流/Agent checkpoint联动保留。
