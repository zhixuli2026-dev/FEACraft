# 原生追踪额度的统一失败分类

2026-11-12 UTC。修复N4-05中的真实缺口，主线48/53完成、剩5项；资源工作清单11/17、剩6块，未将本修复算成整项完成。

## 原问题与实现

N7-04实际MPI100输出场景触及原64MiB追踪额度时，监视器会停止工具并记录已观测字节，但跨原生助手、宿主worker传递后，作业只显示process_exit_failed。用户无法区分资源限制与求解器本身失败。

case_file_queries现用专用TraceBudgetExceeded退出保留原生助手退出码98，并记录实际观测和原64MiB/1024文件上限。工具自身退出仍由原助手转换成普通执行错误，不把任意工具98当作助手超限。monitor以及结束后的collect均保留额度约束；没有扩大额度、取消追踪或修改数值条件。SystemExit仍执行原生finally清理。

native_resource_failure只读取已登记处理器及绑定到原attempt/parent的实际停止回执；MPI/UDF使用指定purpose的已停止worker子范围，prepared serial使用根停止证明。普通shell、相似日志、其他处理器或未核验停止不能提供资源分类。jobrunner在失败且尚无其他失败原因时写入resource_limit，早期收尾记录和最终attempt均保存该原因；取消/中断及原有明确失败原因优先。停止、归档和成功发布规则沿用原实现。

## 实际基础设施验收

| 场景 | 证据 | 结果 |
| --- | --- | --- |
| 原1600单元、2 rank、100输出、64MiB追踪额度 | `../../tmp/workflow-mpi-retry-FS-SQLite-b427zgjg/professional-result.json` | 实际运行取消后按封存重试；超限作业及工作流failed/resource_limit，原生助手退出98，根和子范围停止通过 |
| 同物理条件、10输出正常对照 | `../../tmp/workflow-mpi-retry-FS-SQLite-6mfz8qb5/professional-result.json` | 实际取消/重试、有限非零U/p、明确D确认及原历史保留通过 |
| 普通Windows命令实际退出98/7/0，并打印相似超限文字 | `../../tmp/trace-resource-controls-smneqjdz/acceptance.json` | 98/7仍为process_exit_failed；0仍成功；全部实际停止 |

超限时允许持久化失败发布记录作为历史，但没有新增committed成果，当前成果指针不变，幂等重试不增加作业。首轮`../../tmp/workflow-mpi-retry-FS-SQLite-gumga2r_/professional-result.json`已正确产生resource_limit和真实停止证明，却因测试错误要求“完全没有新发布行”失败；保留该失败，后续改为核对失败历史与成功发布的实际区别。不能把失败发布记录删掉来满足测试。

包括首轮失败，共18个真实原生Job，4个实际API均已退出。首轮及最终超限的early finalizing intent/result和最终attempt均已独立读回resource_limit。UDF/prepared serial共享分类代码已实现，但本轮未声称对它们新跑完整超限工程场景；全入口资源验收仍由RB-13及N7-01覆盖。

## 纯协议检查与模型分类

`../../tmp/N4-05-trace-receipt-audit-20261112.json`只读实际MPI数据库，并在内存副本做3项负面验证：删除注册处理器不得分类、替换parent nonce拒绝、停止标记缺verified拒绝。明确属于元数据协议检查，不等于3次原生执行，不修改数据库。

本增量真实模型0、模拟LLM0：测试对象是原生资源失败，与推理无关。上一N7-04阶段的2次真实DeepSeek调用仅作为既有证据，不重复记为本轮。后续涉及Agent/模型回复仍必须调用真实服务。

## 剩余范围

不改变全部53主线及295全功能目标，不关闭N4-05。RB-12至RB-17原剩余范围仍有效；下一步优先核对长文件入口、native/CLI完整资源生命周期及全局并发已有证据，再补真实缺口。其他主线剩N4-07、N7-01、N7-05、N7-06。源码和导入辅助按显式审查登记，旧模拟LLM集合未运行。
