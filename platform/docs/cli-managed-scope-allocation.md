# CLI 降权交接与会话创建前的持久范围分配

2026-11-08，接续 [原生/API 组合恢复](cli-native-api-crash-recovery.md)。目标仍是完整 CLI 会话和执行锁恢复；本阶段交付两个前置能力，尚未把正式 `Session` 切换到新范围生命周期。N4-05 保持进行中。

## 固定管理入口与普通用户 CLI

`cli_native_handoff.py` 只接受固定 Python 入口、冻结的 helper 摘要、已分配的 cgroup 和一致的 launch nonce。root 管理门经过独立 Windows guardian 和原生预登记，加入指定范围后，先清除 supplementary groups、全部 Linux capabilities，设置 NoNewPrivs，再将真实/有效/保存 UID、GID 全部降为 65534，最后启动 bridge 和安装的 Linux CLI。普通启动路径仍拒绝 root；不是放宽原有 UID 校验。

显式入口：`live_llm_cli_managed_handoff_acceptance.py --source-profile-id 26 [--crash] [--postgres --s3]`。

- [SQLite 实际启动、内核权限和范围停止](../../tmp/live-cli-startup-t9kz6ceq/managed-handoff/result.json)：安装的 CLI initialize 成功；读取真实 `/proc` 验证 UID/GID、5 组 capabilities、NoNewPrivs 与 cgroup 归属；独立进程执行同一生产降权函数后尝试恢复 UID 0，实际返回 EPERM。
- [实际 API 中断与重复恢复](../../tmp/live-cli-startup-jbr_zw7y/managed-api-crash-result.json)：存活 owner 拒绝恢复；实际终止 API 后核验独立 guardian 回执、范围消失及 owner/host/native/bridge 退出；重复恢复不再次删除范围，不生成原会话的正常停止回执。
- [PostgreSQL/真实 S3 兼容与原 Session 路径](../../tmp/live-cli-startup-xglzxx3t/managed-handoff/result.json)：同样完成实际降权启闭，并验证原有应用 Session 仍以非 root 启动和正常关闭。

这三轮共 6 次真实 `api.deepseek.com / deepseek-flash` 调用、25,758 tokens、全部 HTTP 200。验证的是平台 Agent 实际工具读取及流式答案/持久历史一致性；CLI 内部模型推理仍未验收。

## 会话建立前的持久分配

新增 `cli_scope_allocations` 表与 `cli_scope_allocation.py`。独立记录存在于正式 CLI session 之前，解决 scope 已创建、session 尚未提交时无持久归属的问题。

1. 提交账户、拟使用的 session UUID、实际 Windows owner 出生身份、分配 seed 和控制器源码摘要。
2. 原生控制器准备完成后，提交真实 creator、host、预留范围信息；提交成功后才允许创建 cgroup。
3. 实际分配完成后提交完整控制器回执。若此时 API 中断，使用步骤 2 的 creator 预留凭据恢复，不猜测 scope inode。
4. 回收前先提交 `retiring`，阻止迟到的分配登记或接管；再实际 retire/abandon，最后按版本和原字段条件提交回执。
5. 独立恢复要求原 owner 已退出；活进程、身份未知、跨账户、源码变化或已交给 session 的范围均不能由此入口直接回收。完成后的恢复返回原回执。

`adopt` 是后续正式会话在同一 SQL 事务内交接的接口，尚未由生产 `Session.prepare` 调用，未声称已经验收业务接管。当前服务只恢复尚未交给业务的范围；不触碰会话执行锁，不将分配回收冒充整项 CLI 清理。

迁移版本 `e8c3f09624ab`，前版 `d7b2ef8513fa`。[迁移验证](../../tmp/cli-scope-allocation-migration.json)包含全新隔离 SQLite 升级、空表降级及再次升级，以及复制真实测试记录后拒绝有数据降级、6 条原记录和版本保持不变。PostgreSQL 测试只迁移唯一临时数据库，测试后已删除；开发库未迁移或写入。

