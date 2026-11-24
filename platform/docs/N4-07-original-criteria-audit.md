# N4-07 原始条件与当前缺口

最新状态（2026-11-17）：按原退出条件完成，主线 49/53，见 [最终退出评审](N4-07-closure-review.md)。以下按时间保留原问题及修复证据。

2026-11-16。原始任务仍未关闭，主线 48/53，剩 5 项；295 项全功能交付目标保持有效。上一轮真实后处理/报告和本轮关闭窗口验收均是实际进展，不代表原条件已经全部满足。

## 原始条件

[next-stage-mainline-plan.md](next-stage-mainline-plan.md) 的 N4-07 行规定：**关闭与用户取消可区分；worker 只在完整收尾后确认任务；失联可恢复**。任务名称另要求优雅关闭、紧急关闭、看门狗和接入已有 Redis worker。没有将“所有专业工具在任意操作系统上都运行”列为该行独立退出条件；专业环境和跨主机交付仍须在完整计划中完成，不能删除或以本轮结果替代。

| 原条件 | 已有实现与实际证据 | 当前结论 |
|---|---|---|
| 优雅关闭、紧急关闭、等待期限 | `PlatformWorker._close_drained` 停止领取、等待宽限期、取消未完成 delivery、等待全部收尾后关闭连接；真实同机 Redis/WSL 关闭验收 | 命名场景已验证 |
| 关闭与用户取消区分 | `_redis_execution` 在 delivery 关闭时调用 `interrupt`；真实运行中的关闭为 interrupted，API 用户取消为 cancelled | 已验证两条实际路径，保留现有取消优先级 |
| 完整收尾后确认任务 | 启动交接受 shield 保护，关闭等待交接、停止进程、完成 SQL 收尾；本轮直接观察真实 Redis 执行中键 | 启动、运行、专业 PDF finalizing 窗口已有实际证据 |
| 失联恢复、看门狗 | 实际 Linux worker 硬退出后 namespace init/成员退出，新 worker 恢复原 attempt；Windows MPI/UDF 有独立范围和内核撤销证据 | 已登记运行中失联路径已验证；未完成登记仍有实际实现缺口 |
| 已停止但最终记录失败 | PostgreSQL 实际拒写，独立缓存 Linux worker 重核 S3 bundle 后完成原排空及提交；损坏产物拒绝恢复 | 已验证命名故障，不泛化为全部故障矩阵 |

相应资料：[同机关闭](worker-shutdown-drain.md)、[执行主机恢复](worker-owner-recovery.md)、[最终记录失败恢复](worker-drain-recovery.md)、[Linux 运行中失联](linux-worker-loss-recovery.md)、[MPI/UDF 失联](professional-worker-death-recovery.md)。这些资料中的旧计数、当时的未完成描述属于历史；当前台账以 `mainline-task-status.json` 为准。历史模拟模型测试不作真实模型验收证据，也不重跑它们。

## 本轮实际关闭窗口

显式入口 `live_llm_remote_shutdown_boundaries_acceptance.py` 使用真实 Windows API、Linux worker、Redis、PostgreSQL 和 S3。辅助观察器调用原生产函数，只在原函数完成指定阶段后等待显式释放，不替换进程、SQL、模型或发布结果。

1. **启动窗口**：实际 gate 和 namespace 已准备，`attempt.launch_prepared` 已保存，但 running 身份尚未提交。请求紧急关闭后，worker 保持存活，SQL 仍为 starting，真实 Redis `arq:in-progress:` 键仍存在；释放交接后，原命令停止，原 attempt 为 interrupted，完整停止凭据通过。
2. **专业收尾窗口**：实际 PDF 页码程序已完成、原停止凭据和 finalization intent 已保存，最终 SQL 提交尚未执行。请求关闭时 worker 等待，Redis 执行中键保留；释放后原任务成功提交，下载 PDF 页码检查通过，随后 worker 退出。
3. 两条路径的原始输入清单逐字不变，均只有一个 attempt；启动新的 worker 后实际任务继续可用。实际命令计算 PDF 的散列证明，再由真实 DeepSeek 调用文件工具读回。模型调用和基础设施故障观察分别记录。

本轮最终证据为 `tmp/remote-worker-atdr_vj8/`：两个关闭窗口、产物证明和原远端基线共 7 个真实任务通过；DeepSeek `deepseek-flash` 2 次真实 HTTP 200，模拟模型为 0。独立退出观察位于 `shutdown-boundary-exits.json`。数据库记录为 `tmp/remote-worker-postgres-013cf5ec20c74619b2268be97649996a/`；临时数据库已删除、访问规则已恢复，Redis/S3 已停止。源码和证据散列由 `tmp/mainline-N4-07-shutdown-boundaries-evidence/validation.json` 归档。

