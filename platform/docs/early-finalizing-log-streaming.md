# 作业早期收尾日志的分块写入

2026-11-19，N4-05 / RB-12 与 RB-14。早期收尾原来把原生作业日志整份读入 Python，再建立文件计划和安装。原作业、收尾接续和求解证据日志均可走这个路径，原允许的单日志上限为 64 MiB。[原收尾与恢复协议](early-finalizing-stage-recovery.md)

现在按 64 KiB 扫描原日志，记录原有完整 SHA-256 和长度。传给安装线程的是原日志位置及内存中各块的摘要。文件计划、归属、事件和目标互斥规则保持原协议。安装前检查原日志的文件身份，每块核验后才比较或补写实际暂存，现存前缀必须逐字节相同。底层只为尚待写入的字节申请原来的节点预留。完整暂存核验后以原有无覆盖方式安装，日志转移不产生第二份磁盘暂存。原日志中途改变时，已经写出的数据仍只来自核验通过的块。[阶段文件实现](../server/app/services/finalizing_stage.py) · [原运行收尾](../server/app/services/job_finalizing_source.py) · [接续](../server/app/services/finalizing_stage_recovery.py)

## 实际 Windows 和普通文件存储

两个独立 Windows 原生作业均输出 41,943,040 B，SHA-256 均为 `d8594b3a5a4773589c171cedcd0f7440522dcd20cb65fe66c8f33752f33a2d06`。正常收尾的 Python 跟踪分配峰值为 6,778,924 B。另一运行的夹具暂停于实际 `fsync` 后，此时只写了真实 65,536 B 暂存前缀。原 API 仍存活时，公开接续返回 409。随后实际终止原 API，新 API 对破坏后的同长度前缀返回 409 并保留字节。恢复正确前缀后接续同一尝试，实际缓存核验和最终提交通过，重复接续返回原回执。接续阶段的 Python 跟踪分配峰值为 2,601,319 B。[通过回执](../../tmp/early-finalizing-log-qnafjx4t/early-finalizing-log-streaming.json) · [执行器摘要](../../tmp/vibeflow-regression-all-20261119-140533-6771f3/summary.json)

两个原生作业均只启动一次，已核验实际停止。两组最终文件与原日志逐字节匹配，暂存半文件和节点预留均已排空，三个实际 API 出生身份核验退出。首轮夹具错误地要求正常文件存储输出包含 S3 才写入的 `finalizing_source` 属性。该轮已成功写出同一份 40 MiB 日志，夹具仍按失败保存，后续改用本地清单、计划和实际文件核验。[首轮回执](../../tmp/early-finalizing-log-0_9wlkug/early-finalizing-log-streaming.json) · [首轮实际退出](../../tmp/early-finalizing-log-0_9wlkug/first-api-exit.json)

峰值测量覆盖早期收尾复制或接续捕获，使用 Python `tracemalloc`。原生命令、整个进程 RSS 及晚期 S3 作业日志传输分开计量。上述均无模型调用，隔离原生临时目录和文件位于 D 盘。C 盘可用空间 22.20 GiB，前后读数相同。晚期日志流式上传和核验见后续记录。[晚期远端作业日志](job-log-streaming.md)

## 实际 SQLite 和认证 SeaweedFS

两套隔离对象服务串行验收。正常归档和原 API 在已刷新的 65,536 B 暂存后退出，均通过真实对象 GET 分块核对 41,943,040 B 日志及相同 SHA-256。正常收尾与接续捕获的 Python 跟踪分配峰值分别为 6,489,724 B 和 2,460,331 B。恢复前检测到的损坏前缀按原样保留。正确前缀恢复后复用同一个尝试，持久归档并完成原作业。两个原生作业各启动一次，实际停止回执均已核验。[通过回执](../../tmp/early-finalizing-log-8o8uws7c/early-finalizing-log-streaming.json) · [执行器摘要](../../tmp/vibeflow-regression-all-20261119-141021-e9d92b/summary.json)

三个隔离 API、两个对象服务的启动器和原 Linux 出生身份均已核验退出，原隔离目录无活动成员。验收前后 56 个现有 Docker 容器的 ID、名称和状态清单相同，运行中 11 个，原本的两个本地处理端点均返回 HTTP 200。C 盘可用空间 22.20→22.19 GiB，隔离对象数据位于 D 盘。四个原生作业的实际和模拟模型调用均为 0 次。本轮小项覆盖 SQLite 两种实际存储的早期收尾。[对象服务退出独立观察](../../tmp/early-finalizing-log-8o8uws7c/s3-processes-exited.json)
