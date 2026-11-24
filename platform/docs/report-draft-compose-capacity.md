# 报告草稿生成的成果读取与输入封存

2026-11-22，文件系统的 `/report:compose` 在写事务外核验工程成果并封存草稿输入。事务内复核算例权限、修订号、名称、覆盖条件、原请求键与封存记录。认证对象存储沿用既有封存路径。

旧版 SQLite 在核验实际 40 MiB 工程输入时阻塞独立写入，同算例保存为 409。新版同一位置允许独立写入，同账户另案保存返回 200，两个入口都产生真实 Markdown 草稿。文件系统核验了独立生成稿，认证 SeaweedFS 核验了可编辑稿；两个产物均包含结果和证据章节。 [本地生成稿](../../tmp/n4-draft-compose-fixed-bfccf0ml/draft-compose-fixed.json) · [SeaweedFS 可编辑稿](../../tmp/n4-draft-compose-s3-fixed-editable-mmiq9hqp/draft-compose-fixed.json)

旧版对照首轮以草稿文本应含 `350` 为断言。实际稿件已发布，后处理值独立记录为 349.9999997764826 K；复验按公开草稿的结果和证据章节验收，首轮断言失败保留。 [旧版原始记录](../../tmp/n4-draft-compose-baseline-36i0jo25/draft-compose-baseline.json) · [完整验收](../../tmp/N4-05-draft-compose-seal-20261122/validation.json)

下一处已观测的长事务在作业结果的兼容文件写入。真实 WSL 转换读取 40 MiB 已发布结果时，SQLite 独立写入受阻，同算例保存返回 409。 [兼容文件对照](../../tmp/n4-publication-alias-baseline-406uj0ve/publication-alias-baseline.json)

本批使用实际 OpenFOAM、VTK、草稿生成器和存储。模型调用为 0。证据纳入 N4-05 的 RB-12 与 RB-14。