## 启动失联修复与下一步

后续已实际复现并修复根执行完整准备后、running 登记前的硬退出：新 worker 复用原 namespace、准备链和 v4 排空凭据，保留缺失 running 身份与原停止记录，原任务 interrupted，原命令没有执行，新任务可继续。详见 [启动失联恢复](worker-startup-loss-recovery.md)。原始复现和第一次过窄状态检查的失败证据均保留，没有模拟模型回退。

后续又补齐 namespace 已保存但 launch_prepared 缺失的根启动窗口，以及原 owner 退出后、恢复前收到取消的收尾路径，真实新任务均可继续，详见上述记录。[同一工作流的跨主机后处理和报告](confirmed-postprocess-workers.md)也已完成实际验收：单 API 明确 B/C/D/E 确认后，Linux 提取原求解发布，Windows 生成 HTML/DOCX。剩余核对集中于 namespace 尚未保存、专业 child 未完成登记、其他取消组合与外部 owner；不能由根进程退出直接推定完整范围停止，也不新增阶段退出条件。

其余明确范围继续保留：专业子执行的故障组合、其他外部 owner 的停止/接管、确认工作流的 `postprocess.run` 与外部工具环境；全故障矩阵、部署文档/发布包、最终全功能审计仍对应 N7-01/N7-05/N7-06。按原计划核对已有证据后解决实际缺口，不重复扩展权限层或追加阶段退出条件。

后续已补齐 [Windows 报告子执行完整准备后失联恢复](child-startup-loss-recovery.md)：实际 Pandoc worker 在 prepared 已保存、running 未登记时硬退出，原任务恢复为 interrupted，旧报告保留，新 HTML/DOCX 可重新生成。实际联调同时修复了恢复凭据跨 Windows/Linux 校验的登记程序路径问题；最终 8 个真实任务、2 次真实 DeepSeek 调用通过。此处不再把“所有专业 child 缺失登记”笼统算作未实施；剩余聚焦 WSL child、准备链尚不完整、其他原有取消/外部 owner 条件，原主线仍 48/53。上文已完成的 postprocess.run 确认流程也不重复列为新增工作。

随后 [WSL MPI 子执行完整准备后失联恢复](wsl-child-startup-loss-recovery.md)也已通过：读取原准备和 UID65534 入组记录，撤销原 inode 绑定的内核范围，确认原 gate 退出，保留缺失 running/stop 和旧场；新双进程400单元求解及后续 Linux 发布成功。最终 9 个实际任务、2 次真实 DeepSeek 调用，36 项纯凭据检查另列，27 个原进程身份退出。Windows/WSL 两类完整准备窗口均不再列为未实现；剩余核对更早的不完整准备、其他原有取消及外部 owner 条件，主线总数仍 48/53。

进一步补齐 WSL 入组已保存、child_prepared 尚未保存的窗口：只读取原入组身份，核对完整原观察链及原内核历史，不补写缺失事件。同一算例的入组窗口和 prepared 兼容窗口均可恢复并明确新求解。首轮归档前查出普通带类型WSL命令仍以root运行，已沿用现有非特权gate修复；最终完整重跑核对两次串行根和MPI child均为UID65534，14个实际任务、2次真实DeepSeek调用、78项纯凭据检查及44个原进程身份退出通过。首轮功能路径、2次真实调用及权限失败记录保留，见WSL文档增量。下一处代码缺口是 Windows/WSL 根执行的准备身份尚未写入 running 时，原守护/根范围恢复仍依赖 process_identity_json；Linux 根启动恢复已有专用路径。其余更早观察、取消及外部owner仍按原范围核对，不额外增加阶段条件。

2026-11-16 N4-07 根启动失联已补齐：Windows/WSL 完整准备后、WSL 入组后缺少 launch_prepared，以及原 owner 退出后收到取消，共四个真实窗口均恢复并完成新命令发布。复用原启动观察、独立守护和内核范围证据；原 process_identity_json/child_processes_json 仍为空，缺失 stop/prepared 不补造，旧公开文件不变。普通 WSL gate 和新 payload 均实测 UID65534。同版源码再通过原 WSL MPI 子执行失联恢复及新双进程400单元求解兼容。两轮共21个实际任务、4次官方DeepSeek deepseek-flash HTTP200、模拟0；164项纯凭据拒绝检查另列，65个记录原进程出生身份已消失。两套专属Redis/S3停止，临时PG删除且访问规则恢复。见 root-startup-loss-recovery.md。主线48/53、剩5，295项交付状态保持；更早不完整准备、其他原取消/外部owner及N4-05/N7范围继续。

