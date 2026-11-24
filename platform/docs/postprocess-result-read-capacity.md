# 后处理结果读取的算例锁与存储预算

`GET /api/v1/cases/{case_id}/postprocess/results` 先在短事务中选定发布及成功作业。发布清单、指标、图件和当前来源随后在写事务外核验。归档成员按块读取，认证 SeaweedFS 可直接校验远端对象。返回前重新核对账号权限、算例修订、事件序号以及读取到的发布依赖。已有当前结果、`job_id` 和 `publication_id` 三种入口沿用原响应字段。

旧版未登记发布的结果继续通过最新指针或 `job_id` 读取。读取绑定指标、清单和指针的实际文件版本，期间被替换时返回 `409 input_stale`。登记发布归档损坏返回 `409 evidence_invalid`。超时等待已开始的读取收尾后返回 `504 postprocess_result_timeout`，后续请求可重试。普通报告和 LaTeX 结果入口使用同一读取流程。

## 2026-11-22 实测

对照使用真实 WSL `blockMesh` 和 `laplacianFoam`，再由公开后处理入口运行 VTK 提取和 Matplotlib Agg。旧版在真实归档读取期间阻挡独立 SQLite 写事务，文件系统和认证 SeaweedFS 均复现。[本地旧版](../../tmp/N4-05-postprocess-results-20261122/baseline-fs.json) · [SeaweedFS 旧版](../../tmp/N4-05-postprocess-results-20261122/baseline-s3.json)

新版在两种存储上均允许同案写事务提交。均值为 349.999999944 K，归档 PNG 的 SHA-256 与清单一致，实测 600 dpi 输出 3,600 × 2,400 像素。当前结果与按作业、按发布的回读一致。并发编辑求解场返回 409，随后可回读旧指标、清单和工具链，并标示来源过期。测试还覆盖跨账户 404、同长度归档篡改、恢复原字节、超时重试，以及旧版指针和文件版本竞争。认证 SeaweedFS 读端未写入完整的本地归档缓存。[本地新版](../../tmp/N4-05-postprocess-results-20261122/fs.json) · [SeaweedFS 新版](../../tmp/N4-05-postprocess-results-20261122/s3.json)

共享读取流程另外通过实际 Pandoc 和 WSL `pdflatex` 复验。所有场景均为实际文件、HTTP、数据库、对象存储或原生计算检查，模型调用 0。[Pandoc](../../tmp/N4-05-postprocess-results-20261122/report-compat.json) · [LaTeX](../../tmp/N4-05-postprocess-results-20261122/latex-compat.json)

测试迭代修正了夹具里的坐标格式、图表字段位置及旧版算例账号。Windows 上的并发旧版替换发生在原文件读句柄关闭后、文件版本复核前。证据纳入 RB-12、RB-14 已覆盖的入口。[验收脚本](../../tmp/N4-05-postprocess-results-20261122/check.py) · [汇总](../../tmp/N4-05-postprocess-results-20261122/validation.json)