## 实际故障矩阵与模型证据

显式入口：`live_llm_cli_scope_allocation_acceptance.py --source-profile-id 26 [--postgres]`。辅助脚本只暂停实际 I/O 之间的执行并终止真实 API，不生成模型回复或替代原生控制器结果。

| 中断位置 | 恢复依据与结果 |
|---|---|
| 意图已提交，尚未开始准备 | 记录 unused；不捏造曾存在的范围 |
| 准备已提交，尚未发送创建许可 | 实际 creator 退出后 abandon |
| 实际范围已分配，分配回执尚未提交 | 从已提交的准备凭据定位并 abandon |
| 完整分配回执已提交 | 实际 retire 并核验 absent |
| retiring 已提交，尚未执行回收 | 从持久标记继续实际 retire |

最终 [SQLite 矩阵](../../tmp/live-cli-startup-sih2ozp3/scope-allocation-checks.json)和 [PostgreSQL 矩阵](../../tmp/live-cli-startup-9qslu6_i/scope-allocation-checks.json)各覆盖 5 个边界，共 **10 次实际 API 终止、10 个额外独立 API worker**。每个边界验证活 owner 拒绝、两个独立进程并发恢复得到同一持久回执、重复恢复及跨账户 404；另有正常实际分配/回收、重复分配拒绝、错误 owner token 拒绝。

最终模型回执：[SQLite](../../tmp/live-cli-startup-sih2ozp3/live-result.json)、[PostgreSQL](../../tmp/live-cli-startup-9qslu6_i/live-result.json)，共 **4 次、17,116 tokens**，全部 HTTP 200。前序 [SQLite 同一 API 内并发请求](../../tmp/live-cli-startup-fqu1wz3a/live-result.json)另有 2 次、8,720 tokens；本轮分配验收累计 **6 次、25,836 tokens，Mock 0 次**。连同前述降权交接为 12 次、51,594 tokens，不能把前序调用重复算为本轮新增。

[首次失败](../../tmp/live-cli-startup-3oqocew7/live-result.json)发生于跨账户检查的测试邮箱使用保留域名，被注册接口 422 拒绝；当次模型调用 0。修正为 example.com 后从新隔离环境重新验证，原失败记录保留。测试 API、额外 worker 已退出；临时 PG 已删除，前序实际 S3 服务已停止。

## 继续主线的具体任务

| 工作 | 当前状态 / 完成条件 |
|---|---|
| 固定 root 管理门到非 root CLI | 底层实际启闭及中断验证完成；正式会话尚未采用 |
| session 前的持久分配与恢复 | 以上 SQLite/PG 矩阵完成；生产请求接线待下一步 |
| 正式会话接管 | `Session.prepare` 在冻结 nonce/profile 前分配，session 创建与 adopt 同事务；guard/preflight 在许可前持久提交；兼容无 case 的账户检查 |
| 会话完整停止与恢复 | 同时验证 Windows guardian、原生 cgroup、进程出生身份；恢复凭证与原 session/version 绑定，保留原正常 stop 证据，不升级旧协议记录 |
| 执行锁与会话状态收尾 | 实际 API 退出、并发恢复、取消、迟到提交时，仅在完整范围关闭和版本条件满足后释放所属执行锁；覆盖 Session.access/turn/fork |
| CLI 真实推理与审批 | 使用授权真实模型，验证 CLI 内部 turn、工具选择、审批和 fork；平台 Agent 读回不代替这些验收 |
| 资源与历史维护 | 旧根/旧实现/残片维护，guardian 时限与会话/turn 预算统一，OS 硬额度、全局并发与仍未解决的慢扫描/间歇拒绝 |

主线 **33/53 完成，剩 20（3 进行中、17 待办）**；资源 **11/17，剩 6**；全功能 **295 项仍待整项交付验收**。这些项目已有部分实现与增量证据，不能据本阶段前置能力将整项标为完成。
