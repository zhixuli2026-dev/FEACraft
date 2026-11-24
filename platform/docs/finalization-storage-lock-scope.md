# 作业收尾磁盘检查移出写事务

## 11 月 19 日 SQLite 与 S3 的自然收尾联合作业

隔离 API、SQLite 与实际 SeaweedFS 上，三个真实 Windows 作业分别验证正常发布、同一算例在子进程运行期间写入后拒绝旧输入、源保留后重试发布。提交至观察到终态分别为 5.565 秒、12.485 秒和 6.939 秒。两次索引重建从自然收尾的来源校验开始后发起，均返回 HTTP 200。旧输入以 `input_stale` 拒绝并保留上一发布指针，重试才推进指针。三个执行范围均已实际停止。[联合作业记录](../../tmp/live-cli-startup-ueo3g0pt/joined-finalization.json)

冷启动新的 API 后，原文件、日志、发布指针、108 条持久事件及两个写入投递回执均核对一致。索引只命中新内容，四类相关存储预留结算或实化后的剩余量为 0。真实 DeepSeek 通过读取工具回读证据，2 次 HTTP 200、8,600 tokens，模拟调用 0。隔离 API 与 SeaweedFS 均已退出。[重启只读核验](../../tmp/live-cli-startup-ueo3g0pt/joined-readonly-audit.json) · [模型回执](../../tmp/live-cli-startup-ueo3g0pt/live-result.json) · [复跑入口](../server/live_llm_s3_joined_finalization_acceptance.py)

历史 180 秒超时记录经独立只读核对，当时的日志、归档和发布存储预留都已实化，来源占用在约 2.550 秒后释放，随后缺少资源用量与完成回执。现有证据能确定停在准备之后的区间，具体根因仍待证实。[历史超时](../../tmp/residual-s3-job-finalization-timeout-20261119.json) · [核对结果](../../tmp/live-cli-startup-ueo3g0pt/joined-readonly-audit.json)

## 写事务原缺陷与修复

终态提交前仍检查执行目录实际占用。现在文件系统扫描先完成，由来源校验续租在扫描期间保留发布资格。随后排空续租、取得数据库写锁，再核对作业、来源和预算，并提交终态与资源记录。释放来源占用仍与终态同一次提交。

原实现是在记录终态用量时扫描磁盘。故障测试在 SQLite 与 S3 的这次扫描前暂停了工作线程，同账户另一个算例的写入在约 21.8 秒后返回 `storage_quota_busy`。暂停超过测试门限后，原作业以资源问题失败。这个测试复现了磁盘扫描持有 SQLite 写锁造成的阻塞。[旧代码写入结果](../../tmp/live-cli-startup-c5flbkvf/writer-during-final-sample.json) · [旧代码收尾记录](../../tmp/live-cli-startup-c5flbkvf/finalization-diagnostic.json)

## 四种组合实测

| 数据库与存储 | 扫描暂停时另一个算例写入耗时 | 终态与续租证据 | 真实模型回执 |
|---|---:|---|---|
| SQLite 与文件系统 | 0.36 秒 | [实际记录](../../tmp/live-cli-startup-adhhidub/finalization-diagnostic.json) | [DeepSeek](../../tmp/live-cli-startup-adhhidub/live-result.json) |
| PostgreSQL 与文件系统 | 0.28 秒 | [实际记录](../../tmp/live-cli-startup-_lpzsgny/finalization-diagnostic.json) | [DeepSeek](../../tmp/live-cli-startup-_lpzsgny/live-result.json) |
| SQLite 与 S3 | 2.56 秒 | [实际记录](../../tmp/live-cli-startup-lyq2yo6j/finalization-diagnostic.json) | [DeepSeek](../../tmp/live-cli-startup-lyq2yo6j/live-result.json) |
| PostgreSQL 与 S3 | 2.48 秒 | [实际记录](../../tmp/live-cli-startup-tjwnkme5/finalization-diagnostic.json) | [DeepSeek](../../tmp/live-cli-startup-tjwnkme5/live-result.json) |

每组实际运行三个 Windows 子进程。两个正常作业完成，其中一个在最终扫描暂停时观察到来源校验续租，另一个算例在释放扫描前写入成功，随后逐字节读回。第三个作业在最终扫描前被加入 9 MiB 的隔离测试文件，超过该轮配置的 8 MiB 上限，最终返回 `failed`、`resource_limit`、退出码 -1，发布记录为失败。诊断只改变隔离测试配置和目录。四个最终运行共 8 次真实 `deepseek-flash` HTTP 200，模型用工具读取每轮验收摘要。

