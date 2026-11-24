# WSL 求解取消与发布后的迟到取消

2026-11-18。按主线故障矩阵 V12 和 V10 核验。

| 场景 | 实测结果 |
|---|---|
| V12，运行中的 WSL/MPI 求解 | 在 Windows API、临时 PostgreSQL 和文件存储上，观察到 2 个非 root 的 icoFoam rank，再从另一个 API 取消。8 个已记录的原进程身份均验证退出。原控制文件内容保留，320 个参考输入摘要在新旧工作目录一致。相同 attempt 的持久事件 174、189、194、200、201、202 依次记录运行、取消中、子进程停止、写入排空、根进程停止、取消终态。作业日志 API 返回的 case、job、attempt 与事件一致，完整日志包含求解输出并以详情中的日志片段结尾。 |
| V10，已发布求解结果 | 明确重算后，两个 rank 求解到 0.5，生成 400 个速度向量。提交重算的 API 退出后，从新 API 对成功作业 6 发出取消。API 返回 `ok: false`，同一次尝试、发布清单、工作流成功状态和速度场字节均保持一致。速度场 SHA-256 为 `14ca9cb6370c0c0de497c3ae1aee9b1516c6a886a46182f8e751a10537c7a2a2`。 |
| V10，浏览器点击和后台结束发生竞争 | Edge 已发送取消请求时暂缓传输，让真实命令结束，再放行原请求。后端返回 HTTP 200 和 `ok: false`。修复前代码把所有 HTTP 200 显示为“已受理”。现在页面显示“作业不在运行中（succeeded）”，原日志保留 `ORIGINAL_RESULT`。批次接口也按其 `ok` 字段显示结果，本轮浏览器覆盖单个作业。 |

FS/PG 三轮通过，每轮进行了两次 api.deepseek.com / deepseek-flash HTTP 200 回读，模型读取实际验收摘要。故障注入、进程退出核验、数据库事件和浏览器状态由程序直接观测。浏览器竞态验收未调用模型。

- [同一次 FS 作业的事件与日志摘要](../../tmp/live-cli-startup-g60_95h4/mpi-cancel-events.json)、[原生进程身份、重算和迟到取消](../../tmp/live-cli-startup-g60_95h4/mpi-cancel-recovery.json)、[模型调用](../../tmp/live-cli-startup-g60_95h4/upstream.jsonl)
- [独立 FS 复验](../../tmp/live-cli-startup-ynka4_7_/mpi-cancel-events.json)、[浏览器实际答复](../../tmp/job-late-cancel-ui-8u158nrq/browser-result.json)
- [记录了提交 API 与 worker PID 的 FS 复验](../../tmp/live-cli-startup-vrcpc4y1/mpi-cancel-recovery.json)、[同轮模型调用](../../tmp/live-cli-startup-vrcpc4y1/upstream.jsonl)。本轮二者均为 15336，退出后新 API 读取的发布 ID、清单和速度场保持一致。
- [已有 S3/PG 上的 WSL 取消和输入保留](../../tmp/live-cli-startup-dkck4kbh/mpi-cancel-recovery.json)、[既有 Linux/S3 同作业时序](../../tmp/V12-mpi-event-audit-20261118.json)

首次 FS 复验在作业仍处于合法的排队状态时过早断言运行，后续修正了观察窗口。首次浏览器验收读取了算例响应里不存在的 workspace_id，随后改为从工作区接口读取。上述 FS 记录中的 `original_publisher_exited` 能证实提交重算的 API 已退出，原记录没有区分提交者和实际 worker 的 PID，验收入口现已分别记录。S3 本轮遇到 Windows 代理接管回环请求及 `caseDict:advance` 超时。跨物理主机、其他故障场景和剩余资源预算继续跟踪。

## 复验入口

### S3 新一轮结果

首次在 Windows 系统代理下连接回环服务失败。将测试进程的回环地址加入 NO_PROXY 后，WSL 中的 SeaweedFS 可用，真实上传了 320 个参考文件。随后出现三类失败，分别为确认请求超时、工作流写入准入暂忙、确认已提交而后续调度暂忙。

