# 算例创建的整次节点预留

2026-11-08。在 [CLI 原生/API 崩溃组合验收](cli-native-api-crash-recovery.md)中，SQLite/FS 的真实模型验收算例创建两次超过客户端 30 秒等待。实际进程和文件均保留，没有使用模拟模型补成通过。

## 复现与修复

在已有 7 组 CLI 私有目录的条件下，新建 StandardCase 包含 12 个脚手架文件和一个事件文件，另写数值前缀 claim 与 pending plan。原实现每次 `put_new` 都重新申请节点额度，因此执行了 **15 次全节点库存扫描，累计 31.388 秒**；请求实际成功返回耗时 **32.844 秒**。这说明首次观察到的 HTTP 超时不是 CLI 初始化失败，也不是已证实的死锁。

`case_provision` 现在先从完整 manifest 计算这次创建的节点峰值，在 `local_provision_budget.node_batch` 内持有一份真实持久节点 claim。估算包括已保留文件、当前文件的临时/正式硬链接双份逻辑计数和暂存意图；使用较长的 generation 前缀覆盖数值前缀被占用的情况。每次实际 `put_new` 都检查 `retained + 实际峰值 <= 预留上限`，并在 finally 保守累计保留字节；估算过小会在写入前拒绝，不能增加未预留额度。

每次实际写入仍经用户额度检查，保留原 owner SQL 锁及暂存意图、无覆盖硬链接发布、目录同步、条件恢复协议。没有缓存或复用旧库存结果。并发节点申请仍看到完整存活 claim 和实际文件，允许保守重复计数；这不是对外部写入的 OS 硬配额。作用域禁止嵌套和并发写入，取消时先排空真实文件线程，才关闭并释放节点 claim。

恢复旧 pending plan 的历史回执以及没有批次上下文的独立 `put_new` 继续单次预留。已有历史待结清时，整条业务请求可能另外执行恢复扫描；“一次”指没有旧待恢复记录的这次新创建，而不是所有创建/恢复组合都永远只扫描一次。S3 继续走原 owner remote credit。

同一原生目录库存条件下，[修复后真实请求](../../tmp/cli-case-latency-6xzzoy81/diagnostic.json)为 **1 次节点全量扫描、3.735 秒**；[修复前诊断](../../tmp/cli-case-latency-lt2gdgbl/diagnostic.json)保留。没有调长原验收客户端的 30 秒期限。

## 实际检查

- [SQLite 额度/取消](../../tmp/live-cli-startup-can7orms/provision-batch-checks.json)与[PostgreSQL 额度/取消](../../tmp/live-cli-startup-zywkny3a/provision-batch-checks.json)：各一次普通创建确认节点扫描为 1；实际创建过小预留、零用户额度、跨账户输入三个拒绝条件；真实 2 MiB filler 导致节点满拒绝；真实硬链接写入暂停后取消，节点 claim 保留到写入排空，文件与暂存意图按规则收尾。共 10 个边界检查，包含 6 个配置/输入拒绝条件及 4 个实际磁盘/取消场景。
- [FS 819,200 字节实际复制](../../tmp/live-cli-startup-_m5jb3dw/provision-batch-copy.json)与[S3 实际复制](../../tmp/live-cli-startup-fr8m5647/provision-batch-copy.json)：新副本逐字节一致，实际 provision 恢复无未解决项。PG/S3 普通创建兼容也已通过。
- 修复后[完整 SQLite CLI 崩溃矩阵](../../tmp/live-cli-startup-6wb1gzrj/native-api-crash-checks.json)重跑 7 个边界，再通过真实算例创建和模型读取。

上述 5 套创建/复制模型验收共 **10 次真实 api.deepseek.com / deepseek-flash，43,066 tokens**；连同本阶段 CLI 矩阵和前序成功记录合计 **16 次、68,824 tokens，MOCK 0**。真实回复、工具选择、流式及持久答案均由上游生成，CLI 内部模型回合仍未验收。所有测试 API 退出，临时 PG 删除，实际 S3 服务停止；开发数据库未写入。

此修复关闭本次可重复的逐文件全量扫描放大。既有其他慢扫描、全局预算、请求总期限和不可中断 I/O 问题仍保留。主线 33/53、剩 20；资源剩 6；295 项全功能整项验收继续。
