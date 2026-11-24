# 求解日志按块解析

`/cases/{case_id}/logs/parse` 核对算例和文件版本后即可释放数据库事务与算例锁。解析线程每次读取 64 KiB，使用打开的本地文件或带版本条件的 S3 请求。同一进程最多有四项正在运行的文件版本读取和日志解析。请求与文件读取共用同一截止时间。底层 I/O 尚在运行时，线程名额保留到实际操作退出。

增量请求逐块计算既有前缀的 SHA-256。只有前缀与 offset 都匹配时，才解析新增完整行。末尾半行随下一次追加继续解析，偏移和摘要按实际字节计算。普通解析仍包含最后一行。消费过的字节遭重写、日志截断或选定文件在打开前被替换时返回 HTTP 409。单行超过 4 MiB 时返回 HTTP 413，可用原始文件接口分段查看。

## 实际测量

四套隔离环境使用同一份 11,832,128 字节日志，含 OpenFOAM 时刻、TimeStep、Courant、DILU 残差和中文诊断。续读场景另追加 102 字节。每组使用旧缓冲读取和正式 HTTP 路径分别解析相同文件，结果和游标一致。下表是校验与解析阶段的 Python 峰值分配。

| 数据库与存储 | 完整解析旧版 → 当前 | 已有指纹的增量续读旧版 → 当前 | 实测 |
|---|---:|---:|---|
| SQLite 与文件系统 | 47,359,746 → 487,630 B | 11,928,635 → 228,902 B | [验收](../../tmp/live-cli-startup-jlqavs8o/log-parse-stream.json) |
| PostgreSQL 与文件系统 | 47,377,280 → 741,355 B | 11,857,589 → 148,631 B | [验收](../../tmp/live-cli-startup-2x3dmlge/log-parse-stream.json) |
| SQLite 与 S3 | 47,362,940 → 743,132 B | 12,227,890 → 197,845 B | [验收](../../tmp/live-cli-startup-w4zwd4t4/log-parse-stream.json) |
| PostgreSQL 与 S3 | 47,349,521 → 737,215 B | 11,881,251 → 447,226 B | [验收](../../tmp/live-cli-startup-j8ou236o/log-parse-stream.json) |

四组均在文件版本选定后暂停读取，同一账户此时通过正式文件接口完成源文件写入。恢复旧读取时返回 409，重新解析能得到新时刻与错误计数。半行追加、空增量、行内偏移、跨用户访问拒绝、截断、前缀重写、单行限制以及原始文件范围读取逐项通过。测试针对服务器日志接口，浏览器曲线及长序列渲染由 FF-H03 至 FF-H05 的界面验收跟踪。

四组最终验收各有两次真实 `deepseek-flash` HTTP 200，读取的是测试写入算例的验收摘要。此前两组初测还有四次真实请求。临时 PostgreSQL 库已删除，隔离 S3 和 API 进程已退出。[SQLite 文件模型回执](../../tmp/live-cli-startup-jlqavs8o/live-result.json) · [PostgreSQL 文件模型回执](../../tmp/live-cli-startup-2x3dmlge/live-result.json) · [SQLite S3 模型回执](../../tmp/live-cli-startup-w4zwd4t4/live-result.json) · [PostgreSQL S3 模型回执](../../tmp/live-cli-startup-j8ou236o/live-result.json)

内存数据来自诊断文本占多数的样本。响应仍保存匹配到的残差、时刻和 Courant 序列，峰值会随返回点数增长。续读仍扫描已消费前缀以校验摘要。本机 Windows API 连接文件系统或 S3 的结果已记录，跨主机组合继续由 N7-01 跟踪。

使用服务器虚拟环境运行 `live_llm_log_parse_stream_acceptance.py --source-profile-id 26`。可增加 `--postgres` 和 `--s3` 选择相应组合。[复验入口](../server/live_llm_log_parse_stream_acceptance.py)
