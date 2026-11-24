# 未获初始化许可的启动空窗恢复

2026-11-08，接续 [已登记写者的独立恢复](cli-initializer-crash-recovery.md)。上一阶段对缺失桥接身份的所有情况保留额度。本阶段为**新建 v3 预留**记录明确的放行顺序，覆盖创建交接空窗；旧 v1/v2 未知预留仍保持原有保守行为。

## 新版协议及放行

`cli_initialization_capacity` 的 v3 绑定新增 `launch_order=registered_bridge_before_initializer_release_v1`。实际 `Session.register_bridge` 在同一事务中写入桥接身份并核验完整活动额度；事务提交成功、回调返回后，`OwnedStdio` 才能发送原生初始化许可。额度已结清、绑定变化或缺少预留时拒绝放行。v1/v2 仍可验证原始绑定，但不能由新版服务凭空升级为 v3 保证，无数据库迁移或历史行改写。

独立恢复继续要求原 API 出生身份实际退出。对于 v3 且没有已提交桥接登记的会话，新建 `initializer_not_released` 凭证：绑定原进程日志、API 出生身份、冻结选择和初始化预留完整哈希，说明初始化从未获准，而非假称未登记进程已退出。已登记的宿主/原生根仍须实际退出；根尚未登记时不虚构其 PID。持久事务提交后才发送许可，加上恢复的用户额度锁、会话行锁、version/绑定复核，使未完成的登记提交必须先完成或回滚；已提交桥接的情况仍要求真实桥接退出。

`initializer_exited`、`initializer_not_released` 和原 prepared 凭证保持区分。结清只覆盖初始化额度，原始会话 phase、process、stop 和私有文件保持；仍不能据此声称完整 CLI 范围已经停止，或释放会话执行锁。旧 worker 不理解新绑定时应拒绝相关记录，部署应协调相关 worker 的版本。

## 实际中断矩阵

入口仍为 `live_llm_cli_initialization_recovery_acceptance.py --source-profile-id 26 [--postgres --s3]`，新增 `--stages` 可明确运行指定实际窗口。SQLite 首轮验证 9 个窗口，随后补独立的 `--stages bridge_commit`；PostgreSQL/S3 一次运行全部 10 个。三次运行使用相同生产实现，测试源码新增了未提交事务窗口；均保留当时实际执行的 `live_fixture.py`。没有重跑旧模拟集合。

| 实际 API 中断位置 | 独立恢复凭证 |
| --- | --- |
| 仅完成持久准备，尚未创建进程 | initializer_not_released |
| 宿主 CreateProcess 已返回，尚未握手 | initializer_not_released |
| 原生根实际就绪，尚未登记 | initializer_not_released |
| 原生根已登记，桥接未登记 | initializer_not_released |
| 桥接实际存在，尚未登记 | initializer_not_released |
| 桥接登记及额度检查已执行，SQL 事务尚未提交 | 实际 API 退出使事务回滚；initializer_not_released |
| 桥接登记已提交，尚未发送初始化许可 | initializer_exited |
| 原生初始化已写完，prepared 尚未提交 | initializer_exited，文件保留 |
| prepared 已提交，节点清理未完成 | 保留 prepared，恢复节点结清 |
| 实际 Linux CLI 已 active | 保留 prepared；完整 CLI 清理仍单独处理 |

[SQLite 9 窗口](../../tmp/live-cli-startup-xtuql0pv/cli-initialization-recovery-checks.json)、[SQLite 未提交事务窗口](../../tmp/live-cli-startup-j855lv6x/cli-initialization-recovery-checks.json)、[PostgreSQL/S3 10 窗口](../../tmp/live-cli-startup-fn9z_7ok/cli-initialization-recovery-checks.json) 共 **76 项实际基础设施检查、20 次真实 API 中断、20 个独立恢复 worker**。每个窗口都在重启后由两个 API 进程同时恢复，返回相同持久凭证；未结清场景实际拒绝存活 owner，所有窗口实际拒绝另一账户，重复请求无重复释放。已创建但尚未登记的真实进程另由测试观察出生身份并确认退出；这份测试观察不被伪装成产品原始登记证据。

三次环境的 v3 初始化用户及节点额度均结清；原始 stop={} 与 cleanup_verified=null 保留，未获准的初始化没有创建 home，已写文件哈希不变。所有测试 API、独立 worker 与对象存储已退出，临时 PostgreSQL 已删除。开发数据库未写入。

另以 SQLite `mode=ro` 检查 **15 条实际 v1/v2 历史记录**，数据库文件哈希不变：旧绑定可读，4 份已有 initializer_exited 凭证继续有效，2 份缺失桥接的历史未知额度仍拒绝获得新版许可保证。见 [只读历史证据](../../tmp/cli-initialization-legacy-v3-20261108.json)。未改写旧记录，也未重放旧测试。

## 真实模型与剩余范围

三个环境都由真实平台 Agent 调用 read_text_file 读取本次检查摘要，验证流式答案和持久历史一致。共 **6 次 api.deepseek.com / deepseek-flash，全部 HTTP 200，25,788 tokens，MOCK 0 次**。模型回执：[SQLite](../../tmp/live-cli-startup-xtuql0pv/live-result.json)、[事务窗口](../../tmp/live-cli-startup-j855lv6x/live-result.json)、[PG/S3](../../tmp/live-cli-startup-fn9z_7ok/live-result.json)。CLI 内部推理仍未验收。

接下来处理原生初始化文件和节点记录的部分写入/空间故障，完整 CLI 进程范围停止、会话恢复及执行锁结清；继续旧根/实现升级后的历史维护、OS 硬额度、turn/fork 与真实 CLI 模型审批。新版许可恢复解决的是初始化预留，不能替代这些工作。宿主/WSL 整机重启与断电持久性未覆盖。

主线 **33/53 完成，剩 20（3 进行中、17 待办）**；资源 **11/17、剩 6**；全功能 **295 项**目标保持，N4-05 仍进行中。