此前残差验收中自然发生的 180 秒超时，留存记录显示来源锁已经取得并在约 2.6 秒后释放，发布仍处于 prepared。带分阶段追踪的三个初测共跑完 9 个原生作业，未复现该次问题，另有 6 次真实 DeepSeek HTTP 200。本轮修复已有确定反例的数据库锁问题，原超时的根因继续由 N7-01 跟踪。[自然发生的超时证据](../../tmp/residual-s3-job-finalization-timeout-20261119.json) · [SQLite S3 初测一](../../tmp/live-cli-startup-pgpw8i7x/finalization-diagnostic.json) · [SQLite S3 初测二](../../tmp/live-cli-startup-scu_h350/finalization-diagnostic.json) · [PostgreSQL S3 初测](../../tmp/live-cli-startup-3bvfcjh1/finalization-diagnostic.json)

使用服务器虚拟环境运行 `live_llm_s3_finalization_diagnostic_acceptance.py --source-profile-id 26`，可增加 `--postgres` 或 `--s3`。[复验入口](../server/live_llm_s3_finalization_diagnostic_acceptance.py)

## 扫描与作业共用截止时间

旧代码在扫描线程启动后才计算扫描期限。文件系统与 SQLite 的隔离作业设置了 35 秒整次预算。测试将最后一个磁盘扫描线程暂停到作业到期后，再放行线程。旧代码扫描并返回 9,268 字节，最后把磁盘状态写成 `measured`。作业虽已因 `timeout` 失败，仍执行并采纳了到期后的扫描。[旧代码反例](../../tmp/live-cli-startup-siv_6er7/finalization-diagnostic.json)

扫描从进入等待开始，取整次作业、单次测量及外层请求截止时间中最早的一个。单个 API 或 worker 进程同时执行的作业扫描数使用 `storage_measurement_concurrency` 配置。等同一作业的测量锁、等扫描名额和执行扫描共用一个期限。目录枚举到达期限后停止启动新的文件属性读取。系统文件调用正在执行时，由所属线程实际退出后释放该作业的测量锁和扫描名额。后续终态检查仍在数据库写事务里核对作业与预算。

首轮修复中，SQLite 与 S3 的作业正确等待扫描退出并保存失败，资源记录将 Windows 异步计时器提前一个节拍触发的整次作业超时错误标成 `disk_unverified`。现按触发的截止时间判定原因。单独的真实目录与线程检查验证了等待测量锁、等待名额、取消期间继续占有名额、计时器分辨率、外层期限继承，以及目录枚举迟到时停止下一个属性读取。该组没有模型环节。[初轮分类记录](../../tmp/live-cli-startup-n31266go/finalization-diagnostic.json) · [本地检查结果](../../tmp/N4-05-execution-storage-deadline-20261119.json)

| 数据库与存储 | 扫描暂停时另一个算例写入耗时 | 作业与持久用量记录 | DeepSeek 工具读回 |
|---|---:|---|---|
| SQLite 与文件系统 | 0.44 秒 | [作业记录](../../tmp/live-cli-startup-lamggf5w/finalization-diagnostic.json) | [模型回执](../../tmp/live-cli-startup-lamggf5w/live-result.json) |
| PostgreSQL 与文件系统 | 0.41 秒 | [作业记录](../../tmp/live-cli-startup-i7ibnjrr/finalization-diagnostic.json) | [模型回执](../../tmp/live-cli-startup-i7ibnjrr/live-result.json) |
| SQLite 与 S3 | 2.95 秒 | [作业记录](../../tmp/live-cli-startup-scxenhdc/finalization-diagnostic.json) | [模型回执](../../tmp/live-cli-startup-scxenhdc/live-result.json) |
| PostgreSQL 与 S3 | 2.81 秒 | [作业记录](../../tmp/live-cli-startup-f6g0rvle/finalization-diagnostic.json) | [模型回执](../../tmp/live-cli-startup-f6g0rvle/live-result.json) |

最终四组实跑 16 个 Windows 作业。每组两个正常作业完成，一个超出 8 MiB 限额后失败，一个在 35 秒到期后仍等待测量线程排空，再以 `resource_limit` 和退出码 -1 写入失败。四组到期作业均留下 `timeout` 和 `unverified` 记录，没有成功发布可见事件。每组用真实 `deepseek-flash` 调用工具读取摘要，共 8 次 HTTP 200。加上本次首轮三个通过组共有 14 次真实请求，均返回 200，模拟调用为 0。N4-05 继续覆盖其他长 I/O、跨进程额度与资源生命周期。自然发生的收尾超时仍在 N7-01 跟踪。
