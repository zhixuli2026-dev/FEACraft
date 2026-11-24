# N3-06 报告主线原条件验收

2026-11-06，按原条件完成。compose、HTML/DOCX、LaTeX统一生成、发布与取消；实际图件/公式与文档可读，并发、取消和过期结果保留正确版本，原证据门禁不降低。前置N3-05、N2-05已完成；N2-07历史选择、固定下载与源码定位已有独立验收。

| 部分 | SQLite / PostgreSQL | 证据 |
| --- | --- | --- |
| 工程HTML/DOCX | 各24项通过 | `tmp/report-handler-evidence-p0q_ncq0/result.json` |
| compose | 各25项通过 | `tmp/compose-handler-evidence-iqjxc74e/result.json` |
| 普通文档 | 各23项通过 | `tmp/ordinary-document-evidence-ykt3a1kf/result.json` |
| LaTeX | 各32项通过 | `tmp/latex-handler-evidence-15k_57q2/result.json` |
| 注册投递竞争 | 各7项通过 | `tmp/registered-delivery-evidence-mx5hc2p0/result.json` |
| 工作流兼容 | 各17项通过 | `tmp/workflow-handler-evidence-8nuljslp/result.json` |
| 当前预算实现后的注册兼容 | 各7项通过 | `tmp/registered-delivery-evidence-2ltm8qqh/result.json` |

以上进程均退出0，临时PostgreSQL数据库已清理。工程报告/compose与LaTeX各按其启动时加载的代码记录；投递修复由新专项与工作流兼容补足，最新预算改动另由普通预算、真实编译与注册兼容验证，没有宣称所有长套件都在每一次后续增量后重跑。源码绑定及终态收据见 `tmp/mainline-report-cancel-budget-evidence/validation.json`。

所有模板、复杂资源、编辑器及其他专业入口仍按295项完整交付；资源预算、停机与恢复由N4/N5继续，不追加隐式前置，也不删除原范围。

## 普通文档投递竞争

首轮普通文档 PostgreSQL 在新版本提交时返回 `409 state_conflict: Job 20 is already claimed or terminal`。绑定步骤已在提交事务中登记持久投递意图；另一个 API 的扫描器可以先认领，但 `step_handlers.launch` 仍直接调用 `start_job` 再次认领。新增双 API 定向夹具，在提交完成后暂停原请求、由独立 API 正式投递，然后释放原请求，稳定复现相同409。最早一次夹具启动因未使用的 User 导入位置错误失败，已修正；它不计作产品复现。

现在注册处理器经 `job_delivery.deliver` 使用同一持久投递路径，认领已被其他执行器完成时读取持久状态返回，容量暂满则保留 queued，由扫描器以后重试；不会重建 Job/attempt 或重复启动。原生登记、来源校验等实际错误仍传播。返回的是 `LaunchStatus` 状态快照，既不伪造本 API 的进程对象，也不把兼容文件收尾中的成功提前暴露为已完成。没有投递意图的旧记录拒绝执行。

专项 `registered_delivery_race_smoke.py --postgres` 使用真实报告、compose、几何与 LaTeX；检查另一API已运行/已完成/已取消，以及提交后容量暂满、幂等重放和单一 Job/attempt。修复前确定性失败日志为 `tmp/N3-registered-delivery-before-verified.log`；修复后专项双库各7项通过，注册工作流兼容检查双库各17项通过，均退出0且临时数据库已清理。证据为 `tmp/registered-delivery-evidence-mx5hc2p0/result.json` 和 `tmp/workflow-handler-evidence-8nuljslp/result.json`。普通文档完整复验双库各23项、LaTeX双库各32项现均已完成。工程报告/compose已有整套结果保留，它们的原生生成逻辑未改；本次共用投递改动以新的专项与工作流结果补足验证。
