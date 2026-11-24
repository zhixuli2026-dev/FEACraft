# 已发布结果的兼容文件应用与恢复

2026-11-22，文件系统在写事务外核验已发布原件、检查目标版本，并按实际文件大小和目标卷预留暂存空间。事务内复核活动成果指针和目标版本，再同卷安装副本。认证 SeaweedFS 在短事务中取得独立的算例存储租约，条件写入和原件传输在事务外完成。两种存储继续使用原有发布计划和持久回执。

真实 WSL 转换生成 40 MiB 产物。旧文件系统在读取兼容文件原件期间阻塞 SQLite 独立写入，同算例保存返回 409。旧 SeaweedFS 的同阶段也阻塞 SQLite 写入，保存请求在暂停期间等待。新版在两种存储均允许独立写入，同账户另案保存均返回 200，回读文件的 SHA-256 与原件一致。 [新版文件系统](../../tmp/n4-publication-alias-fixed-tlfxnv2l/publication-alias-fixed.json) · [旧版 SeaweedFS](../../tmp/n4-publication-alias-s3-baseline-v40fah1_/publication-alias-baseline.json) · [新版 SeaweedFS](../../tmp/n4-publication-alias-s3-fixed-hp4xrqdj/publication-alias-fixed.json)

文件系统另验证提交前的外部原位编辑。用户文件保留原字节，不可变成果仍能按原 SHA-256 读回。文件系统和 SeaweedFS 分别在真实复制期间终止旧 API，重启后原作业与发布身份保持，各补写一个恢复回执。恢复后文件大小和 SHA-256 一致，存储租约结清。 [外部编辑](../../tmp/n4-publication-alias-edited-s2d8b_oq/publication-alias-edited.json) · [本地重启](../../tmp/n4-publication-alias-restart-enqybrdf/publication-alias-restart.json) · [远端重启](../../tmp/n4-publication-alias-s3-restart-elk1otvc/publication-alias-restart.json)

首轮旧版对照已读回匹配数据，后续断言用错作业 ID 字段。首次重启夹具也将 Windows 启动器 PID 当成服务 PID；复验改用已验证父子关系和出生记录的真实 API 进程。两条失败记录保留。本批模型调用为 0。 [旧版原始记录](../../tmp/n4-publication-alias-baseline-406uj0ve/publication-alias-baseline.json) · [首轮重启记录](../../tmp/n4-publication-alias-restart-y0crkbq9/publication-alias-restart.json)

文件系统和认证 SeaweedFS 分别配 SQLite、PostgreSQL 的四组交错均通过。文件系统的期限用例在真实复制 40 MiB 后达到 4 秒验收期限，排空暂存，未安装兼容文件，原作业的不可变成果仍完整可读。托管工作流另跑全链路回归，正式生成 121,857 字节 HTML 与 87,494 字节 DOCX。验收确认私有 API 监听关闭、四个隔离 SeaweedFS 进程退出、两个 PostgreSQL 临时数据库删除。真实模型调用 0，模拟模型调用 0。 [合并验收与逐轮记录](../../tmp/N4-05-publication-alias-write-20261122/validation.json)
