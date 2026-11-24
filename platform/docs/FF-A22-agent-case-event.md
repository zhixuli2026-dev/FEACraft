# FF-A22 真实 Agent 写入的算例审计

2026-11-19 使用已授权的 DeepSeek Profile 26 在独立 SQLite／文件存储完成一轮真实 Agent 操作。`deepseek-flash` 实际提出 `propose_text_replacement`，申请修改同一算例的 `intent.userIntent`。用户批准后，工具成功执行并留下持久结果。官方服务有两次 HTTP 200，一次生成提案，一次在批准后继续完成原回合。模拟调用为 0。[真实模型回执](../../tmp/live-cli-startup-k0hyimja/live-result.json) · [提案](../../tmp/live-cli-startup-k0hyimja/model-proposal-events.json) · [批准和执行](../../tmp/live-cli-startup-k0hyimja/model-approval-events.json)

待批准期间，caseDict、修订与原时间线保持一致。独立账号批准和查询都返回 HTTP 404，未触发模型续跑。所有者批准使修订由 1 到 2，新增一条 `caseDict.updated`，`source=agent`，阶段保留 A。重复批准返回 HTTP 409，历史记录与模型请求数保持一致。记录的文件原件、raw API 字节和公开 `/events` 分页相互核对，最新 provenance 指向修改后的字典摘要。[综合回执](../../tmp/live-cli-startup-k0hyimja/case-agent-audit.json) · [审计 JSONL](../../tmp/live-cli-startup-k0hyimja/case-events.jsonl) · [分页 API](../../tmp/live-cli-startup-k0hyimja/case-events-api.json)

本次实际 Agent 验收覆盖本机文件存储与 SQLite。真实 API 已退出。自动审批拦截了本次临时加密存储密钥文件的清理命令，本次验收仅发布脱敏回执。复跑入口已在正常退出后加入本次测试专用的清理步骤。[验收入口](../server/live_llm_case_agent_audit_acceptance.py)

```powershell
& 'platform/server/.venv/Scripts/python.exe' -B -X utf8 `
  'platform/server/live_llm_case_agent_audit_acceptance.py' --source-profile-id 26
```
