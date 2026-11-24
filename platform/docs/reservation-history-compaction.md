# S3 已结清预留回执压缩

后台存储维护分批压缩账户与算例两张预留表的历史回执。每轮按主键游标分别读取最多 32 行。状态必须为已结清、剩余额度为零，且回执符合已知的关闭或远端写入证明。活动预留、未知回执和缺少证明的行保持原样。压缩前后逐字节比较，只有文本确实变短才提交。提交再次比对原回执和结清条件，保留原 ID、绑定、时间戳、上传分块引用和 S3 对象。

两张表继续使用原文本列。应用读取原有 JSON 文本或解码带版本和 SHA-256 的压缩回执，返回原始 JSON 字符串。解码校验长度和摘要。直接执行 SQL 导出的工具会看到压缩格式。本文统计的是回执文本字节，数据库物理文件与 Windows C 盘可用空间另行测量。

## 恢复原始 JSON

切回旧版代码前，停止所有实例的后台存储维护，并使用当前版服务环境和数据库连接配置执行恢复命令。在 `platform/server` 下先检查两张表的压缩记录数量，再应用恢复。

```powershell
.\.venv\Scripts\python.exe -B reservation_history_restore.py
.\.venv\Scripts\python.exe -B reservation_history_restore.py --apply
```

核对输出中的 `still_compressed` 两项均为零。恢复仍按页提交，校验失败的记录原样保留，命令返回非零状态，修复后可再次执行。后台维护开关为 `VIBEFLOW_STORAGE_MAINTENANCE_ENABLED`。

## 2026-11-19 验收

SQLite 和临时 PostgreSQL 分别连接独立的实际 SeaweedFS S3。测试在 S3 成功写入后丢弃回执，通过正常对账核验原对象。真实分块上传保留了 25 字节的已写进度。随后重启 API，后台扫描每表每页设为 2 行。两种数据库均跨过第一批不完整证明，压缩目标回执，保留 3 条活动与 3 条缺少证明的记录。重复提交结算保持回执，源对象 ETag、账本 ID、时间戳及上传分块引用一致。修改压缩摘要会被拒绝，恢复命令将隔离数据中的全部压缩行还原，剩余数均为零。[复跑入口](../server/live_llm_reservation_history_acceptance.py)

| 数据库 | 账户分块回执 UTF-8 字节 | 算例核验回执 UTF-8 字节 | 恢复行数 | 验收证据 |
| --- | ---: | ---: | ---: | --- |
| SQLite | 334 → 235 | 197 → 193 | 7 | [存储与恢复](../../tmp/live-cli-startup-9whql6ll/reservation-history.json) · [真实模型](../../tmp/live-cli-startup-9whql6ll/live-result.json) |
| PostgreSQL | 334 → 240 | 197 → 193 | 6 | [存储与恢复](../../tmp/live-cli-startup-mrpzdog0/reservation-history.json) · [真实模型](../../tmp/live-cli-startup-mrpzdog0/live-result.json) |

验收报告的读取回合共发起 4 次真实 `deepseek-flash` HTTP 请求，均为 200，两个回合精确回读各自文件标识，模拟调用为 0。初轮验收的隔离探针未给 SQL 转换列命名，读取接口返回 500。为探针补上列别名后，两种组合复跑通过。两轮初测均在模型调用前结束。[SQLite 初测](../../tmp/live-cli-startup-514q9e_h/live-result.json) · [PostgreSQL 初测](../../tmp/live-cli-startup-8dca36a4/live-result.json)

两种数据库另复跑真实 S3 上传中断、旧终态恢复及迟到请求拒绝。两组分别经历两次隔离 API 退出，活动额度可恢复，取消前后对照及替代上传均通过。复验另有 4 次真实 `deepseek-flash` HTTP 200，合计 8 次，模拟 0。最终四个 API 和四个独立对象存储已停止，两座临时 PostgreSQL 库已删除。[SQLite 上传复验](../../tmp/live-cli-startup-mufkl1xf/upload-cleanup.json) · [SQLite 真实模型](../../tmp/live-cli-startup-mufkl1xf/live-result.json) · [PostgreSQL 上传复验](../../tmp/live-cli-startup-w4uwlk9r/upload-cleanup.json) · [PostgreSQL 真实模型](../../tmp/live-cli-startup-w4uwlk9r/live-result.json)
