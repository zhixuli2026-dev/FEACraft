# N3-07 原条件验收与完成依据

2026-11-09。N3-07 按原验收条件完成：**2子域MPI和UDF现有基准可运行、取消、恢复；多工况配额和状态归属一致。** 原前置 N3-04、N4-03、N5-05 均已完成。主线更新为37/53完成、剩16项（4进行中、12待开始）。53项主线及295项全功能目标保持有效，本项完成不表示全产品交付完成。

本轮是读取当前源码与既有实际运行记录的收口审计，没有新增模型调用或原生执行，没有运行历史模拟回归。复核31份算例记录中既有的62次真实 DeepSeek HTTP200和独立工具回读，不将它们再次计为新增调用。审计入口是 `tmp/audit-N3-07-original-requirements-20261109.py`，机器可读结果为 [原条件逐条记录](../../tmp/N3-07-original-requirements-audit-20261109.json)。

## 原条件逐项核对

| 原条件/子项 | 证明范围与实际结果 | 证据入口 |
|---|---|---|
| 正式MPI求解与下游 | FS/PG和S3/PG真实2个非root rank、分解/求解/重构、400单元场与串行误差不超过1e-5；固定mesh与solve来源，明确D/E、实际提取，终止API后不重放 | [MPI桥接](mpi-workflow-bridge.md)、[专业批次](execution-storage-admission.md)；审计 `MPI_formal_*`、`mixed_professional_*` |
| MPI取消与明确恢复 | 双后端真实运行中跨API取消、原根/原生子进程停止、旧历史与输入保留；明确新工作流和400单元重算，320份附加输入完整核验 | [取消及输入](mpi-s3-input-read-fix.md)；审计 `MPI_cancel_*` |
| UDF构建及来源 | 实际wmake、CDLL、新ELF和源变化后重建；完整原始case封存与实际编译/运行查询绑定，包含缺失文件、枚举、宽目录、动态Make、响应文件和生成前缺失；V2每后端8次实际构建/求解链 | [编译来源](udf-compiler-inputs.md)、[文件查询](udf-file-queries.md)；审计 `UDF_queries_V2_*` |
| 正式UDF求解及下游 | 实际icoFoam加载固定库，400单元0.5结果；私有派生控制绑定，原mesh不被派生文件失效；重启、独立S3空缓存、D/E及实际提取、改C++新ELF、连续前缀复用且不新增Job | [UDF桥接](udf-workflow-bridge.md)、[缓存后当前兼容](private-cache-cleanup-sharing.md)；审计 `UDF_formal_*` |
| UDF编译/加载/求解取消恢复 | 当前查询V2下三阶段各真实观察、跨API取消、停止与排空、原求解成果保留；实际终止源API，在新API明确恢复构建/加载/400单元求解，旧历史不改写 | [三阶段实际证据](udf-running-cancel-recovery.md)；审计 `UDF_V2_cancel_*` |
| 类型批次与同case专业成员 | 独立case正式MPI/UDF工作流，以及同case独立run目录的MPI/UDF/串行成员；固定输入、来源当前性、缺失文件新增失效、无关变化保持fresh、半批回滚、同键幂等、新API不重放 | [类型批次](typed-workflow-batches.md)、[MPI](same-case-mpi-batches.md)、[UDF](same-case-udf-batches.md)、[串行](same-case-serial-batches.md) |
| 批次额度/归属/取消/重启 | 真实Redis重启及两worker、用户/批次上限1与2交叉、其他批次推进、跨用户拒绝；同case运行及排队取消；worker猝死后原身份核验、v4排空和旧消息拒绝；明确新三模式结果与下游 | [worker恢复](professional-worker-death-recovery.md)、[同case取消](same-case-batch-cancel-recovery.md)；审计 `typed_Redis_*`、`worker_death_*`、`prepared_Redis_cancel_*` |
| 原本地入口 | 本地BatchState改读并校验原持久spec；真实停止独立扫描器后，双后端三模式求解、各模式运行取消/其余不领取、API退出/同键重试/明确新批次、完整SQL历史保持；旧commands两个实际Python进程及用户1/批次2检查通过 | [本地批次](local-prepared-batch-delivery.md)；审计 `local_prepared_*` 及实际OS/SQL兼容记录 |
| 原S3 UDF单次409 | 找回原SQLite、Job16/17和原发布、原S3不可变命名空间；原数据副本上旧码真实WinError32/input_stale及15份同名同字节残留，修复码在真实锁下当前引用成功、释放后清理；原历史和数据均不改动 | [原始取证](original-UDF409-cache-forensics.md)、[清理修复及对照](private-cache-cleanup-sharing.md) |
| 旧API与v1/v2/v3/v4兼容 | 旧commands实际执行与幂等；原250、325、75组合的计划协议记录分别保留，不将重叠组合相加；当前计划函数/常量除queue_step外AST相同，v3重算和v4迁移模块相同；双后端实际旧argv失败、明确迁移、真实CadQuery和确认/幂等/旧历史核验 | [迁移](workflow-plan-migration.md)、[源码对照](../../tmp/N3-07-original-contract-source-audit-20261109.json)；审计 `legacy_migration_*` |

