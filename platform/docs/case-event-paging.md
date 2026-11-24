# 算例审计历史的分块分页

`GET /cases/{id}/events` 在所属算例的协调事务中选定历史版本，随后释放事务和存储额度，再用现有的有界读取线程扫描 JSONL。页面沿用最新优先、总数、偏移和 `has_more`。首屏只保留本页记录，较深的页面按同一版本再扫一次。两次扫描共用请求期限，内存主要由当前记录和页面大小决定。

SQLite／Windows 文件系统和 PostgreSQL／SeaweedFS 的真实 HTTP 验收均使用 41,719,040 字节、20,480 条记录。覆盖首屏、15,000 条偏移、末页、超大偏移、横跨读取块的中文字符和混合换行。两种存储分别验证读取暂停时同算例普通保存可完成，版本被替换后返回 409，实际超时在读取线程退出后返回 504，随后可以重试。缺失的历史返回空页。

| 环境 | 分页请求的 Python 峰值分配 | 当前源码回执 |
| --- | ---: | --- |
| SQLite／文件系统 | 918,771 B | [实际分页与审计](../../tmp/case-event-fs-lodzzr88/case-event-paging.json) |
| PostgreSQL／SeaweedFS | 1,164,032 B | [实际分页与审计](../../tmp/case-event-s3-mxc89lza/case-event-paging.json) · [临时库回收](../../tmp/terminal-agent-postgres-iqm2xcot/cleanup.json) |

正常审计使用真实 API 创建算例、确认 A→V 和写入同阶段的第二次修订。逐项比较原始 JSONL 和分页结果，非法保存返回 422 且历史字节一致，另一个账户得到 404。

首次文件系统运行完成全部行为检查，退出回执处理遗漏了 Windows 关闭连接的错误类型。一次 PostgreSQL／SeaweedFS 扩展试验的并发保存超过夹具六秒等待。修正夹具退出识别和等待时间后，两个实际环境全量复验通过。[文件系统首轮](../../tmp/case-event-fs-bobvr_53/case-event-paging.json) · [远端首轮](../../tmp/case-event-s3-y3mjew4s/case-event-paging.json)

实际 DeepSeek 审计另见 [FF-A22](FF-A22-agent-case-event.md)。[复验入口](../server/case_event_paging_acceptance.py)
