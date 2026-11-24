# 正式 CLI 会话接管与启动取消收尾

2026-11-08，接续 [降权交接和持久分配](cli-managed-scope-allocation.md)。生产 `Session` 的 WSL 路径已采用新范围生命周期。本阶段验证实际正常启闭、启动取消和未知原生身份的保留；**API 猝死后的独立完整会话恢复、执行锁恢复及 CLI 内部模型推理仍未完成**。

## 生产行为

`Session.prepare` 在冻结传输 nonce/profile 之前创建并提交 `CliScopeAllocation`，许可真实 cgroup 分配；随后在同一 SQL 事务中创建正式会话、接管该范围并预留初始化额度。回滚时不会留下一个已接管但没有 session 的记录，取消时依据实际提交的行选择分配回收或会话收尾，不能只依赖内存里的 owner 对象。

新 profile 冻结 `native_management` 与 `scope_lifecycle`：范围、降权入口摘要、控制器/guardian/preflight 摘要、guardian 证据目录和生命周期限制。独立 guardian 准备提交后才能创建原生管理门，原生预登记提交后才许可进入 cgroup；root 登记必须与该预登记的真实 host/native 出生身份一致，bridge 必须为 UID 65534。旧 profile 保留原先的非 root 验证规则，历史行不会自动获得新保证。

关闭先排空保留的客户端，再核验 guardian 实际退出回执、实际 retire cgroup 并确认 absent、逐一核验已知 host/native/bridge 出生身份退出。`process_json.management` 保存分配、guardian、preflight 和独立 retirement。普通 stop 回执仍按原规则保存；有完整范围关闭凭据而没有普通 stop 的启动失败可以显示已清理，但 phase 保持失败，stop 仍为空，不伪造正常停止。

初始化额度新增 `verified_scope` 结算类型，必须与会话中经过验证的完整范围关闭凭据摘要一致；实际写者排空后才释放用户预留和节点 claim。`prepared`、旧 `verified_stop` 和旧初始化恢复类型继续使用各自规则。

新 WSL 会话给独立 guardian 配置 3,600 秒生命周期，心跳在该期限前 30 秒请求会话收尾，空闲上限仍为 900 秒。检查放在独立心跳中，不依赖可能较慢的存储扫描。本阶段核验实际 guardian 请求携带的期限，未等待完整一小时进行该时长的超时验收；与长 turn、fork、HTTP、全局预算的统一仍待交付。

## 实际验收

显式入口 `live_llm_cli_managed_session_acceptance.py --source-profile-id 26 [--postgres --s3]`；`--cancellation` 与 `--unknown-native` 为互斥的实际故障验收入口。所有运行使用用户授权的真实 DeepSeek，模型配置只读自开发库，密钥只进入隔离配置。没有运行历史模拟 LLM 回归。

### 正常会话

- [SQLite/FS](../../tmp/live-cli-startup-vadmmku6/managed-session-checks.json)
- [临时 PostgreSQL/真实 SeaweedFS S3](../../tmp/live-cli-startup-7ufjf6kc/managed-session-checks.json)

每套实际启动并关闭两种会话：无 case 的账户检查会话、关联真实算例的会话。调用安装的 Linux CLI initialize/account，校验实际 root/bridge UID、持久接管关系、完整关闭回执；已接管范围不能再由分配恢复入口回收。关联 S3 的会话执行实际 staging 准备与正常收尾。共 4 个真实应用会话，S3 服务已停止，临时 PG 已删除。

### 启动中取消

最终 [SQLite](../../tmp/live-cli-startup-un5o73q3/managed-session-cancellation-checks.json)与 [PostgreSQL/S3](../../tmp/live-cli-startup-a59nbx_p/managed-session-cancellation-checks.json)各覆盖 9 个位置，共 18 个场景：分配准备提交后、实际分配后、session 创建但事务尚未提交、guardian 实际准备后、guardian 登记尚未提交、native 预登记尚未提交、root 登记尚未提交、bridge 登记尚未提交、激活尚未提交。

