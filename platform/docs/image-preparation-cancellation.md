# 镜像清理准备阶段取消与双 API 竞争

2026-11-07。实际复现了取消后的迟到原生许可：在 `host_ready` 持久记录后暂停准备回调，连续三次取消所属 HTTP 请求，恢复回调后旧代码仍发送原生许可并记录 `native_ready`。最终业务释放检查阻止了镜像删除，但取消后仍启动了下一阶段工作。

[修复前记录](../../tmp/sandbox-recovery-ui-retirement-qlhd0kus/preparation-peer-checks.json)保留失败断言、取消前后原始身份/范围/启动记录，模型调用 0。该复现调用真实 API、Windows/WSL 进程和数据库，没有模拟模型。对应旧生产源码见 `mainline-native-receipt-sync-20261107.zip`；执行当前源码的同一入口会验证修复后的行为。

## 实现

- `native_request_execution.py` 为镜像清理准备提供同步取消检查：核对 entry 停止事件及执行器许可。持久保存已观察到的准备身份后再次检查，阻止迟到回调继续发送原生许可。
- `sandbox_image_scope.py` 在计划提交后、控制准备登记后、实际分配回执提交后、创建守护前及原生预登记提交后检查取消。已经实际获得的分配回执先保存，再拒绝推进，以便 finally 撤销精确分配，不遗失资源证据。
- `native_scope_control.py` 接收同一同步检查，在创建控制进程前、释放控制宿主前，以及最终身份复查之后、发送分配许可之前执行。检查与许可写入之间没有额外异步等待。
- 保持既有取消后的排空、范围撤销、独立对账和最终业务释放检查。已有身份与正常停止记录不因重试被重写。旧守护测试包装器同步支持新增关键字参数，未绕过回归审查登记。

## 实际验证

显式入口为 `live_llm_image_preparation_peer_acceptance.py --source-profile-id 26 [--postgres]`；`--stage host_ready` 可单独执行最初的复现场景。

两台实际 API 共用隔离数据库和存储，轮换成为准备请求所有者。每个位置暂停实际准备回调、让另一台 API 尝试清理、连续取消三次并再次竞争，再恢复原回调完成排空。

| 位置 | 取消后验证 |
| --- | --- |
| planned | 不再启动分配准备，prepared 为空 |
| allocation_prepared | 已登记真实控制进程，但不发送分配许可，allocated 为空 |
| allocation_permit | 最终身份复查之后仍检查取消，allocated 为空 |
| allocated | 保留并撤销实际分配，不启动业务预登记 |
| admitted | 保留原生出生身份，不继续 created/host_ready 回调 |
| host_ready | 不发出原生许可，没有 native_ready |
| native_ready | 原生范围已加入，实际排空，最终删除业务不释放 |

每个位置都要求所有者尚活时另一 API 返回 409；取消排空完成前请求保持 active，不能提前接管。排空后镜像与快照仍在，范围有关闭凭证；已知业务宿主和原生身份实际退出。最终重试完成一次镜像清理，两台 API 重复请求返回同一结果，原始 children/result 不改写。六条未完成登记通过独立对账接管；native_ready 的正常停止凭证直接保留，不补造独立对账。实际校验与管理准入恢复。

- [五位置中间验收](../../tmp/sandbox-recovery-ui-retirement-bv9ix32o/preparation-peer-checks.json)：6 项基础设施与 1 项模型读回检查通过，2 次真实 DeepSeek、8,354 tokens。仅作中间版本证据。
- [SQLite 七位置最终验收](../../tmp/sandbox-recovery-ui-retirement-ra3gz8wp/preparation-peer-checks.json)：8 项基础设施与 1 项模型读回检查通过，2 次调用、8,534 tokens；[模型证据](../../tmp/sandbox-recovery-ui-retirement-ra3gz8wp/live-result.json)。
- [PostgreSQL 七位置最终验收](../../tmp/sandbox-recovery-ui-retirement-x_ul2i78/preparation-peer-checks.json)：8 项基础设施与 1 项模型读回检查通过，2 次调用、8,354 tokens；[模型证据](../../tmp/sandbox-recovery-ui-retirement-x_ul2i78/live-result.json)、[临时数据库已删除](../../tmp/image-postgres-729ssk7n/result.json)。

最终双库合计 16 项本矩阵的实际基础设施检查。包含中间验收时，本轮共 6 次真实 `api.deepseek.com / deepseek-flash` 推理、25,242 tokens，模拟 0；三套模型都实际选择工具读取恢复文件，源内容未修改，消息持久化，严格纯字符串也通过。基础退休算例原有的权限/损坏注入仍是协议故障，未计为模型或新并发证据。

## 未完成范围

本轮没有覆盖宿主创建调用内部或许可已发出后尚未收到握手时的跨 API 取消、原生助手/守护期限与异常死亡、错误许可、WSL/宿主重启、真实磁盘/权限故障以及整个请求的统一资源期限。此前偶发退出核验拒绝和命令时延问题仍待定位。本次正常运行不构成对这些问题的修复证明。

主线仍 33/53 完成、剩余 20 项；资源 11/17 完成、剩余 6 块。295 项全功能最终验收要求保持，继续按[主线计划](next-stage-mainline-plan.md)和[资源清单](resource-budget-worklist.md)推进。
