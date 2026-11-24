# 可视化输入准备的期限与算例锁

S3 可视化以前在算例写锁和 API 的会话登记锁内复制整个算例。复制期间，同一算例的常规文件写入也要等待。现在先在短事务中记录算例存储前缀、字典版本和事件游标，再释放事务与账户额度，按原有 ETag 和节点额度校验下载。登记前重新鉴权，比较记录的来源，同时在数据库锁下复核用户和算例的会话配额。源变化后返回 `409 / input_stale`，用户重新打开即可取得新副本。[可视化路由](../server/app/routers/viz.py) · [S3 版本固定下载](../server/app/services/staging_download.py)

准备期限默认为 300 秒，由 `VIBEFLOW_VIZ_PREPARE_TIMEOUT_SECONDS` 配置，覆盖算例检查、锁等待、下载和登记。时限到达后等待已开始的文件 I/O 结束并清理，随后返回 `504 / viz_preparation_timeout`。每个 API 进程同时接纳两个 S3 可视化准备请求，繁忙时返回 `429 / viz_preparation_busy`。准备名额保留到本次启动或收尾结束。渲染进程启动后的就绪监测、心跳、停止证明和关闭同步继续使用已有机制。若登记阶段退出，使用新的数据库事务确认记录已提交，再选择持久会话关闭或未登记副本清理。[配置样例](../server/.env.example)

来源校验按整个算例的事件游标执行。复制期间发生其他算例事件也可能需要重新打开，确保登记不会使用变化前的副本。未登记暂存目录遇到 API 猝死后的维护，以及对长期运行会话的统一资源限制，仍属于原 RB-13、RB-15 等任务范围。

## 实际验收

两个 S3 组合暂停真实复制，在旧副本尚未交付时完成同一算例的正常 HTTP 保存。释放旧副本后返回 409，未创建渲染会话。随后同时暂停两次真实文件下载，两次超过期限时仍占用实际节点预留和准备名额，第三次立即得到 429。释放文件线程后，两次均返回 504，暂存目录、预留和会话登记数均为零。正常重试启动实际 VTK/trame，页面可读，关闭后核验进程停止证明且没有剩余暂存或节点预留。[测试入口](../server/live_llm_viz_preparation_acceptance.py)

| 部署 | 基础设施证据 | 真实模型回读 |
| --- | --- | --- |
| SQLite 与 S3 | [输入准备、故障与恢复](../../tmp/live-cli-startup-agth3epm/viz-preparation.json) | [2 次 DeepSeek HTTP 200](../../tmp/live-cli-startup-agth3epm/live-result.json) |
| PostgreSQL 与 S3 | [输入准备、故障与恢复](../../tmp/live-cli-startup-1b309bf5/viz-preparation.json) | [2 次 DeepSeek HTTP 200](../../tmp/live-cli-startup-1b309bf5/live-result.json) |
| PostgreSQL 与本地文件系统 | [双 API 会话争用、退出证明和名额恢复](../../tmp/live-cli-startup-jpem2ikn/viz-slots.json) | [2 次 DeepSeek HTTP 200](../../tmp/live-cli-startup-jpem2ikn/live-result.json) |
| PostgreSQL 与 S3 | [双 API 会话争用、退出证明和名额恢复](../../tmp/live-cli-startup-jf0i_3hl/viz-slots.json) | [2 次 DeepSeek HTTP 200](../../tmp/live-cli-startup-jf0i_3hl/live-result.json) |
| PostgreSQL 与 S3、Edge | [7 组网页入口检查，含 Gmsh、VTU 和根目录 OpenFOAM 的实际画布](../../tmp/live-cli-startup-1fsd2kqj/project-file-result.json) | [2 次 DeepSeek HTTP 200](../../tmp/live-cli-startup-1fsd2kqj/live-result.json) |

五组共获得 10 次真实 `deepseek-flash` HTTP 200，模拟调用为 0。模型读取验收摘要，网格和画布由实际可视化进程生成。隔离 API 均已退出，临时 PostgreSQL 库已删除，对象存储已停止。[整组执行日志](../../tmp/viz-preparation-20261119)

## 2026-11-19 独立渲染会话的运行预算

`VIBEFLOW_VIZ_SESSION_LIFETIME_SECONDS` 控制已放行渲染进程的时限，`VIBEFLOW_VIZ_SESSION_MAX_OUTPUT_BYTES` 控制单会话累计管道输出，`VIBEFLOW_VIZ_SESSION_WORKSPACE_LIMIT_MB` 控制本地工作目录。数值 0 沿用原运行配置。同一会话使用启动时选定的额度。文件系统直接采样实际算例目录，S3 采样渲染进程的独立本地副本。启用目录限额时，同步检查现有 `VIBEFLOW_NODE_STORAGE_MIN_FREE_MB`，卷余量或目录用量不符合时在放行前拒绝。[配置样例](../server/.env.example) · [会话实现](../server/app/services/viz_sessions.py)

运行期间复用现有有界目录采样和启动租约。累计输出超限时单次写出只保留限额内的字节。时限、累计输出、目录用量和无法验明目录分别使用 `timeout`、`log_limit`、`disk_limit`、`disk_unverified`。触限产生 `VizResourceLimit` 并走同一个原生范围停止与持久回执流程。已启动原生进程的会话通过停止证明释放运行名额。每轮采样间隔使用已有执行存储配置，目录限制按实际采样结果触发。[目录采样](../server/app/services/execution_storage.py) · [验收入口](../server/viz_runtime_budget_acceptance.py)

SQLite 分别连接实际文件系统和认证 SeaweedFS。两个组合各验证六项。把真实 VTK 启动输出限制在 128 字节时，日志正好保留 128 字节，启动返回 409。初次允许 1 MiB 工作目录的会话已启动，实际写入 2,097,152 字节后停止，随后通过正式下载核对原件。带同一大文件再启动立即返回 409，持久记录显示未启动原生进程。启动实际渲染页面后，已选定的 15 秒期限保持生效，即使期间改了默认配置。三种触限均持久记为失败且原生范围为空，随后可以正常重新打开，同库冷重启可读回原停止原因及回执。[文件系统](../../tmp/viz-runtime-budget-fs-h803e46w/viz-runtime-budget.json) · [认证 SeaweedFS](../../tmp/viz-runtime-budget-s3-ewu20km9/viz-runtime-budget.json)

调整准备期心跳顺序后，两个组合按当前源码复验通过。两轮共核查 16 个原生范围与 28 个实际进程身份，四个隔离 API 端口及两个对象存储的 16 个端口均关闭。真实模型调用及模拟均为 0。[原生退出](../../tmp/viz-runtime-budget-s3-ewu20km9/native-exit-proof.json) · [独立监听核验](../../tmp/viz-runtime-budget-s3-ewu20km9/listener-exit-proof.json)