2026-11-16 N4-07 运行中取消与失联收尾已修复：旧恢复仅接受 interrupted，导致 job=cancelled/attempt=cancelling 无法完整排空。现复用原完整范围证明并锁内复核取消时间，完成 cancelled 收尾，保留原身份、输入、发布与缺失 stop。真实 Linux worker 退出后取消、Windows/WSL 取消已提交且进程仍活时退出均通过；后台心跳停止，旧产物保留，新命令可发布。同轮普通Linux中断、WSL入组后启动取消兼容通过。共14个实际任务、2次官方DeepSeek deepseek-flash HTTP200、模拟0；100项纯凭据检查另列，49个记录原进程身份已消失。专属Redis/S3停止、临时PG删除及访问规则恢复。见 cancelled-owner-recovery.md。主线48/53、剩5；295项交付状态保持，更早不完整准备、专业child其余取消、外部owner及N4-05/N7范围继续。

2026-11-16 N4-07 专业 child 取消恢复验收补齐：沿用上一轮生产实现，真实 Windows Pandoc child 完整准备后失联及 WSL MPI child 入组后、child_prepared/running 前失联，均经实际API取消和新worker恢复。原取消时间、缺失identity/stop/prepared和原输入发布记录不补造；旧HTML/DOCX及0.5/U保留，新报告与双进程400单元求解发布通过，后续Linux任务和真实模型文件读取通过。共13个实际任务、20个原生子执行、2次官方DeepSeek deepseek-flash HTTP200、模拟0；74项纯凭据检查另列，57个记录原进程身份已消失。专属Redis/S3停止、临时PG删除和访问规则恢复。见 professional-child-cancellation-recovery.md。主线48/53、剩5；295项交付状态保持。下一实际代码缺口：native.recover 跳过原 RETIRED child，但 scope 证明仍要求全部 native child 的独立退休记录；需复用原完整停止证据衔接已完成子步骤与后续失联。更早准备、外部owner及N4-05/N7原范围继续。

2026-11-16 N4-07 已完成原生子步骤衔接恢复已修复：native.recover 不再跳过原 RETIRED 范围，复用原撤销回执并独立核验 owner/guardian/gate 退出；检查点核对原 stop 缺失事实和逐字段原回执一致性。真实LaTeX探测已stopped后编译prepared失联，以及原RETIRE提交后child finish缺失，两窗口均恢复；原child日志/stop不改写，旧PDF保留，新PDF正文及独立编译验收、后续Linux任务成功。LaTeX沿用已有注册程序映射通过双主机凭据校验。共12个实际任务、15个原生子执行、2次官方DeepSeek deepseek-flash HTTP200、模拟0；50项纯凭据检查另列，59个记录原进程身份已消失。专属Redis/S3停止、临时PG删除及访问规则恢复。见 completed-native-child-recovery.md。主线48/53、剩5，295项交付状态保持。更早不完整准备、其余外部owner及N4-05/N7原范围继续。

2026-11-16 N4-07 原启动观察完整、确认缺失的恢复已修复：读取原 Windows host_ready / WSL native_ready 身份视图，保留缺失 prepared/admitted/running/stop；核对原分配并实际撤销完整范围，不伪造入组确认。Windows、WSL 失联、WSL 取消及已有入组兼容四场景通过；原 payload 未执行、旧文件保留，新命令发布成功，普通 WSL UID65534。共12个实际任务、2次官方DeepSeek deepseek-flash HTTP200、模拟0；134项纯凭据检查另列，38个记录原进程身份消失。专属Redis/S3停止、临时PG删除及访问规则恢复。见 root-observation-loss-recovery.md。主线48/53、剩5，295项交付状态保持；更早不完整观察/分配、其余外部owner及N4-05/N7原范围继续。

2026-11-16 N4-07 更早根创建恢复已修复：Windows/WSL created 和 WSL host_ready 复用原 guardian/分配范围，明确部分范围视图，未知 native gate 出生/退出仍为null；实际撤销完整内核范围并拒绝三次迟到加入。独立 guardian 观察绑定准确宿主出生身份。五场景含取消和原native_ready兼容均通过，原payload未执行、旧文件保留，新UID65534命令发布成功。最终14个实际任务、2次官方DeepSeek deepseek-flash HTTP200、模拟0；188项纯凭据检查另列，42个记录原进程身份消失。前两轮修改宿主PID及截短原观察链的凭据拒绝检查失败，分别补齐独立宿主身份和完整不可变启动历史摘要绑定后整轮重跑；两失败轮模型0、共8个实际任务及7+18个记录身份仅作为失败/清理证据。三轮专属Redis/S3停止、临时PG删除及访问规则恢复。见 root-creation-loss-recovery.md。主线48/53、剩5，295交付状态保持；更早无观察/分配确认、专业child部分准备及外部owner等原范围继续。

