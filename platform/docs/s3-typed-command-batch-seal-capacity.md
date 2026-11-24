# S3 类型化命令批次的输入封存

2026-11-22，N4-05 将 `POST /cases/{id}/jobs:batch` 的 S3 类型化命令封存移出数据库写事务。适用于 `mesh`、`solve`、`geometry`、`pvpython`、`convert` 的默认输入路径。普通 Shell 的实测记录于普通命令封存。 [S3 单作业封存](submission-capacity.md#2026-11-21-s3-类型作业的输入封存) · [普通命令封存](s3-shell-command-seal-capacity.md) · [本地命令批次](local-command-batch-seal-capacity.md)

## 提交范围

先在现有案例存储租约内核对权限、来源版本与数据摘要。整批沿用同一份观测结果，并按作业数核对存储峰值。每项只在上传期间持有一份本地临时输入。对象写入继续按实际字节保留持久额度，各作业封存有独立的版本和待决日志。

源输入读完、校验和上传完成后，用现有同一事务绑定全部作业、投递和批次索引。绑定仍核对可写权限、源对象版本、输入封存对象及恢复日志。读取、暂存和提交共用现有 300 秒期限，达到期限或连接断开时走原排空和租约释放路径。幂等键兼容已提交的批次。

## 实测

认证 SeaweedFS 上用 41,943,040 字节原始对象复现。旧版在真实输入落到本地缓存后仍占用 SQLite 写锁，放行后两个 WSL 作业都回读到原始 SHA-256。新版 SQLite 和 PostgreSQL 均可在同一观察点取得独立写锁，同一账户的另一个案例保存返回 200。两库均通过 WSL 回读和停止凭据核验，相同幂等键返回原两个作业。[旧版对照](../../tmp/n4-s3-batch-seal-baseline-a18uv25y/baseline.json) · [SQLite 验收](../../tmp/n4-s3-batch-seal-fixed-1mbyc60o/baseline.json) · [PostgreSQL 验收](../../tmp/n4-s3-batch-seal-fixed-olvnqpzd/baseline.json)

将所有者额度设为 110 MiB 后，单项峰值可以容纳，整批在第一次材料化副本前返回 429，未产生作业、批次索引或待决封存。[峰值拒绝](../../tmp/n4-s3-batch-seal-quota-tenl09fq/baseline.json)

第一项的封存清单已写入对象存储时，测试进程替换同长度的源对象。第二项绑定返回 409，数据库无新作业，批次列表为空。同键重试核对提交见证并清理 31 个上一请求留下的封存对象，两项 WSL 作业读到了新摘要。[绑定途中替换](../../tmp/n4-s3-batch-seal-late-1j5t89b2/baseline.json)

上传前替换源对象也返回 409，暂存清理后重试成功。第一次跑这一场景时已通过 409 和清理检查，随后的真实 WSL 作业在启动身份核验失败。单独重跑通过，首次失败保留。旧版对照首次在三卷对象存储下返回 `InternalError`，更换为十二卷后完成对照。[上传前替换](../../tmp/n4-s3-batch-seal-stale-xwi4dnl8/baseline.json) · [首次 WSL 启动失败](../../tmp/n4-s3-batch-seal-stale-ni2yao_h/baseline.json) · [首次旧版对照](../../tmp/n4-s3-batch-seal-baseline-e68ij0pq/baseline.json)

本轮使用真实文件系统、认证对象存储、数据库和 WSL 进程，模型调用为 0。RB-12、RB-14 继续按原范围验收。[验收汇总](../../tmp/N4-05-s3-command-batch-20261122/validation.json)
