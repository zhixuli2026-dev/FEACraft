# CLI 所属 API 退出后的独立会话恢复

2026-11-08，接续 [正式会话接管](cli-managed-session-lifecycle.md)。持久 guardian 和原生 preflight 已齐备的 managed WSL 会话，现可通过 `POST /api/v1/codex/sessions/{id}:reconcile-scope` 独立恢复完整进程范围并结算初始化额度。执行锁、turn、审批、fork 和 staging 恢复仍是后续工作。

## 恢复依据与事务边界

入口核验当前用户、会话归属、原分配的 adopted 关系及初始化预留。原 owner 的实际出生身份必须确认退出；租约过期、PID 不存在的猜测或客户端断开都不能替代该证据。缺失 guardian/preflight 的会话返回冲突并保留未验证状态，旧协议会话也不会自动升级。

guardian 回收、cgroup retire 与 host/native/bridge 实际退出观察都在 SQL 事务之外进行。随后重新检查归属，按原 version、phase、profile、process、stop 和 owner 绑定执行 CAS，原子提交失败终态、独立 recovery/retirement 凭据和额度结算。原 stop 日志不变，不生成正常停止或模型完成结果。节点 claim 在提交后释放，可重复完成。

初始化恢复现在对不可变写者身份（schema/root/bridge）绑定；追加范围生命周期凭据不再使先前的初始化退出回执失效。旧格式回执仍按其原摘要校验；对于本入口追加的 recovery，先重建原始 admission 日志再验证旧摘要。

PostgreSQL 额度锁冲突最多尝试 6 次，等待 50、100、200、400、800 毫秒；每次失败事务退出后才等待。重试复用已取得的 OS 证据，不重复原生范围操作；持续争用仍返回 HTTP 409。恢复并发上限 2、单请求期限 90 秒。这些局部限制不代表全局资源预算已完成。

## 实际基础设施验收

显式入口：`platform/server/live_llm_cli_managed_recovery_acceptance.py --source-profile-id 26`，可选 `--postgres --s3`；故障模式 `--recovery-crashes`、`--refusals`、`--quota-race` 互斥，最后一种要求 PostgreSQL。

| 验收 | 实际范围 | 证据 |
|---|---|---|
| SQLite/FS 会话恢复 | preflight、root、bridge、private_prepared、active、stopped 六处真实 API 退出，每处两个独立 worker 并发恢复 | [结果](../../tmp/live-cli-startup-uemem1xo/managed-session-recovery-checks.json) |
| PostgreSQL/S3 会话恢复 | 同一六处矩阵，真实临时 PG 与 SeaweedFS | [结果](../../tmp/live-cli-startup-i1qpxkh7/managed-session-recovery-checks.json) |
| SQLite 恢复自身中断 | 实际 OS 观察后、SQL 提交前、提交后、节点 claim 释放后四处杀掉恢复 API，再由新 API 完成 | [结果](../../tmp/live-cli-startup-bpxwewu6/managed-recovery-interruptions.json) |
| PostgreSQL 恢复自身中断 | 同一四处矩阵 | [结果](../../tmp/live-cli-startup-5s02v1uy/managed-recovery-interruptions.json) |
| 拒绝不完整证据 | 实际未登记原生出生身份、实际 Windows 旧协议会话；返回 409，未知写者额度保留 | [结果](../../tmp/live-cli-startup-ms9vulie/managed-recovery-refusals.json) |
| 真实 PG 额度锁争用 | 短暂锁争用 1 次拒绝后恢复；持续持锁 6 次拒绝后返回冲突，释放锁后恢复；每轮内部只观察一次范围 | [结果](../../tmp/live-cli-startup-bll0focb/managed-recovery-quota.json) |

两套六处矩阵共 12 个独立 peer；成功场景共 31 次 API 进程终止，其中恢复自身的八个场景各终止原 owner 和恢复 owner 两次。并发/重复请求得到相同持久回执；其他账户被拒绝。bridge 场景先完成初始化恢复，再恢复完整范围并重复初始化恢复，原初始化回执保持不变。

[首次 PG/S3 失败](../../tmp/live-cli-startup-0v0y1s4b/live-result.json)只完成前两处，第三处的并发请求遇到 `storage_quota_busy`。真实模型调用为 0，失败原件保留。以上 PG/S3 完整矩阵及独立争用测试验证了修复后的行为。失败运行的临时数据库已删除、S3 已停止；未取得第三处完整成功回执，不能补记为通过，也不把数据库删除视作未知范围已清理的证据。

## 真实模型验收与边界

六个成功运行在基础设施场景后，通过平台 Agent 调用真实 `api.deepseek.com / deepseek-flash`，由模型选择 `read_text_file` 读取本次证据摘要，验证流式回复与持久历史。共 **12 次真实请求、51,496 tokens、全部 HTTP 200、Mock 0 次**。逐次时间、响应 ID、源日志摘要见 [核查记录](../../tmp/real-llm-reminder-audit-20261108.json)。记录核查本身没有新增模型调用。

这不证明 CLI 内部模型回合、审批和 fork 已通过。基础设施故障钩子暂停真实调用或终止真实进程，没有替换模型回复。测试使用隔离账号、SQLite 或临时 PostgreSQL，开发库只读配置，密钥不出现在报告中。所有成功运行的测试 API 已退出，临时 PG 已删除，S3 服务已停止。

## 后续任务与计数

1. 为原 owner 已退出且完整会话范围已核验的执行锁提供独立恢复：原子保存中断状态、过期审批、终态事件和锁状态；不能伪造 native completion 或重新执行未知工具。
2. 完成启动请求、turn/fork 与 staging 的关联恢复；跨 worker 并发、提交前后 API 退出必须有实际证据。
3. 补齐 guardian 准备到持久登记、原生 prepared frame 到生产 preflight 之间的未知窗口；旧记录保留原有验证级别。
4. 为 guardian 请求、准备、回执和诊断文件补齐持久写前峰值预留，继续旧根维护、统一期限、硬额度及全局预算。
5. 接入 CLI 内部真实模型推理、工具选择和审批验收；协议不兼容时修复或记录阻碍，禁止 Mock 替代。

主线 **33/53 完成，剩 20（3 进行中、17 待办）**；资源 **11/17，剩 6**；全功能 **295 项仍待整项交付验收**。N4-05 仍在进行，不能用本次局部范围恢复验收减少整项剩余数。
