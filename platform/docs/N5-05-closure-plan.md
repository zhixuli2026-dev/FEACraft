# N5-05 原要求收口清单

依据：[原主线任务及验收条件](next-stage-mainline-plan.md)、[权威状态和原 remaining](mainline-task-status.json)、[操作契约](job-repeat-policies.md)。53项主线及295项全功能目标继续有效。

原条件是“重试留新 attempt；续算验证网格/初始或重启场/时刻/求解器；未知 shell 不自动重放”。N1-04、N2-06转入本项的冻结来源、旧专业准备、缓存丢失、类型/版本/归属和既定崩溃交错要求也保留。下列三组对应这些尚需核对的要求；处理器命名证据覆盖率单独记在[矩阵](job-repeat-handler-acceptance.json)。

## G1：其余旧固定 argv 类型的明确重新准备

状态：已完成本组剩余三类FS/S3真实命名验收，见[本批证据](legacy-engineering-reprepare.md)。已有 geometry→geometry.export、convert→latex.build 的FS/S3真实旧命令一次执行及当前输入新根证据，见[既有验收](engineering-repeat-and-legacy-reprepare.md)。

- 补 mesh→mesh.generate、solve→solve.openfoam、postprocess→postprocess.run；旧命令真实执行且保持一次，正式目标使用真实当前工程输入及工具，不重放旧 argv。
- 核对明确 handler/version、作业类型、原尝试版本、当前修订/事件序号、停止/封存证明与新旧来源记录；错误版本/类型、缺封存、未排空均拒绝。
- 记录正式产物、原历史保留、同幂等键返回同一新根；FS/临时PG与S3/临时PG分别留证。

## G2：来源、版本、工具链和发布归属的拒绝条件

状态：本组命名拒绝检查及已有缓存/检查点证据合并完成，见[实际验收](repeat-integrity.md)和tmp/mainline-N5-repeat-integrity-evidence/requirement-evidence-map.json。

- 汇总现有真实检查点的网格、原始场/重启场、时刻、求解器和环境负例；明确仅注册的串行OpenFOAM检查点可resume，其他处理器的公开操作选项和拒绝原因必须一致。
- 核对专业输入或当前上游发布变化的拒绝，保留原封存与成果；本批真实依赖图的后续输入变化拒绝属于此项证据。
- 补齐未知/不匹配handler版本、请求/执行哈希、成果预留/attempt/case归属损坏，以及已声明工具链变化的实际拒绝检查。区分已归档原字节、当前输入新准备与无法验证的历史，不补造旧来源。
- 对照原缓存要求：FS严格拒绝损坏封存，S3整份/仅run/部分缓存损坏或缺失时使用独立核验归档视图；未知内容保留；重复取消后的第三次真实续算及数值比较已有证据，不重复冒充新交付。

## G3：重复操作的准入和崩溃交错

状态：已完成FS/S3实际准备交错、提交后API终止/恢复、活动工作流拒绝，见[实际证据](repeat-interleavings.md)。已有双API同键、取消排空、真实重复续算及普通命令唯一执行证据保留。

- 原生文档准备在SQL/算例锁外完成；准备期间原来源版本变化后，重新核对并拒绝过期请求，不建立未经确认的新执行。
- 在重复请求已持久提交、投递/启动尚未完成的窗口实际终止处理请求的API；由共享持久状态恢复，保留同一幂等键和新Job/attempt关系，实际正式程序只启动一次。
- 核对旧工作流仍活跃、原尝试尚未排空及原head已变化时的拒绝；新的显式重复操作不能暗中推进旧工作流。完整工程回退/审批/Agent checkpoint联动继续按原N5-06交付。

## 收口记录

逐条记录原要求、来源文件和证据路径、实际服务/模型、原生执行/实际基础设施/纯协议的范围，以及失败或未验证。涉及推理全部调用授权真实服务；无模拟LLM回归。未证实的要求保持在本项，不能以处理器已注册或单个成功运行代替完整条件。

本项关闭前核对上述三组、原任务全部remaining和26类处理器适用操作。完整部署故障矩阵、迁移回退、三类CAE基准、负载性能及295项最终台账按原N7继续验收；本清单不替代这些交付。

2026-11-09：G1/G2/G3按原条件完成，N5-05策略验收关闭；逐条原remaining审计在tmp/mainline-N5-repeat-policy-closure-evidence/original-requirements-audit.json。53/295完整目标与原其他任务继续。
