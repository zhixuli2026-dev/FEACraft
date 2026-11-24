# Windows 进程交接期间取消

2026-11-07，继续 N4-05 / RB-12 至 RB-13。此前七处准备边界的验证见[准备阶段取消](image-preparation-cancellation.md)。

## 实际缺陷与修复

在实际 Windows `create()` 已返回进程、`OwnedProcess` 尚未接回该 transport 的位置暂停交接，两台真实 API 竞争同一清理请求。连续三次取消所有者，再恢复交接。旧代码仍释放宿主、启动只读原生助手并写入 admitted。原有最终业务许可拦住删除，镜像和快照没有损失，但取消后仍推进准备工作。

[修复前失败记录](../../tmp/sandbox-recovery-ui-retirement-pzde0af0/preparation-peer-checks.json)包含真实宿主出生身份、取消前后数据库记录和已关闭的资源范围；原始模型调用为 0，尚未进入模型阶段。进程清理检查通过。此记录保留为失败，不改写为通过。旧生产代码对应 `mainline-image-preparation-cancel-20261107.zip`。

`OwnedProcess` 新增可选同步 `check_preparation_active`，镜像清理执行器将自己的停止检查传入。实际创建操作必须先完成，保留 transport 和句柄，随后检查取消；宿主握手之后、启动只读原生助手之前再次检查。抛出停止错误时继续使用既有 abort、guardian 退出和原生范围撤销流程，不丢失已创建进程的清理责任。其他调用者默认不提供此检查。

## 验证范围

显式真实模型入口仍为 `live_llm_image_preparation_peer_acceptance.py --source-profile-id 26 [--postgres]`。当前矩阵为九处：原七处加 `host_transport_return` 与 `native_handshake`。

- `host_transport_return`：真实进程已在选定 Windows Job 中，重复取消后不启动并登记原生助手，实际宿主退出。此处覆盖异步 transport 交接窗口；没有声称能在同步 `CreateProcessW` 系统调用内部投递同一事件循环的取消。
- `native_handshake`：已经发出原生许可且收到实际握手，在握手结果交给准备代码前暂停。原生范围实际 populated，数据库仍没有 native_ready。重复取消后保留实际出生、停止与范围关闭证据，最终删除业务不得释放。此处没有暂停原生进程内部“许可已读、握手未发”的窗口。
- 两台 API 在所有者存活及取消排空期间均须拒绝接管；最终重试只完成一次删除，原始 children/result 不改写，恢复快照保持原字节。最终使用真实 DeepSeek 自主选择读取工具并回复随机验证字符串。

SQLite 九处验收已通过：[矩阵证据](../../tmp/sandbox-recovery-ui-retirement-hsukubt9/preparation-peer-checks.json)、[真实模型](../../tmp/sandbox-recovery-ui-retirement-hsukubt9/live-result.json)。共 10 项实际基础设施检查与 1 项真实模型读回检查，实际 DeepSeek 调用 2 次、8,444 tokens、模拟 0；严格原样回复通过。13 个已登记进程出生均实际退出，另单独核验未登记的 Windows 交接宿主退出。

PostgreSQL 九处验收也已通过：[矩阵证据](../../tmp/sandbox-recovery-ui-retirement-5oy3nnzt/preparation-peer-checks.json)、[真实模型](../../tmp/sandbox-recovery-ui-retirement-5oy3nnzt/live-result.json)、[临时数据库删除回执](../../tmp/image-postgres-51t1s9c3/result.json)。同样为 10 项实际基础设施与 1 项模型读回检查，真实调用 2 次、8,570 tokens，严格原样回复通过。

本轮双库合计 20 项实际基础设施检查、2 项真实模型验收；实际推理 4 次，全部 HTTP 200，共 17,014 tokens，模拟 0。26 个已登记进程出生均退出，两个未登记的交接宿主另有实际退出观测。七条未完成登记通过独立对账；native_handshake 和 native_ready 保留实际停止凭证。既有退休基础算例内的权限和损坏注入单独归为协议故障，不计入这里的 20 项基础设施或 4 次模型调用。历史模拟集合未执行，审查门禁未放宽。

## 继续项

原生许可接收至握手发出之间的内部交接、助手/guardian 期限与异常死亡、错误许可、全请求期限、WSL/宿主重启及真实磁盘/权限故障仍待。此前偶发退出核验拒绝与命令时延未因此标记修复。

主线 33/53 完成、剩余 20；资源 11/17 完成、剩余 6 块。295 项全功能交付目标不变。新增边界验证属于尚未完成大项内的推进，不降低剩余计数。
