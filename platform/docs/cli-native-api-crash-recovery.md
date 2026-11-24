# API 崩溃与原生初始化残片的组合恢复

2026-11-08，接续 [原生初始化事务](cli-native-initialization-transaction.md)。本阶段把原生文件部分写入接入实际平台启动链：登记 owner、host/native root 和 bridge，提交放行许可，再由该 bridge 执行生产初始化器。验证 API 此时猝死后的文件保留、独立额度恢复及同一账户再次启动。过程中发现并修复了 [FS 算例创建重复扫描](case-creation-node-batch.md)，未改变数据库结构。

## 故障注入与验收范围

显式入口 `live_llm_cli_native_api_crash_acceptance.py --source-profile-id 26 [--postgres --s3]`。测试在隔离目录复制固定 bridge、identity、probe 和初始化器；只在初始化器副本末尾增加实际 I/O 后的暂停钩子。副本由真实启动及恢复流程冻结、校验源码摘要；生产文件保持原样。暂停不生成协议结果，不替代模型或 CLI 推理。每次写入/发布仍执行生产函数与真实文件系统调用，驱动随后直接终止实际 Windows API 进程。

两套环境分别为 SQLite/FS 与临时 PostgreSQL/真实 SeaweedFS S3。每套覆盖 7 个边界：暂存目录已创建、owner 部分写入、owner 已同步、config 部分写入、config 已同步、owner 已发布、home 已发布但暂存目录未清理。合计 **14 个组合恢复场景、14 次实际 API 中断、14 个额外独立恢复 API worker、14 次同账户真实 CLI 重启及正常停止**。

每个场景均证明：存活 owner 的额度不能回收；实际 API 被终止后 owner/host/native/bridge 身份均观察为 exited；原始 process/stop 日志及残片字节不变；其他账户返回 404；两个真实 API worker 并发恢复得到同一份持久 `initializer_exited` 回执，用户额度结清且节点 claim 解除；重复恢复幂等。随后保持同一 user/native selection，启动真实 Linux CLI，补完原残片、去掉空暂存目录、保留精确配置/owner，完成 prepared 额度结清与正常停止。

新会话成功不修改旧会话的 stop 记录；旧记录仍未获得完整 CLI 范围清理证明。本阶段证明初始化额度和同账户重用，**不把根进程退出冒充所有后代清理，也不把新会话停止当作旧会话停止**。

## 实际证据

- [SQLite 组合恢复](../../tmp/live-cli-startup-6wb1gzrj/native-api-crash-checks.json)
- [PostgreSQL/S3 组合恢复](../../tmp/live-cli-startup-_wv3f0nn/native-api-crash-checks.json)
- [SQLite 真实模型回执](../../tmp/live-cli-startup-6wb1gzrj/live-result.json)
- [PostgreSQL/S3 真实模型回执](../../tmp/live-cli-startup-_wv3f0nn/live-result.json)

两套平台 Agent 各以真实 read_text_file 工具选择读取本套证据摘要，并核对流式答案与持久历史。**api.deepseek.com / deepseek-flash 共 4 次，全部 HTTP 200，17,132 tokens，MOCK 0 次**。这是平台 Agent 验收；CLI 内部推理仍未验收。

前序 PostgreSQL/S3 全矩阵另有 2 次真实模型调用、8,626 tokens；本阶段连同创建额度/取消及复制验收累计 **16 次、68,824 tokens**。前序 SQLite 在完成 6 个边界后准备账户/算例时出现 30 秒 HTTP 超时，0 次模型；加入逐请求耗时跟踪后，第二轮 7 个 CLI 场景全部完成，但真实模型算例创建 `POST /workspaces/16/cases` 再次超时，仍是 0 次模型。[首次失败](../../tmp/live-cli-startup-__lefova/live-result.json)和[第二次失败](../../tmp/live-cli-startup-a55yaj0d/live-result.json)均保留。最终 SQLite 矩阵是在修复重复扫描后完整重跑，仍使用原 30 秒 HTTP 客户端期限并成功完成模型验收；最终 PG/S3 崩溃矩阵在该 FS 专属修复前通过，修复后另有 PG/S3 创建和复制实际兼容检查，范围分别记录。

失败环境的[后续只读计量](../../tmp/cli-native-api-latency-observation.json)起初只证明单次扫描约 2.375 秒，不能独立解释请求超时。进一步的[真实算例创建诊断](../../tmp/cli-case-latency-lt2gdgbl/diagnostic.json)发现 15 次全节点扫描累计 31.388 秒，整个请求 32.844 秒；[修复后](../../tmp/cli-case-latency-6xzzoy81/diagnostic.json)变为一次扫描、整个请求 3.735 秒。解决的是本次可重复的逐文件重复扫描，其他已登记的慢扫描或不可中断 I/O 限制仍未全部关闭。

测试 API 和独立 worker、正常 CLI 及原生进程均已退出，对象存储已停止，临时 PG 已删除。开发数据库仅只读取授权模型配置，未迁移或写入。原生残片及测试证据按审计需要保留。只有本测试自己的暂停控制文件被移除。

## 剩余交付

继续完整 CLI 范围停止/会话/执行锁恢复、旧根/旧实现和历史残片维护、Session.access/turn/fork 收尾、CLI 内部真实模型与审批、OS 硬额度和全局预算。此前直接初始化器的真实 ENOSPC 已单独验证，本轮不声称平台 API 路径完成了磁盘硬配额集成。

下一实现块的代码约束已核对：现有 `OwnedProcess.prepare_guardian` 能保留独立 Windows Job 守护回执；镜像请求已使用 `native_scope_control` 和原生预登记。但 CLI 的 `OwnedStdio` 当前明确以 nobody 启动，现有 cgroup 接入只允许 root gate，不能直接设置 `native_scope` 后放宽 UID 检查。应先实现受固定入口约束的 root 管理门与 nobody bridge/CLI 交接，并保持用户进程非 root；持久提交范围分配、守护准备及许可后才能创建/放行业务。随后把两个范围的实际关闭凭证绑定原会话，按版本条件更新会话/执行锁；旧记录不自动获得新协议保证。各步骤均需实际进程中断、并发恢复、边界拒绝和真实模型证据。

主线 **33/53 完成，剩 20（3 进行中、17 待办）**；资源 **11/17、剩 6**；全功能 **295 项仍待整项交付验收**，其中已有部分实现和增量证据，不等于 295 项均未开发。N4-05 保持进行中。