暂停钩子位于真实生产调用之后；驱动取消真实启动任务，排空实际 OS 创建和清理，再检查 SQL 与实际回执。未创建 session 的分配以 abandon/retire 收尾；已提交的 session 保留 adopted，完整关闭凭据属于该 session。预留额度结清，节点 claim 解除，会话注册表移除。取消场景的 CLI 启动真实执行，但没有要求这些中断中的 CLI 进行模型推理。

[首轮失败](../../tmp/live-cli-startup-wtk53auu/live-result.json)在完成前 5 个边界后，于 native 预登记取消处暴露“实际完整范围已关闭但没有普通 stop，公开状态仍为未清理”的问题。该次真实模型调用为 0，原始 SQL、stop 和日志保留；修复独立 retirement 判定和 `verified_scope` 额度结算后，从新隔离环境完成全部矩阵。正常会话证据产生于该修复前，取消矩阵覆盖修复后的路径；归档时用当前读取器重读正常及历史真实记录验证兼容性。

### 未知原生出生身份

[实际未知身份场景](../../tmp/live-cli-startup-7xdb_b3l/managed-session-unknown-native.json)在实际原生 prepared frame 已读出、生产流程尚未接收到该返回值时取消。测试侧另行观察到该出生进程已退出、目标 cgroup 已不存在；生产日志没有收到该出生身份，仍保持 phase=failed、cleanup_verified=false、stop 为空，初始化额度继续保留。测试掌握的身份没有注入生产日志，没有凭 cgroup absent 就宣布全部原生 bootstrap 已结束。

这个保留行为是有意的，但相应恢复能力尚未实现。后续必须补齐原生预登记之前的持久交接/退出证据，不能把测试侧知道的 PID 或事后单次观察冒充生产恢复授权。

## 真实模型记录

五个成功运行的模型回执：[正常 FS](../../tmp/live-cli-startup-vadmmku6/live-result.json)、[正常 PG/S3](../../tmp/live-cli-startup-7ufjf6kc/live-result.json)、[取消 FS](../../tmp/live-cli-startup-un5o73q3/live-result.json)、[取消 PG/S3](../../tmp/live-cli-startup-a59nbx_p/live-result.json)、[未知身份保留](../../tmp/live-cli-startup-7xdb_b3l/live-result.json)。共 **10 次 api.deepseek.com / deepseek-flash 调用、42,978 tokens，全部 HTTP 200，Mock 0 次**。

模型实际选择 read_text_file 读取各运行的证据摘要，流式答案与持久历史一致。这些是平台 Agent 的真实模型验证，**不等于 CLI 内部 turn、工具选择、审批或 fork 已通过模型验收**。所有测试 API 已退出；开发数据库未写入或迁移。本阶段没有新增数据库迁移，沿用 `e8c3f09624ab`。

## 下一步与未交付范围

1. 为持久 guardian/preflight 已齐备的失联 session 实现授权恢复 API：核验真实 owner 已退出，在 SQL 锁外观察和关闭范围，按原 profile/process/version 条件提交独立恢复凭据；原 stop 保留，重复和跨 worker 并发恢复幂等。
2. 补齐原生 prepared frame 观察前的未知窗口、guardian 准备与登记之间的中断、实际 API 猝死后的正常/取消交错。未知身份和旧协议记录继续保留，不能自动升级。
3. 将会话恢复与初始化结算、启动请求、turn、审批、fork 及 conversation execution 锁绑定；不能仅凭初始化额度或分配范围已回收就释放执行锁。初始化恢复的 process 摘要还需与新增生命周期日志的后续演进保持一致。
4. 对新增 guardian 请求/准备/回执及失败诊断文件补齐用户和节点的持久峰值预留与收尾结算；当前它们位于账户私有目录、运行期会计量，但新增证据写入的完整前置预算仍未交付。继续旧根/旧实现维护、OS 硬额度和全局预算。
5. 接入并验收 CLI 内部真实模型回合、真实工具选择与审批、turn/fork 恢复；遇到真实服务或协议不兼容就报告阻碍，不用模拟模型补过。

主线 **33/53 完成，剩 20（3 进行中、17 待办）**；资源 **11/17、剩 6**；全功能 **295 项仍待整项交付验收**。正式会话接管是本阶段新增进展，整项恢复和交付目标仍未完成。
