# FF-J11 组织审计与外部锚点

组织审计使用共享数据库，每个组织独立递增序号与 SHA-256 链头。拥有组织管理权限的用户从设置页或 `/api/v1/org/{id}/audit` 查询，接口支持按动作和操作者筛选及分页。读取在固定链头内逐条核验，发现缺失、顺序变化、索引字段或正文变更时报告链无效。既有 JSONL 在首笔写入时核验并导入原哈希，原档案保留。升级多副本前让所有迁移实例读到同一份原归档，再启动处理写入的实例。

已接入成员和套餐变更、运行环境及模型配置、作业与批次提交、工作流和作业取消、Agent 审批决定与命令、原生 Codex 审批与命令、终端及渲染会话生命周期。审计的命令字段使用摘要。交互终端的审计粒度为会话，记录运行环境与所绑定的配置编号。审计中的取消事件记录受理，真实退出仍使用原生作业或会话回执。

SQLite 和 PostgreSQL 以真实双 API 核对历史导入、并发写入、筛选分页、哈希链完整性和重启读取。最终 PostgreSQL 运行验证了 16 条记录、真实 Windows 作业取消及终端关闭。两次 API 请求复用同一幂等提交，数据库只有一条对应作业记录。修改一条早期记录会触发核验失败，即使当前页面只筛选作业提交。 [SQLite](../../tmp/org-audit-primary-boh39o1g/organization-audit.json) · [最终 PostgreSQL、Edge 与对象锁](../../tmp/org-audit-primary-5xbh9gfa/organization-audit.json) · [Edge 过滤画面](../../tmp/org-audit-primary-5xbh9gfa/audit-center.png) · [临时数据库清理](../../tmp/terminal-agent-postgres-b6_q0dyr/cleanup.json)

独立运行的认证 SeaweedFS 使用启用 Object Lock 的专用桶。条件写入复用原版本，同一序号不同摘要被拒绝，锁定版本在保留期内删除返回 403。指定版本比对数据库通过，改动历史会使比对失败。锚点仅包含部署标识、组织编号、序号和链头摘要。数据库保留完整事件，锚点可由另一个账户维护并独立读取。 [S3 对象锁](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html) · [条件写入协议](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html) · [版本保留查询](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObjectRetention.html)

真实模型侧，在 API Agent 中批准实际 Windows 命令后记录 `agent.tool.approved` 和 `agent.command.admitted`，官方 DeepSeek HTTP 200 两次。Windows Codex 跨实例停止记录 `agent.cancel.requested`，官方 Responses HTTP 200 一次。非特权 WSL Codex 对隔离算例文件的真实审批通过，数据库各有一条 `codex.approval.claimed` 与 `codex.command.started`。WSL 验收报告有 1 个实际模型回合和实际审批写入，未观测逐次上游 HTTP 状态。该脚本首轮缺少显式 Linux Codex 路径，在启动时返回 503，模型调用 0，指定已安装的 ELF 后复验通过。所有验收模拟调用为 0。 [API Agent](../../tmp/live-f24-f25-agent-shell-oaxprpf3/live-result.json) · [Windows Codex](../../tmp/live-f34-native-owner-70g61kt7/native-peer-stop-result.json) · [WSL Codex 审批](../../tmp/live-cli-approval-5so5kcz0/live-result.json)

## 部署和核验

新版 API 与 worker 共享迁移至 `56e2d59f03c1` 的数据库。外部锚点使用与业务存储分离的桶和单独的写入身份。设置 `VIBEFLOW_AUDIT_CHECKPOINT_BUCKET`、`VIBEFLOW_AUDIT_CHECKPOINT_DEPLOYMENT` 和适用的 AWS 凭据链。兼容存储可设置 `VIBEFLOW_AUDIT_CHECKPOINT_ENDPOINT`。部署方可按实际留存周期定时运行命令，并在独立位置保存返回的 `version_id` 和回执。

```powershell
python -m app.services.audit_checkpoints --retention-days 365 --org 1
python -m app.services.audit_checkpoints --verify-receipt audit-checkpoint.json
```

复核时指定原回执。校验读取锁定版本原字节和实际保留时间，并比对数据库相同序号的哈希。独立对象存储测试使用同一物理宿主，既有运行部署没有启用外部上传。 [验收入口](../server/organization_audit_acceptance.py)
