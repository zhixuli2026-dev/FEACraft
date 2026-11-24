# 2026-11-07 真实模型调用失败记录

本记录核实用户提醒后的实际调用情况。禁止模拟 LLM 的要求继续执行；服务失败不回退到固定回复或 mock provider，不将基础设施检查算作模型验收。

| 算例 | 实际服务与模型 | 推理请求 | 结果 |
| --- | --- | --- | --- |
| PostgreSQL 双 API 重复取消与清理 | api.deepseek.com / deepseek-flash | 1 次 | 上游 HTTP 500，模型验收失败 |
| 用户提醒后的隔离重试 | api.deepseek.com / deepseek-flash | 1 次 | 模型列表 HTTP 200；推理 HTTP 500，平台翻译接口返回 502，验收失败 |

两份上游记录的 Date 响应头分别为 2026-11-07 07:07:31 UTC 和 07:13:19 UTC（北京时间 15:07、15:13）。这是上游响应头时间，非独立计费凭证。本记录涉及 2 次真实推理请求，成功 0 次，模拟 0 次；没有成功 usage，不推算 token 数。流式回复和模型工具选择未因本次重试得到通过证据。

- 首次失败：[模型结果](../../tmp/sandbox-recovery-ui-retirement-oddq3v9s/live-result.json)、[基础设施结果](../../tmp/sandbox-recovery-ui-retirement-oddq3v9s/image-concurrency-checks.json)、[PostgreSQL 算例结果](../../tmp/image-postgres-d5ezk2ml/result.json)。5 项实际基础设施检查已记录通过，但整套 `passed=false`，不能称为整体验收通过。
- 重试失败：[模型结果](../../tmp/live-deepseek-c4aeelwt/live-result.json)、[隔离算例结果](../../tmp/live-deepseek-c4aeelwt/result.json)。执行入口为 `platform/server/live_llm_acceptance.py --source-profile-id 26`，复用本地授权凭据；执行前已阅读入口和 `testsupport.py`，HTTP 观察器转发真实请求，没有替换模型响应。开发数据库未修改。

当前阻碍是两次真实推理请求均收到上游 500；仅凭该响应不能判断服务内部原因。保留失败证据，后续真实模型验收需要再次实际成功调用；不靠改断言或模拟补齐。

主线仍为 33/53 完成，剩余 20 项（3 项进行中、17 项待办）；资源任务 11/17 完成、剩余 6 项；295 项全功能最终验收要求继续有效。本记录不增加任何完成项。回执提交中断的新测试文件尚未执行，不作为通过证据；现有源码快照不包含这些未封存改动。
