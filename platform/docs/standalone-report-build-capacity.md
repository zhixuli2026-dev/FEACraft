# 独立报告构建的输入封存

2026-11-22，文件系统上的 `/report:build` 在写事务外核验工具、准备文档并封存输入。提交复核写权限、原请求键、来源和工具版本，然后提交作业及成果预留。认证对象存储沿用先准备再提交的路径，两个存储入口使用同一作业回执。

旧版 SQLite 在核验实际 231,056,136 字节 Pandoc 文件时阻塞独立写入，同算例保存返回 409。新版同一算例的非报告文件保存返回 200，真实 HTML 与 DOCX 均可读回。修改报告草稿后重放原请求键，返回已发布的原作业。 [旧版对照](../../tmp/n4-standalone-report-baseline-sva0ve89/standalone-report-baseline.json) · [新版同算例复验](../../tmp/n4-standalone-report-fixed-same-case-mdwwgp7_/standalone-report-fixed.json)

认证 SeaweedFS 配 SQLite 通过独立入口的兼容验收。同账户另案保存为 200，Pandoc HTML 和 DOCX 实际生成，原请求键可复用原作业。 [远端复验](../../tmp/n4-standalone-report-s3-fixed-ndvruq3d/standalone-report-fixed.json)

本批使用实际 OpenFOAM、VTK、Pandoc 和存储。模型调用为 0。验收纳入 N4-05 的 RB-12 与 RB-14。 [完整验收](../../tmp/N4-05-standalone-report-seal-20261122/validation.json)