S3 计时发现网格准备曾把不属于网格输入的 Ref 目录一并下载。现在准备和发布校验复用相同的网格输入选择，仍覆盖 caseDict、system、constant 和初始场。同一 320 文件场景中，网格准备的输入读取从 4.641–5.157 秒降至 0.219–0.610 秒。FS/PG 完整工程链在修复后通过。

确认已经提交而后续调度暂忙时，API 现在返回结构化的 `storage_quota_busy`。在最后一次 S3 复验中，B 确认后的网格已继续，C 确认后的求解在 60 秒检查窗口内没有启动。计时文件记录了 11 次输入封存内额度检查异常和 11 次对应的排队失败，确认没有重发。此轮 S3 未通过，也没有进入模型回读。仍需确定占用额度的具体事务并完成同次 S3 复验。[最后一次失败](../../tmp/live-cli-startup-9jd7dkva/live-result.json)、[输入封存与额度计时](../../tmp/live-cli-startup-9jd7dkva/mpi-stage-timings.jsonl)、[修复前网格计时](../../tmp/live-cli-startup-imc5th2n/mpi-stage-timings.jsonl)、[修复后网格计时](../../tmp/live-cli-startup-ot6ia4gi/mpi-stage-timings.jsonl)

### S3 额度冲突的修复与复验（11 月 18 日）

诊断轮把 12 次额度拒绝逐一关联到事件投递的读取预留。未启用搜索索引的算例也进入了索引读取路径，占用了写入方需要的额度。现在仅对已启用索引的算例申请读取容量。在 PostgreSQL/S3 上，若写入事务持有算例锁，索引消费者会释放预留并让持久事件等待重试。[故障计时](../../tmp/live-cli-startup-dldbw9km/mpi-stage-timings.jsonl)

同一规模的 S3/PG 复验中，320 个参考输入保留。两个非特权 MPI 进程开始求解后，另一 API 取消了作业，8 个原进程身份全部退出。重新计算到 0.5 得到 400 个速度向量，速度场摘要与文件存储一致。重算提交者和 worker 退出后，迟到取消被拒绝，发布清单及结果字节一致。[取消事件与日志](../../tmp/live-cli-startup-0p8mnjv8/mpi-cancel-events.json)、[重算与进程身份](../../tmp/live-cli-startup-0p8mnjv8/mpi-cancel-recovery.json)、[DeepSeek 调用](../../tmp/live-cli-startup-0p8mnjv8/upstream.jsonl)

单独持有写入锁时，未启用索引的事件直接交付，读取预留为零。已启用索引时，事件先保持待投递，预留释放后写入方通过额度检查。解锁后事件成功交付，重复投递后索引回执仍只有一条。[索引与额度专项](../../tmp/live-cli-startup-nfgjqifb/index-grant-result.json)、[DeepSeek 调用](../../tmp/live-cli-startup-nfgjqifb/upstream.jsonl)

### 排队取消后的 Redis 重投（V08）

在隔离恢复环境中，先停默认 worker，确认 Redis 收到了作业 171，再通过 API 取消。使用新传输 ID 重投同一作业后，实际 worker 返回 `already_owned_or_terminal`。数据库一直只有一次尝试，最后状态是 cancelled，无认领、启动或运行事件，探针文件也未出现。该项为基础设施验收。随后同环境的作业 172 经历两次 API 进程重启，仅执行一次。[重投证据](../../tmp/queued-restart-n7630xwj/cancelled-redelivery.json)、[恢复验收结果](../../tmp/queued-restart-n7630xwj/result.json)

### 命令

在 `platform/server` 使用项目 Python 执行 `live_llm_mpi_cancel_recovery_acceptance.py --source-profile-id 26 --postgres`，S3 增加 `--s3`；Windows 代理环境为测试进程的 `NO_PROXY` 加入 `127.0.0.1,localhost,::1`。浏览器验收使用已构建的 web、Playwright 和 Edge，入口为 `job_late_cancel_ui_acceptance.py`，先执行项目的回归审查校验。上述实验使用临时数据库和独立算例。
