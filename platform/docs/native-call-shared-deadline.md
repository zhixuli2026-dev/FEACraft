# 原生调用共享截止时间

2026-11-07，N4-05 / RB-13 至 RB-14。本批修复原生客户端、执行队列、准备与业务运行各自计时造成的期限刷新，并保留超时后的资源责任。上游完整 HTTP 请求预算仍继续推进。

## 实际缺陷与实现

1. `native_execution_client.run_native` 原先对每次 socket I/O 设置 `timeout + 90`。持续输出或逐字节发送可以刷新等待。[修复前实际 TCP 复现](../../tmp/native-deadline-lmisbaqb/result.json)中，0.6 秒调用等待到服务端断连，报告 ChildExecutionError，而非按总期限超时。新客户端在调用开始计算单调截止时间，每次连接、发送、实际 recv 和处理帧前都检查剩余时间；取消 buffered readline，避免其内部多次读取刷新计时。分片帧和大小限制保留。
2. 私有 native-call v1 增加可选 `deadline`。服务端保留旧格式，并验证新字段为有限正数；实际截止时间取客户端、服务端配置和已有执行预算中的最早值。该字段用于共享宿主单调时钟的本地调用，不写成可跨重启重放的持久时刻。
3. `NativeExecutor` 在排队前确定期限，等待槽位也受限。期限到达设置 entry 停止事件；准备、身份核验、登记及最终许可前重新检查，业务运行继续使用同一个截止时间。已收到的准备身份先保留再拒绝推进。清理有自己的排空责任，不通过刷新业务期限来继续运行。
4. 实际预留提交可能早于调用者收到返回。预留操作和 `reserved` 责任标记放在同一个等待排空的协程内，直接取消也不能漏掉已提交预留。普通原生调用及镜像清理的准备检查统一使用 entry 截止时间。
5. [实际排队超时暴露的 HTTP 500](../../tmp/sandbox-recovery-ui-retirement-t1hfwhl1/native-deadline-checks.json)来自 `subprocess.TimeoutExpired` 未进入上层 TimeoutError 处理。`RequestExecutor.run_sync` 先停止并等待执行器及同步线程，再转换为 TimeoutError，镜像接口返回结构化 409 未完成结果。
6. [预留提交后中断的恢复缺口](../../tmp/sandbox-recovery-ui-retirement-ns2f9u3c/native-deadline-checks.json)：预留被正确终结，但没有启动证据，后续重试被保守拒绝。新增 `terminal.image_launch_unstarted`，只在仍持有实际未使用的 OwnedProcess、没有任何进程/守护/范围/启动记录时写入。凭证绑定请求、子项、用户、nonce 和实现哈希，封闭该预留的后续 plan/reserve。独立对账可识别该明确凭证；不为历史缺证据记录推断从未启动，不伪造正常停止回执。

首次 API 测试入口把函数导入位置写错，启动失败于调用模型前；保留[失败记录](../../tmp/native-deadline-wus0zw0q/result.json)。随后修正为包装实际 native client 导入点。以上失败均未改写为通过。

## 验证

入口 `live_llm_native_deadline_acceptance.py --source-profile-id 26 [--postgres]` 先执行实际 loopback socket 协议检查，再执行实际双 API、数据库、Windows/WSL 镜像场景和真实模型读回。协议字节是原生通道诊断流，不是模型替代回复。

六个实际期限位置为 queue、reserve_return、allocated、host_transport_return、native_ready 和 pre_release。排队/预留使用 8 秒，其他位置使用 20 秒。预留提交场景还实际取消底层执行协程三次，必须完成责任登记和终结。排空期间原请求保持 active、另一 API 返回 409；排空后原镜像和快照保持完整。额外篡改 unstarted 请求绑定必须拒绝恢复，恢复原始凭证后才允许继续。最后双 API 重试只清理一次，失败的 children/result 不被改写。

SQLite 最终六位置已通过：[矩阵](../../tmp/sandbox-recovery-ui-retirement-1rqaif5d/native-deadline-checks.json)、[传输与整体结果](../../tmp/native-deadline-a813g54m/result.json)、[真实模型](../../tmp/sandbox-recovery-ui-retirement-1rqaif5d/live-result.json)。7 项基础设施、1 项凭证损坏协议检查及 1 项模型验收；实际模型 2 次、8,434 tokens，严格原样读回通过。0.6 秒 TCP 总期限的连续输出、逐字节场景分别约 0.594 / 0.609 秒结束；正常分片成功。

另有独立真实模型工具测试 `live_llm_native_shell_deadline_acceptance.py --source-profile-id 26`：[实际结果](../../tmp/live-shell-deadline-mtju5vew/live-result.json)。DeepSeek 自主选择一次 PowerShell 工具调用，12 秒上限，实际命令睡眠 25 秒。准备阶段真实等待 5 秒，放行后只剩约 6.828 秒，到期停止后约 0.609 秒完成清理。真实 Windows 进程退出且停止凭证 verified，最终模型如实报告 TimeoutError，未重试。实际模型调用 2 次、9,446 tokens；没有模拟 CLI 或模型工具选择。

PostgreSQL 最终六位置也通过：[矩阵](../../tmp/sandbox-recovery-ui-retirement-edfx769a/native-deadline-checks.json)、[传输及整体结果](../../tmp/native-deadline-3962662l/result.json)、[真实模型](../../tmp/sandbox-recovery-ui-retirement-edfx769a/live-result.json)、[临时库删除](../../tmp/image-postgres-2el8lij4/result.json)。同样 7 项基础设施、1 项凭证损坏协议检查及 1 项模型验收；真实模型 2 次、8,436 tokens，严格读回通过。

最终双库矩阵合计 14 项实际基础设施、2 项凭证损坏协议检查、2 项真实模型验收；另有两套共 6 项实际 TCP 协议检查和上述真实模型 Shell 场景。全批模型推理共 6 次，全部 HTTP 200，26,316 tokens，模拟 0。矩阵 26 个登记出生和独立 Shell 宿主均核验退出，未登记的交接宿主另有逐案退出观测。统计只列最终验收，不重复计入此前失败尝试中已通过的协议检查；所有失败保留。历史模拟集合未运行，审查门禁未放宽。

## 未完成范围

此次截止时间覆盖一个原生调用。上游 HTTP 请求在进入 native call 之前的归档读取、存储准入、数据库锁、配置准备，以及之后的最终提交，尚未全部共享这一预算。同步 sink、不可中断线程和已提交写入必须排空；“到期停止新的执行许可”不等于“到期瞬间所有清理已完成”。后续继续有界 I/O、全请求预算与跨 worker 全局额度。

历史无 unstarted 凭证的缺记录仍按原规则拒绝自动恢复。此前偶发退出核验拒绝/宿主命令时延、原生门后续 330 秒期限、WSL/宿主重启和真实空间/权限故障继续保留。主线 33/53、剩 20；资源 11/17、剩 6；全部 295 项最终交付要求保持。
