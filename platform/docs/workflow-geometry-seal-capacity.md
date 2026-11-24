# 托管几何预览和导出的输入封存

2026-11-22，工作流启动后，几何脚本、输入文件与上一代几何成果记录在写事务外准备。提交仍复核工作流版本、算例修订号、写权限、预算及已读取的成果记录。适用于注册的 CadQuery 预览和导出处理器。

旧版 SQLite 在实际复制 40 MiB 几何目录文件时阻塞独立写入，同算例保存返回 409。新版同算例保存返回 200，真实 CadQuery 预览产生 `A-geo/validation.json`，只创建一个托管几何作业并完成确认 B。认证 SeaweedFS 配 SQLite 的导出模式在实际复制时允许独立写入，同账户另案保存返回 200，实际导出和确认 B 均完成。 [旧版对照](../../tmp/n4-workflow-geometry-baseline-tavgc9us/workflow-geometry-baseline.json) · [本地预览](../../tmp/n4-workflow-geometry-fixed-qoep2nwm/workflow-geometry-fixed.json) · [SeaweedFS 导出](../../tmp/n4-workflow-geometry-s3-fixed-49axs3tc/workflow-geometry-fixed.json)

本批使用真实 CadQuery 和存储。模型调用为 0。验收纳入 N4-05 的 RB-12 与 RB-14。 [完整验收](../../tmp/N4-05-workflow-geometry-seal-20261122/validation.json)