2026-11-16 N4-07 首条启动观察缺失恢复已修复：真实Windows/WSL进程创建后、原SQL观察提交前退出，复用原意图/guardian/内核分配关闭完整范围；原host/native身份及逐进程exit仍为null，原观察/登记/stop不补写。Windows、WSL、WSL恢复前取消及原created兼容四场景通过；旧产物保留，新UID65534命令发布成功，三次实际迟到加入原范围均拒绝。最终12个实际任务、2次官方DeepSeek deepseek-flash HTTP200、模拟0；152项纯凭据检查另列，35个由独立测试记录的原进程身份已消失。专属Redis/S3停止、临时PG删除及访问规则恢复。见 root-unobserved-loss-recovery.md。主线48/53、剩5，295交付状态保持；原分配/意图/guardian更早未确认、专业child部分准备、外部owner及N4-05/N7原范围继续。

2026-11-16 N4-07 根原生分配失联恢复已完成实际验收：分配意图冻结原实现与业务释放顺序，原worker及已知创建者退出后，复用原retire/abandon回执关闭已确认或未确认分配；未知引导身份不伪造退出。意图、准备、分配返回未确认、已确认、未确认后取消五场景及原首条观察缺失兼容通过，原payload未运行、旧文件保留，新UID65534命令均发布成功。共16个实际任务、2次官方DeepSeek deepseek-flash HTTP200、模拟0；234项纯凭据检查另列，74个已记录原进程身份核验消失。专属Redis/S3停止、临时PG删除及访问规则恢复。见 native-allocation-loss-recovery.md。主线48/53、剩5，295交付状态保持；根意图与guardian交接、专业child更早准备、其他外部owner及N4-05/N7原范围继续。

2026-11-16 N4-07 根启动意图与guardian登记交接恢复已补齐：新意图保存原守护/启动登记先于业务释放的实现约定，既有准备恢复器接入Windows无原生分配及WSL原范围撤销，未登记guardian保持未知、不伪造退出。Windows/WSL意图提交、实际guardian返回但未提交、WSL取消五场景通过，三个未提交guardian由测试独立核验实际退出/关闭；同版原未确认分配及首条观察缺失恢复兼容通过。旧产物保留，新命令发布，普通WSL UID65534。共18个实际任务、2次官方DeepSeek deepseek-flash HTTP200、模拟0；292项纯凭据检查另列，83个已记录原进程身份消失。专属Redis/S3停止、临时PG删除及访问规则恢复。见 guardian-handoff-loss-recovery.md。主线48/53、剩5，295交付状态保持；专业child更早准备、其他外部owner及N4-05/N7原范围继续。

2026-11-17 N4-07 专业子执行部分启动恢复已通过：Windows首条观察缺失、created、host_ready及WSL host_ready、native_ready后取消，均使用原guardian、完整原观察链和原内核范围完成恢复；未知出生保持null，不补写缺失prepared/admission/running/stop。旧HTML/DOCX及求解场保留，新报告正文和非特权双进程400单元求解发布通过；Windows/WSL原prepared兼容通过。共35个实际任务、2次官方DeepSeek deepseek-flash模型调用、模拟0；204项纯凭据拒绝检查另列，180个记录进程出生身份独立核验退出。专属Redis/S3停止、临时PG删除且访问规则恢复。主线48/53、剩5，295项交付状态不整体提升。专业child更早分配/守护登记交接、其余外部owner及N4-05/N7原范围继续。 见 [专业子执行部分启动恢复](child-observation-loss-recovery.md)。

2026-11-17 N4-07 专业子执行分配与守护交接恢复通过9组实际场景：Windows预留/guardian返回未登记、WSL分配意图/准备/返回未确认/已分配/guardian返回后取消，以及原Windows观察缺失和WSL prepared兼容。首轮因隔离S3容量不足失败，保留六组通过；生产源码不变，在扩大测试容量的新环境补齐余下三组和基线。两轮共47个实际任务、官方DeepSeek deepseek-flash真实调用6次，模拟0、Claude 0；纯凭据拒绝检查300项另列，202个记录进程出生身份独立核验退出。最终独立检查首次201个PID不存在、1个Windows PID出生改变；该原出生在首轮清理时已核验退出，定向复查时PID也已不存在，原检查失败与补充观察分别保留，未放宽生产停止证明。旧产物保留，新报告及非特权双进程400单元求解发布通过；专属Redis/S3停止，临时PG删除且访问规则恢复。主线48/53、剩5；295项交付状态不整体提升。下一步修复已实际复现的API正常关闭遗漏终端清理，N4-07仍未整体关闭。 见 [专业子执行交接恢复](child-handoff-loss-recovery.md)。
