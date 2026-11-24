# 恢复校验运行中API退出后的接续

2026-11-10（北京时间）。具有原始输出和停止证明的S3报告、LaTeX作业，在独立恢复校验运行中发生API退出后，可以明确接续同一个Job/attempt，完成真实校验及原成果发布。原worker和solver不重放，缺失的旧校验停止字段保持缺失。主线仍37/53，剩16项；本段不关闭N5-04，295项全功能目标继续有效。

## 实际问题与修复

修复前的真实Pandoc反例：原成果已归档并冷恢复，实际编译后暂停受监督的校验进程，执行API退出79。新API的补交返回409，旧NativeRequest一直active且没有保存校验用量。反例记录在`tmp/professional-finalizing-hg7mxkdh/professional-result.json`，不是模型推理替身。

`finalizing_native_scope.py`为每个独立校验child保存与原case、Job/attempt绑定的请求配置、API出生身份、启动计划、Windows guardian、准备观察及关闭回执。Windows使用真正的Job成员排空；WSL另外持久登记真实cgroup分配、同进程原生预检与内核回收。guardian的未来回执写入额度绑定guardian自身出生身份，API退出后仍保留；已有镜像清理的历史协议不被改写成新校验协议。

WSL以显式root启动固定预检，加入确切内核范围后降为nobody，再放行业务校验。降权前后逐字段核对同一进程出生，只允许固定uid从0变为65534。Windows守护关闭证明与Linux范围回收证明分别保存，不能用Windows退出替代Linux完整停止。

`finalizing_native_usage.py`在每次最多4096字节的pipe读取后提交累计用量，再允许下一次读取；diagnostic记录确切目录、文件身份及已观察长度。真实停止后，可从原文件补记尚未持久的diagnostic增长。API退出时，一个未结算读取窗口保留为`unsettled_output_bytes`，从后续可用额度中扣除，绝不算作实际观察到的字节，也不清零。

只有存在未结算额度时才使用`vibeflow.execution-output-usage.v3`；普通完成保持原v2格式。恢复仍使用原claim的绝对截止时间和总日志额度。

`finalizing_request_recovery.py`在确切旧API出生已退出、所有已登记范围实际关闭、累计用量和未结算窗口均能核验后，将旧请求作为failed/OwnerExited保留，并附独立恢复证书。旧child的身份和stop不改写；重试事件把新旧校验请求串起来，逐次核验额度不倒退。明确调用原`:finalize-outputs`才建立下一次专业校验；它不是原solver重算。

来源校验准入也核验这份独立证书，避免有真实停止证明的旧请求仍被当作未知写者。缺失、损坏或历史无该证明的请求继续拒绝自动接管。

## 验收与证据分类

实际入口是`attempt_professional_finalizing_acceptance.py --handler report|latex --terminal-boundary before --interrupt-validation [--postgres]`。夹具先转发真正的Pandoc/LaTeX编译，再暂停实际受监督进程；执行真正的API退出、HTTP重试、SQL提交和S3回读。每轮检查原Job/attempt、输入、原截止时间、原发布ID及正文，原构建worker仅启动一次。

- 报告、LaTeX分别在SQLite和临时PostgreSQL验证运行中API退出及原发布接续。新旧校验请求分别保留，4096字节待核验额度由新请求继承。
- 原提交前中断的报告、提交后中断的LaTeX兼容性通过。普通S3完成和真实原截止时间到期失败另行记录，不把资源失败算作成果成功。
- PostgreSQL另补两轮原始回执导出，在临时库删除前保存独立恢复、完整Windows成员关闭和WSL cgroup回收凭据。额外回执留存轮次不计成新的功能场景。
- `tmp/N5-validator-budget-protocol-checks-20261110.json`是六组纯额度/协议检查，覆盖实际运行用量恢复、未知额度扣减、非法数值拒绝、旧v2格式保留及原期限已过，不是LLM验收。
- 真实模型兼容性单独使用`live_llm_native_shell_deadline_acceptance.py --source-profile-id 26`：`api.deepseek.com / deepseek-flash`实际2次chat-completions、2份usage回执，共9704 tokens。模型选择真实Shell工具，命令按原期限超时并实际停止，模型随后报告结果。证据为`tmp/live-shell-deadline-tsj7i_au/live-result.json`及同目录`independent-model-and-exit-receipt.json`。密钥未输出，开发数据库只读。

模拟LLM调用为0；没有执行历史混合模拟回归。源审查为`tmp/N5-validator-interruption-review-v1..v6-20261110.json`，真实模型入口另有独立审查。v4的完整审查源码保存在`tmp/N5-validator-reviewed-source-v4-20261110`；后续夹具仅增加原始证明导出，生产代码没有因导出重跑而改动。

两轮实际失败保留：首轮报告在独立停止及重试已完成后，来源准入仍要求缺失的旧stop，HTTP最终ReadTimeout；随后接通独立证书检查。首轮LaTeX的实际原生栈定位到`owned_process.prepare`拒绝降权预检，随后修正显式root预检及同出生降权检查。不补造旧stop或把失败覆盖成通过。审查记录器曾因Python编码别名拼写失败，独立记录该工具失败并使用新的记录脚本，没有重跑一次性脚本。

完整场景、源摘要、临时库删除和端口/进程复核，以`tmp/mainline-N5-04-validator-owner-recovery-evidence/validation.json`为准。源码包为`platform/backups/mainline-N5-04-validator-owner-recovery-20261110.zip`。

## 原范围仍需完成

1. 独立恢复校验在child预登记、scope分配或guardian/preflight准备中退出，尚未形成完整准备证明的处置；旧协议、未知出生继续保守拒绝。当前运行中退出成功不等于全部准备边界已通过。
2. 恢复HTTP取消、重复恢复中断和连续多次owner退出、实际非零diagnostic增长与未结算额度耗尽、专业校验之后到期等组合的实际验收。当前运行中退出、单次4096字节保留和纯额度检查不能替代这些场景。
3. 早期输出/原日志证明形成前的证据生成、同步及校验中断；原`native_execution_smoke`两处LaTeX历史物理停止证明的恢复或明确处置。
4. 其余注册处理器、legacy、FS及取消/输入改变/并发接续矩阵；历史已提交缓存、旧staging/恢复目录和缺失唯一未归档副本的处置。

N5-04原四条remaining原文保留；N4/N6/N7与全功能295项的剩余目标不缩减。
