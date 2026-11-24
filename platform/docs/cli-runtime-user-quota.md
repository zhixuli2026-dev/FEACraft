# CLI 运行期用户用量与启动放行

## 2026-11-20 独立 Codex 会话并发名额

`VIBEFLOW_CODEX_CONCURRENT_SESSIONS` 默认每账户 4 个会话，Windows 和 WSL 共用。同一账户的私有目录准备前先检查一次，登记会话时在数据库账户锁内再次检查并提交。直接创建达到上限返回 `429 cli_session_capacity`。会话启动请求保留 `CliSessionCapacity` 失败记录及已核验的未启动状态，同一请求 ID 可查询同一回执。

名额包含待启动、活动和停止证据缺失的历史会话。Windows 用已验证停止回执结清，托管 WSL 用已验证范围退役记录结清。活跃记录的租约到期不会释放名额，已退出进程也要等终态事务提交才释放。

实际文件系统上的 SQLite 和 PostgreSQL 各通过 6 项。两个 API 对不同工况同时完成前置准入后竞争唯一名额，Windows 得到 200，WSL 得到 429。另一个账户同时正常开启 CLI。会话启动请求记录未启动、重复请求 ID 一致。等待已核验停止回执提交时拒绝下一条申请，恢复一条测试中受损的停止回执后，实际 WSL 会话能使用空出的名额。两轮各核验 13 个真实进程身份退出，4 个隔离 API 监听关闭，临时 PostgreSQL 库已删除。SQLite 另核对了竞争失败的 WSL 预备范围已退役。[SQLite](../../tmp/cli-session-capacity-f7ranal7/capacity.json) · [PostgreSQL](../../tmp/cli-session-capacity-97z24k9q/capacity.json) · [预备范围退役](../../tmp/cli-session-capacity-f7ranal7/native-allocation-release.json)

启用单名额后，完整复跑原生会话期限及输出套件，6 项通过。真实 WSL DeepSeek `deepseek-flash` 首轮合计收到 65,671 字节触限，下一轮同一对话的真实答案成功落库。冷启动重读 6 条停止回执，核验 37 个原进程身份退出。本阶段实际观察到 2 个模型回合及 0 次模拟，未独立计数上游 HTTP。初次并发测试把启动请求的公开字段读成了 `session_id`，修正夹具改读 `session` 后通过，两次发布的并发结果来自最终版。[单名额真实模型复验](../../tmp/live-cli-session-budget-gcs0v48k/live-result.json) · [原始夹具失败](../../tmp/cli-session-capacity-8t00zsn2/capacity.json)

## 2026-11-19 原生 Codex 会话期限与累计输出

`VIBEFLOW_CODEX_SESSION_LIFETIME_SECONDS` 从桥接确认原生 Codex 进程启动起计时，贯穿初始化、空闲和模型回合。`VIBEFLOW_CODEX_SESSION_MAX_OUTPUT_BYTES` 计入同一路径收到的标准输出和标准错误原始字节，包括初始化和模型通知。两个值在会话创建时确定，默认均为 `0`，分别关闭新增限制。

达到期限以 `CliSessionTimeout` 收尾。累计输出超过上限以 `CliSessionOutputLimit` 收尾，跨限额的帧停止交付。停止回执沿用已登记的进程范围，原因分别为 `timeout` 和 `log_limit`。启动时触限返回 HTTP 409，运行时触限写入会话及活动回合的失败记录。验证范围停止后释放执行归属。失败回合保留已写事件供查看，正式对话历史仅提交成功送达的助手消息。S3 暂存失败沿用已登记副本的保留与恢复流程。

[实际验收](../../tmp/live-cli-session-budget-3rk1mici/live-result.json)使用 SQLite、文件系统、已安装的 Windows Codex 和非特权 WSL Codex。Windows 初始化实际读到 371 字节 stdout，WSL 启动实际读到 332 字节 stderr，两种环境均以 64 字节上限返回 409。两种环境还分别以 12 秒上限停止空闲会话。启动后将配置改回默认值，原会话仍按创建时的期限停止。

非特权 WSL 共观察到两轮真实 `api.deepseek.com / deepseek-flash` 模型流，模拟调用为 0。第一轮收到 230 个模型推理增量通知，累计读到 65,283 字节 stdout 和 332 字节 stderr。合计 65,615 字节超过 65,536 字节上限，会话及回合失败，未写入正式助手消息。随后同一对话创建新会话，模型按要求回复 48 字节验证串，流和历史内容一致。该入口统计实际 CLI 模型流回合，未逐笔观察上游 HTTP。冷启动确认 6 条停止回执原样保存，独立核验 21 个 Windows 和 16 个 Linux 原进程身份均已退出。[最终源码审查](../../tmp/cli-session-budget-live-review-20261119-v3.json) · [隔离监听退出](../../tmp/live-cli-session-budget-3rk1mici/listener-exit-proof.json)

前两轮夹具要求触限前必须已有可见回答。实际模型在推理阶段输出触限，四项初始化及空闲检查已通过，整轮验收按失败保留。最终夹具只按实际 CLI 通知计数，保留正文空白和回合失败结果。[20 KiB 轮次](../../tmp/live-cli-session-budget-_9w060_e/live-result.json) · [64 KiB 第一轮](../../tmp/live-cli-session-budget-6ogonte4/live-result.json)

