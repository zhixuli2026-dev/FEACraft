# S3 成果发布按块归档

2026-11-19，N4-05 的 RB-12 和 RB-14 补齐类型化作业的 S3 不可变成果文件复制。成果准备复用原生作业的按块归档实现。在实际 PUT 之前，应用先按节点额度暂存文件，并核对原清单大小和 SHA-256。上传使用原不可变条件 `If-None-Match: *`，随后按块读取远端并核对原摘要。单个超过 64 KiB 的文件使用节点暂存，小文件沿用有界缓冲。S3 节点暂存的准入失败沿用已有的磁盘资源失败分类。成果意图、确认回执、发布头事务与已有恢复流程继续约束结果可见性。

首轮实际 Windows 转换命令产生 40 MiB 文件。SeaweedFS 接收了实际条件 PUT，后续远端验证与 SQL 发布额度结算完成，文件归档阶段的 Python 峰值分配为 3,635,655 B。该轮随后在等待作业终态的 180 秒窗口超时。SQL 在测试结束前记录了最终发布意图，作业由夹具退出流程取消，保留原失败证据。[阶段回执](../../tmp/typed-publication-streaming-yd0umqrt/publication-events.jsonl) · [隔离进程回执](../../tmp/typed-publication-streaming-yd0umqrt/typed-publication-streaming.json)

事后查询 SQLite 回执，源校验租约于 09:42:34.028 获取，10.875 秒后释放。该次源快照留下部分已删除的暂存目录。后续 PostgreSQL 实测在提交阶段进入同一源快照函数，Windows 删除 `B-mesh` 时返回 WinError 32。Python 3.11 的临时目录清理继续尝试文件删除，反复得到 WinError 5 与 32，最终出现 `RecursionError` 和 HTTP 500。这为首轮超时提供了同一清理路径的证据。首轮没有捕获异常栈，保留该诊断范围。[实际提交失败回执](../../tmp/typed-publication-streaming-_b2yiav0/typed-publication-streaming.json) · [异常堆栈](../../tmp/typed-publication-streaming-_b2yiav0/api.log) · [失败轮临时数据库清理](../../tmp/terminal-agent-postgres-21ebs8ak/cleanup.json)

源快照现在使用已存在的归属目录清理。每次删除前复核目标位于原暂存根内，Windows 共享冲突沿用最多三秒的重试。

完整场景使用 16 MiB 实际产物。正常转换完成成果提交，公开文件接口下载的哈希与原生作业和实际 S3 对象一致。中断组在 SeaweedFS 返回 200 后结束原 API，此时尝试仍在收尾。重启 API 使用原尝试和原成果标识。对象服务对同键条件提交实际返回 412，已存对象的 ETag 保持，验证后完成成果提交。恢复前后原生作业累计只有一次。两个场景捕获的节点暂存额度均已释放。

| 数据库 | 正常复制峰值 | 恢复复制峰值 | 证据 |
| --- | ---: | ---: | --- |
| SQLite | 2,838,678 B | 9,337,314 B | [完整验收](../../tmp/typed-publication-streaming-tdw90uuk/typed-publication-streaming.json) · [S3 已停止](../../tmp/typed-publication-streaming-tdw90uuk/s3-cleanup.json) |
| PostgreSQL | 2,885,601 B | 3,043,555 B | [完整验收](../../tmp/typed-publication-streaming-u9aoyhtl/typed-publication-streaming.json) · [S3 已停止](../../tmp/typed-publication-streaming-u9aoyhtl/s3-cleanup.json) · [临时数据库已删除](../../tmp/terminal-agent-postgres-yzf6tjyr/cleanup.json) |

两种数据库各完成一个正常作业与一个原作业恢复。四个 API 的进程身份独立确认退出，两个临时对象服务已停止。

40 MiB 复验在不可变文件校验返回、别名准备开始之前，实际打开 `B-mesh` 目录并禁止共享删除。首轮夹具探明仅访问元数据的句柄允许删除，目录列举访问才复现 WinError 32。最终夹具在实际冲突出现后释放该句柄，每组均检查原暂存目录清理和真实发布事务。正常发布与 API 突停后的恢复继续逐字节核对公开下载和 SeaweedFS 对象。

