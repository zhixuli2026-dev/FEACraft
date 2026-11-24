# FF-H02A 作业落库与 FF-H19 历史结束时间

2026-11-18，分别使用 SQLite 搭配文件存储、PostgreSQL 搭配 SeaweedFS，验证作业列表和详情。两轮都用独立数据库连接逐行读取状态、退出码和结束时间，在杀死原 API、启动新 API 后再核对公开响应。[SQLite 结果](../../tmp/live-cli-startup-bx1t89gg/job-history.json) · [PostgreSQL 结果](../../tmp/live-cli-startup-1nbfeccp/job-history.json)

| 场景 | 验收结果 |
|---|---|
| 正常、失败、取消和并发 | 两个同键并发提交只创建一个实际作业，运行时结束时间及退出码均为空。取消后验证实际进程停止，数据库记录取消和结束时间。随后两个真实命令以 0、7 退出，数据库分别记录成功、失败和对应退出码。空命令被拒绝。 |
| 重启和时间精度 | 原 API 和重启后的 API 均通过详情和列表返回与 SQL 精确相等的 Unix 时间戳。两个旧格式兼容用例没有 attempt：保存的微秒时间被保留，历史数据缺失结束时间时仍返回空值。 |
| 权限和组织共享 | 匿名查询被拒绝；其他组织用户无法读取。接受邀请并启用只读共享后，成员可读到相同的五行历史且不能取消作业。撤销共享后查询再次被拒绝。两种数据库中的原始作业字段经复核均保持一致。 |
| 执行者意外结束和原界面 | 历史故障验收包含真实收尾进程退出、持久化中断状态与结束时间、新 API 重读。旧版查询兼容验收覆盖四类终态、批次和日志；已有浏览器验收覆盖实际选择、阶段、尝试、旧作业和日志错误。[收尾故障](../../tmp/early-finalization-evidence-j19x8g7v/result.json) · [双库接口](../../tmp/job-query-compat-evidence-9kkw0di8/result.json) · [浏览器](../../tmp/legacy-query-browser-result.json) |

两个功能沿用既有数据表、公开路由和作业面板。无新增部署配置或迁移，旧版可直接读取相同字段。新增验收的旧格式行只写入临时数据库；三条现代作业记录均由实际命令产生。本次修正统一了无权访问及不存在的作业响应。每轮结束后，DeepSeek 通过文件工具核对该轮结果摘要，共 4 次真实请求。[验收入口](../server/live_llm_job_history_acceptance.py) · [SQLite 模型回执](../../tmp/live-cli-startup-bx1t89gg/live-result.json) · [PostgreSQL 模型回执](../../tmp/live-cli-startup-1nbfeccp/live-result.json)