账户并行会话的准入与停止后释放见本页 2026-11-20 的独立 Codex 会话并发名额。

## 2026-11-07 用户实际用量观察

2026-11-07。接续 [WSL 私有目录库存](cli-native-private-inventory.md)。此前运行期仅检查节点用量，CLI 自行产生的文件没有持续与用户套餐比较。本次新增实际用户用量观察，接入 Windows/WSL CLI 启动及运行期。

`cli_runtime_storage.observe` 先完整扫描该用户实际公共、宿主私有和已配置的原生私有用量，再以短数据库事务读取活动用户及最新套餐。扫描期间不持有 SQL 写锁。无法完成计量、读取配额或配额类型异常时返回 `cli_user_storage_unavailable`；实际字节超过套餐时为 `cli_user_storage_full`；用户不可用时为 `cli_user_unavailable`。运行期观察只比较物理库存，写前额度预留仍由已有持锁准入负责，不能把尚未落盘预留视作进程已经写出的字节。

检查在准备完成后、真实 CLI 初始化返回后和每次运行期节点检查后执行；WSL 还在原生私有初始化完成、实际 CLI 启动许可发出之前检查。启动资源错误保留具体 code。观察采用新的本地截止时间，文件扫描取消继续等待实际工作线程排空；原有心跳独立运行。超过额度或不能核验时走实际关闭/持久停止记录流程，不删除保留文件。

## 实际验收

显式入口 `platform/server/live_llm_cli_runtime_user_acceptance.py --source-profile-id 26 [--postgres --s3]`，导入的公共验收辅助代码与上一阶段审查哈希相同，没有调用历史模拟套件。FS/SQLite、真实 SeaweedFS/PostgreSQL 各 **8 项实际基础设施检查**，共 **16 项**：

1. Windows 与 WSL 实际 CLI 正常初始化，通过用户库存检查。
2. 各运行时实际新增 3 MiB 文件，使当前用户超过套餐，观察任务终止 CLI；第二个用户用量不变，原文件哈希不变。
3. 各运行时新增实际未知归属的旧私有目录，节点能计量而用户归属无法证明；以 `cli_user_storage_unavailable` 关闭，保留原数据。
4. 真实 WSL 初始化完成后、放行前实际写入超额数据；阻止 CLI 子程序启动，已登记桥接进程核验退出，保存 `cli_user_storage_full`。
5. 将隔离数据库的套餐实际改为非法字符串额度，观察拒绝，随后恢复原套餐。

证据：[SQLite](../../tmp/live-cli-startup-b1qhprk6/cli-runtime-user-checks.json)、[S3/PostgreSQL](../../tmp/live-cli-startup-nsxx_udh/cli-runtime-user-checks.json)。合计 10 个实际会话根退出，WSL native state 均为 exited；两套 API 已退出，节点预留无残留，临时 PostgreSQL 已删除、对象存储已停止。只在本轮隔离目录、用户和套餐操作；未知目录在核验关闭后仅将本测试创建的目录移到本测试用户目录，生产未知历史未处理。SQLite 中观察到 Windows/WSL 超额至完成关闭约 5.7/6.8 秒，这是本次测量值，不是所有部署的严格停止时限。

## 真实模型与范围

首轮 S3/PG `y8skq8ym` 在四个实际 CLI 已关闭后，移动本测试创建的目录时遭遇 Windows sharing violation（WinError 32），在模型阶段前失败，真实调用 0。原记录保留；四个根核验退出，临时库删除，存储停止。测试收尾新增只针对 WinError 32/33 的最多 5 秒重试，路径和文件核验保持；S3/PG 随后重跑全部检查。SQLite 使用重试增加前的等价检查，生产代码与两套验收一致，执行 fixture 哈希分别保存。不重复计入最终 16 项通过检查。

两套检查之后，平台 Agent 实际使用 `read_text_file` 读取该套证据摘要的哈希；流式和持久化回答严格一致。[SQLite 回执](../../tmp/live-cli-startup-b1qhprk6/live-result.json)、[S3/PG 回执](../../tmp/live-cli-startup-nsxx_udh/live-result.json)：**4 次真实 `api.deepseek.com / deepseek-flash`，全部 HTTP 200，17,186 tokens，模拟 LLM 0**。纯协议模拟测试 0；文件与套餐故障为实际基础设施操作。CLI 内部模型推理仍未验收。

本实现是启动门检查和周期观察，不能阻止两次观察间的超额写入，也没有为较早的原生初始化写入提供持久峰值预留。完整用户硬配额、不同卷/WSL 的 OS 强制边界、旧配置根和未知历史对账、协作用户公共写入归属、跨 worker 竞争/猝死、慢扫描及长时间运行矩阵仍待完成。不能据此将 RB-13/14/17 或 N4-05 整项销项。

主线 **33/53 完成，剩 20（进行中 3、待办 17）**；资源 **11/17 完成、剩 6**；全部 295 项全功能交付目标继续。下一步补原生初始化的持久峰值准入及生命周期收尾，然后继续硬资源边界、历史对账和 CLI 内部真实模型回合。
