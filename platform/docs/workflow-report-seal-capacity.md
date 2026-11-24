# 托管报告的准备和输入封存

2026-11-22，确认 E 后的托管报告先在写事务外准备。Pandoc 工具校验、原生文档准备、已发布工程成果的读取、报告输入复制均在入队前完成。提交时复核工作流与步骤版本、已确认后处理、实际封存内容、来源文件及工具身份的观测。旧式存储仍走原报告准备和提交校验。

旧版 SQLite 在实际核验 231,056,136 字节 Pandoc 文件时阻塞独立写入，同算例保存返回 409。新版同一阶段允许独立写入，同账户另案保存返回 200。自动组稿和用户 Markdown 均实际输出 HTML 与 DOCX，并发布唯一报告作业。Markdown 的自定义标题已从发布后的 HTML 读回。 [旧版对照](../../tmp/n4-workflow-report-baseline-vonpafor/workflow-report-baseline.json) · [自动组稿](../../tmp/n4-workflow-report-fixed-eyumnz4i/workflow-report-fixed.json) · [Markdown](../../tmp/n4-workflow-report-fixed-markdown-2o7ksbm2/workflow-report-fixed.json)

认证 SeaweedFS 首轮在初始几何仍就绪时返回 `storage_quota_busy`，当时尚未到报告阶段。工作流封存提交改用有期限的存储额度准入，等已发写入结算后才进入写事务。 [首轮远端记录](../../tmp/n4-workflow-report-s3-fixed-_24n2zrp/workflow-report-fixed.json)

修复后认证 SeaweedFS 配 SQLite 跑通完整工程链。在实际核验同一 Pandoc 文件期间允许独立写入，另案保存为 200。确认 E 成功，只生成一个报告作业，实际产物可读回。 [远端复验](../../tmp/n4-workflow-report-s3-fixed-gk6_xwwd/workflow-report-fixed.json)

本批使用实际 OpenFOAM、VTK、Pandoc 和存储。模型调用为 0。验收纳入 N4-05 的 RB-12 与 RB-14。 [完整验收](../../tmp/N4-05-workflow-report-seal-20261122/validation.json)
