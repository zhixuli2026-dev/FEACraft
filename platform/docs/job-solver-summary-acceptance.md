# FF-H06 作业日志结构化摘要验收

作业收尾使用同一版本的日志逐块生成 `Job.parsed_summary`。日志先经过本地文件身份或 S3 对象版本校验，每次读取 64 KiB。读取、解析和序列化在数据库会话外完成，写回时核对作业、算例、日志路径和原有摘要，避免覆盖并发修改。生成结果仍沿用 `vibeflow.solver-log.v1`。

无法解析或超出浮点范围的数值跳过该条记录。可用的时刻、Courant、残差、迭代次数、耗时及警告继续参与计算。作业摘要跳过超过 4 MiB 的单行诊断，保留同一日志的其他行。直接解析接口对此仍返回 413，原始字节可分段读取。历史摘要中的非有限数值会触发已有的 `record_issues`，原摘要保存在数据库，详情和列表可正常返回。

修复前，Windows 测试作业将 `TimeStep ... = 9e999` 写入日志，旧代码保存了非标准 JSON 数值，随后详情请求返回 HTTP 500。同批日志还包含截断的残差与耗时字段。已保留首次失败回执。[修复前故障](../../tmp/live-cli-startup-0ytiybj4/live-result.json)

## 实际验收

| 数据库与存储 | Windows 作业与 API 重启证据 | 真实 DeepSeek 回执 |
|---|---|---|
| SQLite 与文件系统 | [实测](../../tmp/live-cli-startup-e4bt4c3f/solver-log-format.json) | [模型](../../tmp/live-cli-startup-e4bt4c3f/live-result.json) |
| PostgreSQL 与文件系统 | [实测](../../tmp/live-cli-startup-icievu4f/solver-log-format.json) | [模型](../../tmp/live-cli-startup-icievu4f/live-result.json) |
| SQLite 与 S3 | [实测](../../tmp/live-cli-startup-jip6rrsr/solver-log-format.json) | [模型](../../tmp/live-cli-startup-jip6rrsr/live-result.json) |
| PostgreSQL 与 S3 | [实测](../../tmp/live-cli-startup-b56ccnj7/solver-log-format.json) | [模型](../../tmp/live-cli-startup-b56ccnj7/live-result.json) |

每组运行三个 Windows 作业，脚本通过实际子进程输出标准及故障日志样本。成功作业核对八种求解器标记、时刻、残差细节、Courant、耗时与无换行的 End。失败作业保留退出码 7、有效时刻与残差及一条致命错误。超长诊断作业保留该行前后两个时刻。日志解析接口与成功和失败作业的持久摘要逐字段一致。实际重启 API 后复读摘要，跨账户访问仍返回 404；三个同时读取同一增量游标的请求保持一致，追加后的半行顺利接续。坏游标返回 400，重写和截断返回 409。三条历史记录在详情和列表保留可见性。四组独立的模型工具回读产生 8 次真实 `deepseek-flash` HTTP 200，模拟调用为 0。

原有 11.3 MiB 日志增量验收在四种组合上复跑通过，完整解析的 Python 峰值分配最高约 0.74 MiB，已有游标的续读最高约 0.19 MiB。相同账户仍可在解析期间写入，新旧文件版本发生冲突时原读请求返回 409。该组另有 8 次真实 `deepseek-flash` HTTP 200。[SQLite 文件](../../tmp/live-cli-startup-r53pxn5j/log-parse-stream.json) · [PostgreSQL 文件](../../tmp/live-cli-startup-15vtfip2/log-parse-stream.json) · [SQLite S3](../../tmp/live-cli-startup-wh3f3igy/log-parse-stream.json) · [PostgreSQL S3](../../tmp/live-cli-startup-emafhw2q/log-parse-stream.json)

界面曲线与作业日志的展示证据见 FF-H03 和 FF-H04 验收。本轮签收 FF-H06 的自动摘要，并补 FF-H05 八种格式及服务重启后的旧游标验证。FF-H05 后续另完成浏览器增量验收。[界面与曲线](residual-chart-acceptance.md) · [浏览器增量续读](residual-log-cursor-acceptance.md)

用虚拟环境运行 `live_llm_solver_log_format_acceptance.py --source-profile-id 26`，可追加 `--postgres` 和 `--s3`。变更沿用已有数据库表，应用版本可直接回滚，旧版会重新暴露非有限摘要的详情错误。[验收入口](../server/live_llm_solver_log_format_acceptance.py)