| 数据库 | Windows 实际清理结果 | 正常复制峰值 | 恢复复制峰值 | 证据 |
| --- | --- | ---: | ---: | --- |
| SQLite | 三次 WinError 32，0.312 秒完成 | 3,110,033 B | 8,561,951 B | [完整验收](../../tmp/typed-publication-streaming-y4midg3t/typed-publication-streaming.json) · [S3 已停止](../../tmp/typed-publication-streaming-y4midg3t/s3-cleanup.json) |
| PostgreSQL | 三次 WinError 32，0.297 秒完成 | 2,909,979 B | 3,535,840 B | [完整验收](../../tmp/typed-publication-streaming-ucbezl6s/typed-publication-streaming.json) · [S3 已停止](../../tmp/typed-publication-streaming-ucbezl6s/s3-cleanup.json) · [临时数据库已删除](../../tmp/terminal-agent-postgres-5uoz7ke2/cleanup.json) |

最终 40 MiB 矩阵中，不可变文件校验为 1.025–1.461 秒，别名准备为 0.507–0.590 秒。每种数据库各有一个正常发布和一个突停后恢复。每组的三个目标 PUT 结果依次为正常发布 200、原进程退出前 200、恢复条件提交 412。四个实际 API 已退出，两个隔离 S3 已停止。

峰值取自 `tracemalloc` 中不可变文件复制阶段。进程 RSS、对象服务及兼容别名路径的资源占用分别计量。复验入口为 [typed_publication_streaming_acceptance.py](../server/typed_publication_streaming_acceptance.py)。使用 `--size-mib 40 --source-sharing` 执行 40 MiB 实际共享冲突场景，加 `--postgres` 使用临时数据库。模型调用和模拟模型调用均为 0。

## S3 兼容路径的按块发布

成果的兼容路径通过 S3 文件目录记录对外可见。准备别名计划时，现在逐块核对已固定版本的源和目标摘要。应用计划前后，目录快照中的目标文件也按块核对。超过 64 KiB 的成果先进入有节点额度的临时文件，校验大小和摘要后以流写入原有条件 PUT。小文件可使用有界字节路径。原相对路径、目录快照的条件更新、原目标摘要、已删除文件的标记和结果回执仍参与应用条件。别名应用绑定已提交成果和活动发布头，恢复核对原尝试及原进程的退出证明。

本轮从正常和重启后的发布结果下载 40 MiB 公开兼容路径，校验实际 HTTP 内容与原作业 SHA-256 一致。突停点位于不可变文件实际 PUT 成功后，原作业重启恢复时继续别名应用。别名大文件 PUT 期间记录了节点暂存额度，应用结束后该额度释放。第三个实际作业在计划持久化、应用开始前由独立 S3 客户端编辑目标。应用回执记录 `ValueError`，保留用户修改，同一批次的另一个兼容路径仍未出现，两份不可变成果可读。

| 数据库 | 正常计划／应用峰值 | 恢复计划／应用峰值 | 证据 |
| --- | ---: | ---: | --- |
| SQLite | 2,504,241／3,530,034 B | 2,673,958／3,567,138 B | [真实验收](../../tmp/typed-publication-streaming-c4geh7lq/typed-publication-streaming.json) · [S3 停止](../../tmp/typed-publication-streaming-c4geh7lq/s3-cleanup.json) |
| PostgreSQL | 2,501,152／3,521,962 B | 2,529,249／3,483,417 B | [真实验收](../../tmp/typed-publication-streaming-48t4rs14/typed-publication-streaming.json) · [S3 停止](../../tmp/typed-publication-streaming-48t4rs14/s3-cleanup.json) · [临时数据库清理](../../tmp/terminal-agent-postgres-43l7a2et/cleanup.json) |

四个实际 API 均确认退出，两个临时 S3 均已停止。别名峰值为实际 Python 跟踪分配，复验包含标准 S3 文件目录路径，使用 `--size-mib 40 --alias-streaming`，模型调用与模拟调用均为 0。
