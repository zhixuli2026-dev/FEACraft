# 发布恢复期间的磁盘监控

后续更新：不可变成果复制现已接入持久用户额度预留及写入进程排他；详见[成果复制存储预留](publication-storage-admission.md)。

2026-11-06，N4-05 增量。主线仍为 33/53 完成，20 项未完成。本轮衔接[执行磁盘监控](execution-storage-monitoring.md)，补上 API/worker 退出后发布恢复的监控断点。

## 恢复行为

已经完成复制的发布验证，以及复制中断后的继续复制，现在都会在恢复前根据原尝试的 `storage_policy` 创建 Watch。重新启动节点的配置即使更宽松，也不会提高原尝试的目录额度。

复制前已超额或本地目录无法确认时，直接按 `resource_limit` 提交失败，不调用复制/验证入口。复制过程中观察到超额时，等待真实线程结束并记录已有复制排空证明，再提交失败。复制、验证与终态提交沿用同一个 Budget，避免重新加载后丢失刚发现的违规。恢复不会重新启动原生构建工具。

预复制失败仍使用原有 `attempt.bundle_preparation_expired`，明确 `copy_resumed=false`、`storage_reclaimed=false`。已经复制并排空的路径继续记录 `attempt.bundle_copy_drained` 和原有 v3 checkpoint；没有将“额度耗尽”误写成“复制未开始”，也不声称失败文件已经释放。

## 新节点与未知状态

恢复时 `attempt.storage_usage.scope` 为 `recovery_node`。它描述当前恢复节点观察到的文件字节，不能替代原执行节点未持久化的历史峰值。

- S3 新节点尚未建立尝试缓存时，允许在校验目录路径后记录 `state=absent`、`root_present=false` 和本节点当前字节 0；仍检查本节点磁盘余量。后续首次实际建立缓存时绑定目录身份，之后目录丢失或被替换会触发失败。
- 本地 FS 恢复要求原尝试目录存在。丢失时记录 `state=unverified`、`current_bytes=null`、`root_present=null`，按未知用量失败，不按零继续。
- 临时目录删除仍使用有界完整重试；无法确认的扫描停止恢复。取消恢复会等待正在进行的盘点/复制 I/O 排空。

原执行 Watch 的事件现在同样明确 `scope=execution` 和目录存在状态。这是观测范围说明，不是新的全局磁盘额度。

## 验收范围

`recovery_storage_smoke.py --postgres` 在 SQLite/PostgreSQL 各验证 8 个场景：prepared/prebundle 正常恢复、恢复前超额、恢复中途超额，以及原本地目录丢失。

`--postgres --s3` 在两库各验证 3 个场景：全新缓存节点的 prepared 正常恢复、恢复前超额、恢复中途超额。它没有宣称 S3 新节点可以在缺少原生成输出时重新执行构建或恢复所有未完成复制。

测试使用真实 API 进程退出、真实报告产物、真实 S3 服务和实际文件增长；核对原额度、失败发布、排空证明、唯一尝试和持久用量。进程终止命令返回后继续轮询原进程出生身份，只有确认退出才启动恢复。测试中的数据库均为隔离临时库，开发数据库未改动。

原恢复期限兼容检查覆盖 prepared/prebundle 正常恢复，以及验证/复制期间期限耗尽；原运行监控兼容检查覆盖正常、未知用量和 WSL。详细回执：`../../tmp/mainline-recovery-storage-evidence/validation.json`。

## 后续交付

下一单元是复制/归档的分配前峰值准入和持久占用。此次仍是检查和周期监控，允许采样间隔内短暂超额，不是 OS 磁盘硬配额，也未为原生进程未来写入预留空间。

历史尝试缺少冻结策略、S3 未完成复制缺少原生成文件、旧 staging/独立 native request/CLI 生命周期、目录外写入限制、长 I/O 锁与心跳、跨节点并发和缓存安全淘汰仍需继续。全部 53 项主线与 295 项功能的交付范围不变。
