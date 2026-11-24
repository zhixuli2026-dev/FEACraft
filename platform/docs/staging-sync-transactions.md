# 旧 S3 同步接入文件事务

后续状态：第十六批已修复旧作业批量同步持锁及旧收尾 I/O 夹具，见[同步心跳与取消](staging-transfer-liveness.md)。下文保留第十五批验收时的边界；交互同步、元数据请求和补偿恢复的长时锁问题仍未关闭。

2026-11-05，N2-06 第十五批。终端、可视化、Agent Shell、CLI 会话以及旧作业的 staging 同步已调用 `staging_sync.sync` / `sync_job`，复用现有 S3 文件事务日志与 SQL 提交证明。

## 行为与边界

同步先验证副本身份、来源算例、清理标记和路径，再计算新增、修改和删除。未变的本地文件不回写，也不会删除副本建立之后远端新增的文件。源内容以快照摘要为前提；即使恢复旧内容后的 ETag 与原多段上传 ETag 不同，也可以继续同步。事务会重新取得当前对象版本，并在实际上传、删除时使用条件请求，阻止预检之后的覆盖竞争。

变更前后内容与元数据保存到远端不可变事务对象，pending 记录和 SQL 事件沿用既有恢复协议。SQL 未提交时按证明恢复旧内容，已提交时保留新内容，外部字节或元数据发生变化时保留现场并返回恢复冲突。空缓存的新 API 可以恢复已发布的事务日志。调用方在数据库提交后才开始删除本地副本；同步失败时保留副本。

交互入口保留原授权和算例协调锁。旧作业额外验证当前 worker 所属、算例绑定和收尾阶段，已终结或不处于收尾阶段的作业不能继续同步。文件提交和旧作业最终终态仍是两个提交边界，后续 N5 恢复要求没有因此取消。

大批量同步使用独立 `staging` 事务范围：100000 个文件、64 MiB 清单，文件大小不沿用普通保存的 1 GiB 上限；实际数据逐文件流式准备。普通保存仍保留原 256 文件、1 GiB 单文件和 2 GiB 含备份总量限制。目的路径冲突检查改为检查路径祖先，避免扩大文件数量后出现两两比较的成本。300 文件和缩小普通额度后的大文件测试已通过；未实际传输超过 1 GiB 的文件。

旧 `storage.sync_job_staging` 仅保留为低层兼容函数，也已改为先验证后端条件请求能力、再逐项条件写入/删除；它本身没有 SQL 提交证明。代码搜索确认产品的五类调用入口均已迁移，不再直接调用它。

## 验收证据

| 验收 | 结果 | 原始证据 |
| --- | --- | --- |
| 真实 S3 + SQLite：正常、部分写入、提交前/后退出、写入竞争、响应丢失、300 文件 | 7 组通过 | `tmp/staging-sync-evidence-d1mb6oug/checks.json` |
| 真实 S3 + PostgreSQL：部分写入、提交前/后退出、写入竞争 | 4 组通过，临时库已删除 | `tmp/staging-sync-evidence-2ad123ps/checks.json` |
| 真实旧作业执行、同步并清理副本 | 1 组通过 | `tmp/staging-sync-evidence-ieeipumr/checks.json` |
| 缩小普通文件限额，验证 staging 独立容量范围 | 1 组通过 | `tmp/staging-sync-evidence-fktxg8k1/checks.json` |
| 同内容、不同原始 ETag 的恢复后同步 | 1 组通过；旧 ETag 由夹具模拟 | `tmp/staging-sync-evidence-vzqcz9zz/checks.json` |
| 终端、Agent Shell、CLI 和可视化原关闭/冲突/取消/清理矩阵 | 各 9 项通过；真实原生进程，文件系统 S3 夹具 | `tmp/terminal-staging-evidence-g57i7mhf/`、`tmp/agent-staging-evidence-5f30qv30/`、`tmp/codex-staging-evidence-_6o0_rqg/`、`tmp/viz-staging-evidence-4bn790yg/` |
| 原真实 S3 作业/重试/WSL/取消/损坏保护 | 14 项通过 | `tmp/s3-attempt-store-cs1t5yyn/attempt-result.json` |
| 原普通 S3 文件事务：正常、部分写入、删除、元数据 | 4 组通过 | `tmp/s3-file-transactions-evidence-bemfu668/checks.json` |
| 作业最终提交与错误处理 | 8 项通过 | `tmp/job-finalization-qqtc8p39/result.json` |
| 低层兼容合并、冲突、重试和清理 | 5 项通过；可控 S3 | `tmp/N2-staging-low-level-final.log` |

通过的定向项/组共 81。第一次竞争测试把 409 错误写成预期 500，修正夹具后整组通过。低层兼容函数最初缺少 `ClientError` 导入，已修复并通过原五项。旧最终提交测试通过已不接受裸 Job 的 Redis 入口启动夹具，现改为直接运行器等待真实提交；它不再宣称验证 Redis 派发，派发仍由原独立测试覆盖。

## 未关闭的问题

`job_finalizing_io_smoke.py` 全量运行在第一个旧求解样例失败：假 `U` 文件没有 OpenFOAM 头部，被当前真实发布校验拒绝，尚未执行到同步阶段。此失败记录在 `tmp/N2-staging-job-io.log`，不能计作通过，夹具仍需按当前契约修复。

同步当前在算例事务锁内完成。大批量/慢存储同步对作业心跳、取消及 SQLite 写锁的影响需要下一批专门复现、处理；本批的 300 文件通过不证明长时同步响应性。优先核对此项，不能因为数据恢复测试通过就关闭 N4/N5 的收尾时序要求。

准备日志发布之前的中断残留、未完成会话的恢复入口、缺证明数据诊断/隔离、已提交缓存重建、索引对账、原 UDF 409 与完整跨主机部署范围继续保留。主线仍为 23/53 完成、剩余 30；295 项严格整项交付仍为 0，本批不关闭 N2-06。
