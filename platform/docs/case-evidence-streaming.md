# 工程证据的按块校验

2026-11-19，N4-05 的 RB-12 和 RB-14 补齐网格、求解、报告及后处理共用的 `verify_records` 文件校验。每份证据复用现有文件读取器，按最多 64 KiB 的块计算 SHA-256。文件系统固定所选文件身份，S3 固定所选 ETag。读取结束后复核当前路径的版本，避免旧句柄读完后把同路径的新版本判为通过。旧清单继续只依赖路径和 SHA-256，缺少 `size` 字段的记录也可校验。有共享请求期限的调用继续沿用该期限。

实际文件缺失保留原证据过期提示。哈希不符或已选路径发生替换时，沿用文件已变化提示。存储服务明确拒绝访问时保留原服务错误。两个隔离 HTTP 入口均调用相同的生产证据函数，各测试实际 40 MiB、零字节、缺失和摘要不符。另一个写者在原流关闭后、校验返回前用相同字节替换路径，两种存储都拒绝旧校验，新一次校验通过。真实 SeaweedFS 还返回了未签名读取的 403。

| 存储 | 40 MiB Python 峰值分配 | 回执 |
| --- | ---: | --- |
| Windows 文件系统 | 177,192 B | [实际验收](../../tmp/case-evidence-fs-8b5lg8ht/case-evidence-streaming.json) |
| SeaweedFS S3 | 193,463 B | [实际验收](../../tmp/case-evidence-s3-ojd8sfd4/case-evidence-streaming.json) |

峰值取自校验过程的 `tracemalloc`。两个隔离 API 的进程身份确认退出，临时对象服务已停止。真实模型与模拟模型调用均为 0。[复验入口](../server/case_evidence_streaming_acceptance.py)
