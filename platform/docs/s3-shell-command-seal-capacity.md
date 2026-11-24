# 普通命令的输入封存

2026-11-22，N4-05 将普通命令的输入大文件传输移出数据库写事务，覆盖本地及 S3 单作业的默认输入和显式文件目录，以及 S3 普通命令批次。带成果引用的单作业也已完成专项复验。 [成果引用封存](publication-command-seal-capacity.md) · [本地批次](local-command-batch-seal-capacity.md) · [S3 类型化批次](s3-typed-command-batch-seal-capacity.md)

## 提交与恢复

1. 校验访问权限、命令和幂等键，释放初始查询事务。案例存储租约保护同案操作，账户用量按现有版本核对，预先检查整批封存峰值。
2. 在数据库写事务外将版本固定的源文件下载至受节点额度约束的临时目录，计算摘要，再上传各项作业独有的不可变输入和待决记录。批次成员的源对象版本和便携权限清单来自同一个观测。准备阶段共用现有请求期限和复制名额。
3. 提交事务复核权限、源对象版本、临时封存和恢复日志，再绑定作业及批次索引。普通整案作业仍校验历史执行已停止。中途源变化时整批拒绝，由后续请求按数据库见证回收未提交对象。已提交的同键请求优先回读原提交。

严格条件操作通过认证 SeaweedFS 4.52 实测。若能力探针证实后端缺少条件操作，单项普通命令进入原准入流程并遵守原文件恢复检查。批次索引使用现有条件事务，能力不足时在复制前拒绝。

## 对照实测

实际物化 41,943,040 字节时，旧版 SQLite 的独立写入被挡住。单作业与双作业批次各有一组完整对照。新版 SQLite 单作业和 PostgreSQL 批次均允许独立写入，同账户另案保存返回 200。真实 WSL 作业输出与原始文件一致的 SHA-256，已登记停止证明，原幂等键重放返回同一提交。 [旧版单作业](../../tmp/n4-s3-shell-single-baseline-cismft4h/baseline.json) · [旧版批次](../../tmp/n4-s3-shell-batch-baseline-279uuas2/baseline.json) · [新版 SQLite](../../tmp/n4-s3-shell-single-fixed-a688y02l/baseline.json) · [新版 PostgreSQL](../../tmp/n4-s3-shell-batch-fixed-gif4q8sj/baseline.json)

新版 S3 SQLite 双作业同样通过。显式文件作业在源目录另有真实文件时仍只封存指定的 `Ref/seal-input.bin`。将账户额度设为 110 MiB 后，整批在第一次物化前返回 429，没有创建作业、批次或待决封存。 [新版 SQLite 批次](../../tmp/n4-s3-shell-batch-fixed-axt0idmi/baseline.json) · [S3 显式输入](../../tmp/n4-s3-shell-single-fixed-t0_t_rr0/baseline.json) · [峰值拒绝](../../tmp/n4-s3-shell-batch-quota-ie5ho7hu/baseline.json)

本地普通单作业在同一 40 MiB 场景中验证了默认输入和显式文件。SQLite 旧版同案保存为 409，新版同案保存为 200，独立写入可取得锁。复制期间新增的文件或外部文件改写使旧封存返回 409，原幂等键重试运行真实 WSL 且输出新摘要。默认输入在复制时预留 83,911,558 字节节点空间，显式输入回读清单只含选定文件。 [本地旧版](../../tmp/n4-local-shell-baseline-5x6j9l_a/receipt.json) · [本地默认输入](../../tmp/n4-local-shell-fixed-zspya5d9/receipt.json) · [本地显式输入](../../tmp/n4-local-shell-fixed-oofkfkld/receipt.json)

普通命令批次绑定第一项后替换同长度 S3 对象，第二项触发整批 409。作业和批次表都无新增，同键重试按见证清理 31 个未提交对象并成功运行。两个并发同键请求共享同一提交，只运行一组成员。 [部分绑定后改动](../../tmp/n4-s3-shell-batch-late-a_1r09t3/baseline.json) · [并发同键](../../tmp/n4-s3-shell-batch-concurrent-tflqruqz/baseline.json)

真实 HTTP 客户端断开后，实际 I/O 排空，节点预留和临时目录回收，案例租约结清。隔离的十秒期限在真实 I/O 排空后返回 504，两组均无新作业与远端待决封存。注入缺少停止证明的历史记录时，本地普通作业仍返回 `409 recovery_required`。本地类型化单作业及 S3 类型化批次也通过真实 WSL 回归。 [断线](../../tmp/n4-s3-shell-batch-disconnect-tiqb4b3_/baseline.json) · [期限](../../tmp/n4-s3-shell-batch-timeout-6h4useb5/baseline.json) · [历史停止门](../../tmp/n4-local-shell-history-gc3e82vl/receipt.json) · [本地类型化回归](../../tmp/n4-local-cmd-fixed-celymxgb/seal-result.json) · [S3 类型化回归](../../tmp/n4-s3-shell-batch-fixed-6nem274r/baseline.json)

并行调度隔离验收时，存储用量核验曾返回 503。有一组作业输出摘要后未取得 WSL 停止证明，作为失败保留。单独探测时其 Windows 进程与 Linux 进程组已退出。另有五个测试对象存储在退出阶段超时，核对原出生时间、执行文件和工作目录后逐个关闭。后续改为逐项验证，保留首次失败和清理记录。 [WSL 失败](../../tmp/n4-s3-shell-batch-fixed-53_9sg32/baseline.json) · [组验证](../../tmp/N4-05-s3-shell-seal-20261122/failed-native-group.json) · [存储关闭](../../tmp/N4-05-s3-shell-seal-20261122/fixture-cleanup.json)

SeaweedFS 3.99 的条件探针实测不通过，批次于首次物化前拒绝，数据库和远端无新增作业封存。该版本的旧式输入案例在两次隔离测试中均由原文件恢复门返回 409。一次经新版条件预检进入原提交，另一次在夹具中直接调用原提交。两次均无新存储租约或作业。本次 3.99 的回执用于核对保护行为。 [批次拒绝](../../tmp/n4-s3-shell-batch-baseline-cio0lw_r/baseline.json) · [新版入口](../../tmp/n4-s3-shell-single-baseline-894cdju_/baseline.json) · [原提交对照](../../tmp/n4-s3-shell-single-baseline-qa36cc9d/baseline.json)

本阶段使用真实存储、数据库和 WSL 进程，模型调用为 0。RB-12、RB-14 保持原验收范围。回退时先排空在途请求，按现有封存日志结清已确认的记录，再撤回普通命令的准备接线。 [验收汇总](../../tmp/N4-05-s3-shell-seal-20261122/validation.json)
