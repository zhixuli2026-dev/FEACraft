# 本地命令批次的输入封存

2026-11-22，N4-05 补齐了本地文件系统中 `POST /cases/{id}/jobs:batch` 的 `commands` 提交路径。普通 Shell 使用整个案例作为输入，`mesh`、`solve`、`geometry`、`pvpython`、`convert` 保留原类型化输入语义。S3 的类型化命令批次另有验收记录。[单作业封存](local-typed-command-seal-capacity.md) · [S3 批次](s3-typed-command-batch-seal-capacity.md) · [注册操作](same-case-serial-batches.md)

## 提交与恢复

1. 核验权限、命令和幂等键，结束初始查询事务。新请求先库存盘点并计算整批输入、运行工作区和安装元数据的峰值，在复制前核对工作区所有者额度并取得节点空间预留。
2. 在数据库写事务外封存源输入。每项作业拥有独立的输入和运行副本。第二项起复制首项已封存的输入，同一个批次使用同一份来源版本。整个准备阶段共用复制名额及 300 秒期限。
3. 在案例提交事务内复核权限、封存目录和来源身份，提交全部作业、投递记录及批次索引。普通 Shell 继续要求历史作业有停止及发布证明。输入变化会拒绝整个提交。若已有物理目录先完成安装，原封存日志保留提交状态供恢复，下次同键重试会核对数据库见证并移除原未提交目录。
4. 同一幂等键重放返回已有批次和成员。客户端断开或达到期限时，先等实际复制排空，再清理本次暂存与节点预留。

## 实测

将真实的 40 MiB 输入复制到首份隔离目录后暂停。旧版 SQLite 的独立写事务被挡住，同案保存返回 409。新版普通 Shell 和 `convert` 在 SQLite 均允许独立写入，同案保存返回 200。普通 Shell 在 PostgreSQL 也得到相同结果。这三组在保存后拒绝旧封存，原幂等键重试成功，两项真实 WSL 作业分别输出当前输入的 SHA-256 并写入停止凭据。[旧版对照](../../tmp/n4-local-batch-baseline-ljcft9p1/batch-seal-result.json) · [SQLite Shell](../../tmp/n4-local-batch-fixed-d7ny2r35/batch-seal-result.json) · [SQLite convert](../../tmp/n4-local-batch-fixed-4tvv16m0/batch-seal-result.json) · [PostgreSQL](../../tmp/n4-local-batch-fixed-9brrp_c_/batch-seal-result.json)

普通 Shell 双作业的节点预留为 167,819,020 字节。分别将所有者和节点额度设在单作业能容纳、整批峰值不足的范围，均在第一次复制前拒绝。真实 HTTP 客户端断开后，暂存排空且没有新作业。设置三秒期限时，API 在复制排空后返回 `504 batch_seal_timeout`。[所有者额度](../../tmp/n4-local-batch-quota-0sg6k5da/batch-seal-result.json) · [节点额度](../../tmp/n4-local-batch-node-elrqm6ze/batch-seal-result.json) · [断开](../../tmp/n4-local-batch-disconnect-zxnz73be/batch-seal-result.json) · [期限](../../tmp/n4-local-batch-timeout-7gf85c8y/batch-seal-result.json)

另在第一项实际安装后改写源文件一个字节，并恢复文件大小和修改时间。第二项返回 `409 input_stale`，数据库无新增作业，批次列表为空。下次同键提交按见证回收上一条目录，两项新作业读到了改动后的摘要。历史成功作业缺少停止凭据时，普通 Shell 仍返回 `409 recovery_required` 并清理暂存。[提交中改动](../../tmp/n4-local-batch-late-ebd6z8cp/batch-seal-result.json) · [历史停止凭据](../../tmp/n4-local-batch-history-7qym46wp/compatibility.json)

首次同键提交暂停时，第二次提交成功。首次恢复后返回同一批次，数据库只保留两项作业，暂存和节点预留均已清理。认证 SeaweedFS 的普通 Shell 批次同样真实执行两项 WSL 作业，重放沿用原成员。单个本地类型化作业的真实 WSL 回归通过。[并发同键](../../tmp/n4-local-batch-concurrent-gmpfjdzl/batch-seal-result.json) · [认证 SeaweedFS](../../tmp/n4-s3-batch-6wn0fem5/compatibility.json) · [单作业回归](../../tmp/n4-local-cmd-fixed-kjuek8p3/seal-result.json)

本轮测试使用真实文件系统、数据库和原生进程，模型调用为 0。原资源验收仍剩 RB-12、RB-14。本机继续完成可以独立核验的工作，N7-01 跨物理宿主验收按用户决定暂缓。[验收汇总](../../tmp/N4-05-local-command-batch-20261122/validation.json)
