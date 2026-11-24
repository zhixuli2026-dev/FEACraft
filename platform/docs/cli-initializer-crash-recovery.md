# 原生初始化中断后的独立额度恢复

2026-11-08，接续 [节点预留](cli-native-node-credit.md)。新增显式接口 `POST /api/v1/codex/sessions/{id}:reconcile-initialization`。历史 GET 不触发恢复，不重启 CLI，不改写原始进程或停止记录。

## 恢复依据与事务边界

固定 `stdio_bridge` 同步执行初始化文件写入；原生 CLI 要等 prepared 的额度事务提交后才能启动。新增 `cli_initialization_recovery` 在完整登记的原生根和桥接身份上核对冻结选择、初始化实现及门代码摘要，再独立探测原 API、宿主根、原生根和桥接进程的出生身份。四者必须都为实际 `exited`；存活、身份变化、探测失败和身份缺失均拒绝恢复。

OS/WSL 探测不持有 SQL 事务。探测后取得用户额度锁和会话行锁，重新验证账户状态、会话所有者、冻结绑定与 version；再提交 `initializer_exited` 独立凭证并结清用户初始化额度。凭证绑定 session、原始进程日志、API 出生身份、选择哈希、实际探测结果及时间。随后既有节点对账核验该凭证与精确节点文件，清理节点预留。提交后中断可以重试；两个 worker 竞争不会覆盖首份凭证。单 API 同时核验限 2 个，并使用 60 秒合作式截止时间；必要 I/O/提交取消仍等待实际收尾，不声称这是全局硬期限。

`initializer_exited` **只证明初始化写者不能继续使用这份预留**，不是整个 CLI 进程范围的停止凭证。会话仍保持原始 starting/active、stop={}、cleanup_verified=null；完整会话收尾、历史删除与执行锁恢复不能据此冒充完成。已有 prepared/verified_stop 的结清机制保持，已生成的文件由实际库存计量，不删除私有文件来消除预留。

## 真实中断与双 worker 验收

显式入口 `live_llm_cli_initialization_recovery_acceptance.py --source-profile-id 26 [--postgres --s3]`。只暂停真实回调或 prepared 提交后的结清边界，随后对真实 API 使用 TerminateProcess，再启动独立 API 并重新登录；没有伪造 CLI RPC、模型输出或停止回执。

| API 中断窗口 | 恢复后结果 |
| --- | --- |
| 原生根已登记、桥接尚未登记 | 已登记进程实际退出，但缺少桥接出生凭证，完整保留用户/节点额度 |
| 桥接已登记、初始化尚未放行 | 四个出生身份均实际退出；新建独立初始化退出凭证，结清额度 |
| 初始化文件已实际写完、prepared 尚未提交 | 保留文件及哈希；新建独立凭证，结清额度 |
| prepared 已提交、节点记录尚未清理 | 保留原 prepared 凭证，恢复节点清理 |
| 实际 Linux CLI 已初始化并处于 active | 保留原 prepared 凭证及空停止日志；不把额度恢复当作完整 CLI 收尾 |

最终 [SQLite/FS 检查](../../tmp/live-cli-startup-d0l34qz7/cli-initialization-recovery-checks.json)、[PostgreSQL/S3 检查](../../tmp/live-cli-startup-g07cuo16/cli-initialization-recovery-checks.json) 各 18 项，共 **36 项实际基础设施检查**、10 次真实 API 中断。每套另用 4 个独立 API worker 与主 API 同时恢复，结果凭证一致，重复请求保持幂等；每个窗口实际验证另一账户返回 404。活跃 owner 被拒绝。文件哈希、原始进程记录和空停止记录均保留。较早同 worker 检查仅作前序证据，不重复计算最终 36 项。

测试 API、8 个独立恢复 worker 和真实对象存储均已退出；临时 PostgreSQL 已删除，开发数据库未修改。每套 root-only 场景刻意保留 133 字节未知预留；未清空未知记录来制造成功。原生身份退出证据说明上述登记进程退出，不扩大为所有未知子孙已核验。未覆盖宿主/WSL 整机重启。

## 真实模型与后续

两套最终验收各通过真实平台 Agent 调用 read_text_file 读取本套证据摘要，再原样回复，流式内容与持久历史一致。最终 **4 次 api.deepseek.com / deepseek-flash，全部 HTTP 200，17,354 tokens**；前序验收另 2 次、8,560 tokens，本阶段合计 **6 次、25,914 tokens，模拟 LLM 0**。[SQLite 模型回执](../../tmp/live-cli-startup-d0l34qz7/live-result.json)、[PG/S3 模型回执](../../tmp/live-cli-startup-g07cuo16/live-result.json)。这些是平台 Agent 模型回合，**CLI 内部推理仍未验收**。

下一步处理未登记桥接/原生创建交接窗口的可靠证据、初始化/节点记录部分写入、实现或目录升级后的旧记录恢复，以及完整 CLI 进程范围停止与会话恢复。继续 OS 硬额度、turn/fork 收尾、历史维护和 CLI 内部真实模型/审批；本接口不会自动重放初始化或释放缺失证据的额度。

主线 **33/53 完成，剩 20（进行中 3、待办 17）**；资源 **11/17、剩 6**；295 项全功能目标不变，N4-05 仍进行中。
