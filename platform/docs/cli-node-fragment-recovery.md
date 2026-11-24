# 节点预留原子发布与残片恢复

2026-11-08，接续 [未许可初始化恢复](cli-unreleased-initialization-recovery.md)。原实现直接向正式 `.json` 写入，写入中断会留下无法解析的 JSON，阻断节点计量和全目录对账。本阶段修复这一写入路径，保留原生初始化文件的部分写入作为下一项工作。

## 发布、计量与保留

`cli_node_capacity.acquire` 在既有节点 OS 锁内先写入 `<session_hex>.<完整记录SHA256>.cli-pending`，实际 flush/fsync 后原子 rename 为正式 `<session_hex>.json`，再同步目录（POSIX）；方法成功返回后才允许创建原生进程。所有合作发布者共用该锁，已存在目标不覆盖，悬空链接也按已存在处理。正式记录格式保持不变。

未完成的 pending 文件按原生初始化最大值 **32,768 字节**保守预留，其文件本身继续计入节点实际库存；不需要解析半截 JSON。识别要求精确文件名、普通文件、单硬链接和最多 16 KiB 元数据。未知文件或非法类型仍拒绝计量，不把失败当作零。完整记录正常发布后按实际初始化字节保留。

恢复先提交持久初始化凭证，然后针对该会话的精确 ticket 处理节点文件，不再先解析全节点的其他记录。正式记录完整且字节完全一致时按原流程删除；pending 或旧式正式文件的残片只有在**确为预期记录的完整前缀**时，才原子转为 `.cli-retired-pending` 或 `.cli-retired-published`，保留原字节和哈希。保留文件不再占用未来初始化额度，但其实际文件字节仍计量；不做 TTL 淘汰。非前缀内容、类型/链接异常或已存在的不同审计内容均拒绝改写。重复恢复不会覆盖第一份残片。

`settlement_ticket` 统一核验 prepared、可信停止、initializer_exited 和 initializer_not_released 凭证。泛化扫描发现 pending 文件后，还要在 SQL 中检查 user_id，避免另一用户结算自己的额度时处理他人的残片。存在 pending 仍阻止历史删除；已保留的审计文件不冒充活动预留。

这仍是合作写入的逻辑额度。旧 worker 不识别新文件名会拒绝计量，部署须协调相关 worker；回滚须保留兼容读取或先完成审计文件维护，不能直接清空这些文件。Windows 文件同步和原子重命名覆盖进程中断，本阶段不声称任意磁盘/断电持久性。

## 实际验收

显式入口 `live_llm_cli_node_fragment_acceptance.py --source-profile-id 26 [--postgres --s3]`。固定测试回调在真实文件 I/O 边界暂停，随后实际 TerminateProcess API，再独立重启恢复。没有模拟模型或 CLI 推理。

| 中断或故障 | 结果 |
| --- | --- |
| pending 已创建但为空 | API 退出后保留 32 KiB 逻辑额度，核验后保留空残片并结清 |
| pending 只写完一半且已 fsync | 保留半截原字节；双 worker 恢复得到同一凭证 |
| pending 已完整 fsync、尚未发布 | 完整临时记录作为审计文件保留，额度结清 |
| 已原子发布、调用尚未返回 | 精确正式记录正常结清，未创建原生进程 |
| 旧式正式文件位置的半截内容 | 计量先拒绝；显式定向恢复无需全目录解析，原残片保留 |
| pending 被改成不匹配内容 | 实际返回 502，内容和哈希保持；恢复测试原字节后再重试成功 |

最后两种包含明确注入的文件系统故障：旧式位置使用本轮实际 v3 ticket，不冒充历史未知记录的升级；篡改/恢复仅针对该隔离测试自己创建的文件。未制造真实磁盘写满。

[SQLite/FS 检查](../../tmp/live-cli-startup-j4t8zw0v/cli-node-fragment-checks.json)、[PostgreSQL/S3 检查](../../tmp/live-cli-startup-i6h1rdch/cli-node-fragment-checks.json) 各 16 项，合计 **32 项实际基础设施检查、10 次真实 API 中断、10 个独立恢复 worker**，另记录 **4 项明确文件系统故障条件**。每个中断窗口均检查另一账户拒绝及重复/并发恢复。两套环境最后都实际启动并关闭 Linux CLI，已验证正常停止，保留残片不会阻止后续节点计量。

八份审计残片原字节及哈希保留，全部测试初始化额度已结清。测试 API、独立恢复 worker 和对象存储均退出，临时 PostgreSQL 已删除，开发数据库未写入。中断会话仍保留原始空 process/stop，不将初始化额度恢复算作完整 CLI 会话清理。

## 真实模型与后续

每套环境结束后，真实平台 Agent 调用 read_text_file 读取该套检查摘要，流式回答与持久历史完全一致。合计 **4 次 api.deepseek.com / deepseek-flash，全部 HTTP 200，17,142 tokens，MOCK 0 次**。[SQLite 回执](../../tmp/live-cli-startup-j4t8zw0v/live-result.json)、[PG/S3 回执](../../tmp/live-cli-startup-i6h1rdch/live-result.json)。CLI 内部模型回合仍未验收。

后续继续原生私有 home/config/owner 文件的部分写入恢复、真实受限文件系统空间故障、完整 CLI 范围停止及会话/执行锁结清、旧根/实现升级维护、审计文件安全维护、OS 硬额度及真实 CLI 模型/审批/turn/fork。

主线 **33/53 完成，剩 20（3 进行中、17 待办）**；资源 **11/17、剩 6**；全功能 **295 项**目标保持，N4-05 仍进行中。