## 四条原remaining的处理

四条原文完整保存在台账 `remaining_history` 和机器审计 `original_remaining_mapping`，完成对应关系如下，没有用新条件替换原要求。

1. **MPI/UDF/批量入口桥接与恢复。** 由正式MPI/UDF生产者、独立case类型批次、同case准备成员、本地及Redis入口、运行取消和实际worker退出后的明确恢复共同证明，未用普通投递专项代替。
2. **类型批次/专业模式的完整来源策略。** 原封存保留完整原始来源，实际查询及缺失/目录观察决定后续当前性；同成员、共享输入及跨目录包含均有真实变更证据。私有运行目录独立的原约定保持，不承诺任意写者同时修改同一活动兼容文件。
3. **N2-04原全部执行模式桥接。** 既有几何/网格/串行/后处理/报告链保持；新增MPI及UDF正式生产者、固定版本来源、模式/分区/库名重算选择和D/E后续接通，类型批次使用相同注册执行契约。旧计划、v3重算和v4显式迁移保留旧历史和明确确认。
4. **原S3 UDF当前引用409归因。** 满足原清单“复现同类故障并取得底层异常、输入/对象版本及生命周期证据”的条件。结论为有效的缓存清理故障类别及修复对照；原始精确异常、持锁者或更早被清理异常掩盖的异常未保存，仍未知，未用后续一次成功推断这些细节。

## 当前源码绑定

审计核验本地批次源码包的1702个文件均与当前工作树一致。逐项比较worker猝死验收时的399个生产Python模块后，只有 `private_cache.py` 和 `batch_jobs.py` 改动；二者分别有当前原数据锁故障/正式UDF兼容和双后端本地MPI/UDF/串行实际验收。当前正式MPI/UDF处理器、注册器、队列、批次、查询来源和旧路由与实际worker恢复验收归档一致。

当前 `workflow_scheduler` 除queue_step外的全部AST和常量，与已验收UDF工作流归档相同。queue_step的批次require_open、dispatch的批次/容量接线，以及MPI/UDF处理器的prepare/source/artifacts，均已绑定到当前实际worker混合生产者及恢复后下游证据。v3重算、v4迁移、工作流状态、证据与持续派发模块保持相同。

## 其他原任务继续交付

本项原现有基准是functionObject，不替代295项中的其他UDF类型。受管工具链/系统头文件另有观察记录，完整可变运行环境闭包和部署故障矩阵仍由原N7任务交付。N4-05的全入口预算、N4-07的直接/部分原生归属和远端看门狗/接管、N5-04的证据形成前finalizing与未完成上传恢复、N6-06的刷新phase视图统一均继续，未把它们标成完成。

已有S3等待超时等未归因历史观察、负向对照和三轮本地验收脚本失败全部保留，不将后续通过改写为这些失败已修复。模型仅验证独立证据回读；自主工程规划、其他UDF类型、全部产品界面与最终295项验收不由本项代替。

## 归档

完成台账、证据摘要、原条件历史、全部源码SHA与Python AST核验见 tmp/mainline-N3-07-closure-evidence/validation.json；源码包 platform/backups/mainline-N3-07-closure-20261109.zip。
