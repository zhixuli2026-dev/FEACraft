# CLI 退出后的 S3 暂存恢复

## 2026-11-21 PostgreSQL 存储准备与覆盖写入

PostgreSQL 独立复跑在准备测试文件时返回 `504 / file_write_timeout`。跟踪记录显示这次保存和结算发出 19 次受容量控制的物理 PUT 准入，首尾约 33 秒，模型回合为 0。上一轮在同一路由返回用量盘点 503，两轮失败各自保留。[写入超时](../../tmp/live-cli-staging-recovery-wsegpoqy/live-result.json) · [物理 PUT 跟踪](../../tmp/live-cli-staging-recovery-wsegpoqy/actual-capacity-calls.jsonl) · [盘点 503](../../tmp/live-cli-staging-recovery-sqyp3k_9/live-result.json)

账户的 WSL 私有目录若确实不存在，通过已绑定根目录和父目录的无跟随属性读取确认缺失。每次盘点重新确认。已有账户目录和节点盘点继续使用原生 Linux 读取器核验身份、条数、容量与退出凭据。此改动后的 PostgreSQL 跑过准备、真实 DeepSeek 审批和第一次 API 中断恢复，在有私有数据的账户做下一次 S3 文件覆盖时仍返回 504。[首回合和恢复实测](../../tmp/live-cli-staging-recovery-wd7xh8i8/live-result.json) · [覆盖写入跟踪](../../tmp/live-cli-staging-recovery-wd7xh8i8/actual-capacity-calls.jsonl)

S3 物理条件写入探测通过后，同一存储操作对同一客户端、地址、bucket 和前缀复用探测结果。新的操作或 API 重启重新探测，实际写入仍逐笔核验容量和条件。后一轮通过准备文件，CLI 启动返回 502 后结束，模型回合为 0。各轮对象服务均已停止，临时 PostgreSQL 库均已移除。[启动失败](../../tmp/live-cli-staging-recovery-70f91v93/live-result.json) · [对象服务](../../tmp/s3-attempt-store-zl6w8rn_/cli-recovery-result.json) · [临时数据库](../../tmp/cli-staging-recovery-postgres-03araaou/result.json)

在同一正式代码上独立复跑，PostgreSQL、认证 SeaweedFS 和非特权 WSL 的完整流程通过 8 项。此前失败的准备文件和账户有私有数据时的覆盖均通过。第一次真实 `deepseek-flash` 回合停在审批，实际 API 退出后复验远端冲突、部分删除和完整删除后再次退出的恢复。第二回合实际读取恢复文件并原样写入会话历史，状态为 `completed`，进程清理已核验。CLI 上游 HTTP 次数未采集，模拟模型调用为 0。隔离双 API 和对象服务已关闭，临时数据库已删除。[完整回归](../../tmp/live-cli-staging-recovery-cxlpczhk/live-result.json) · [物理 PUT 跟踪](../../tmp/live-cli-staging-recovery-cxlpczhk/actual-capacity-calls.jsonl) · [对象服务](../../tmp/s3-attempt-store-z4iwain5/cli-recovery-result.json) · [临时数据库](../../tmp/cli-staging-recovery-postgres-ah96bvm5/result.json)

另有 9 项定向盘点检查通过。缺失账户保留已有计数，创建账户并写入 31 字节后立即按实际数据计量，重定向的账户路径被拒绝。原有条目与容量上限、普通文件和链接计数、FIFO、助手期限及退出和源码校验继续通过。平台 Agent 对该验收证据的真实工具回读发出 2 次 `deepseek-flash` HTTP 请求，均返回 200，模拟模型调用为 0。[盘点专项](../../tmp/live-cli-startup-aze_qrnc/native-inventory-faults.json) · [平台 Agent 实际回读](../../tmp/live-cli-startup-aze_qrnc/live-result.json)

相同版本在 SQLite、认证 SeaweedFS 和非特权 WSL 同样通过全部 8 项。两次真实 CLI 回合均满足原场景，恢复后文件读取、会话原样保存与停止证明通过，隔离 API、对照 API 和对象服务已退出。模拟模型调用为 0。[SQLite 完整回归](../../tmp/live-cli-staging-recovery-hfe1zyr7/live-result.json) · [对象服务收尾](../../tmp/s3-attempt-store-l4cnvdle/cli-recovery-result.json)

## 2026-11-20 启动期限与运行期库存检查

最新 PostgreSQL 与认证 SeaweedFS 回归中，实际通过首次 CLI 审批、API 中断、暂存冲突拒绝、部分清理及两次重启。后续真实 CLI 回合被后台存储检查中止，关闭会话返回 502；该轮保留失败。更早的隔离环境未继承已安装 Linux Codex 的主机路径，启动前返回 503，实际模型回合为 0。[启动前 503](../../tmp/live-cli-staging-recovery-fxt5ulii/live-result.json) · [恢复后收尾失败](../../tmp/live-cli-staging-recovery-saeeixmt/live-result.json)

后台任务在启动时创建，会继承当时的截止时间。定向验证让实际账户盘点继承已过期的上下文，复现 `cli_user_storage_unavailable`，底层为 `TimeoutError`。运行期盘点现在在自己的期限内执行。以同一过期上下文进入后台任务，实际盘点读到 9,220 字节、38 条记录，未触发会话关闭。启动流程仍受原启动期限约束。[定向验收](../../tmp/rb14-cli-observer-scope-g5ucmbjf/cli-observer-scope.json)

修复后的 PostgreSQL、认证 SeaweedFS 和非特权 WSL 完整回归通过 8 项，包括真实审批中断、各次重启、同键恢复、局部文件清理、保留旧版本、实际读取恢复文件和原样持久化回答。两个真实 `deepseek-flash` CLI 回合，模拟 0 次。CLI 上游 HTTP 次数未采集。最终回合 `completed`，停止已核验，隔离 API、同库对照 API 和对象存储已停止，临时 PostgreSQL 库已删除。[完整复验](../../tmp/live-cli-staging-recovery-abyg19mi/live-result.json) · [对象服务](../../tmp/s3-attempt-store-zpkfsrlb/cli-recovery-result.json) · [临时数据库](../../tmp/cli-staging-recovery-postgres-gm18e9qu/result.json)

2026-11-15，N4-05、FF-F41 的功能增量。原有范围恢复能证明 CLI 已停止，并使未完成回合进入 interrupted、令旧审批过期，但 S3 暂存存在时仍保留会话执行锁。此前没有正式操作把这些文件同步回原算例并完成释放。

新增 `POST /api/v1/codex/turns/{turn_id}:reconcile-staging`。该显式操作先使用既有回合恢复核验所属进程退出、完整范围关闭和回合归属，再使用既有 S3 文件事务同步：当前用户、算例、权限模式、模型配置及会话绑定必须保持有效；远端变化冲突时返回 409，保留双方文件及执行锁。

新的暂存清单记录原 endpoint、bucket、prefix。正常同步会检查记录的存储地址；恢复入口还拒绝缺少该记录的旧副本，保留文件供核对，不能根据当前部署配置猜测旧来源。未改变旧清单的版本号或改写旧现场。

同步与清理沿用同一个现有存储操作租约，阻止两个 worker 同时同步或在部分删除期间重新扫描该目录。真实远端事务提交并完成对账后，才调用现有暂存清理；其 `source_synchronized=true` 清理标记允许中断后继续删除本地副本。带有该标记时不能把已删除的本地文件再次解释为远端删除。清理完成后再次核验执行锁；只有不存在其他未收尾执行时才释放。

同时修复保留暂存时的恢复幂等问题：已有有效恢复凭据且仍需保留执行锁时，重复核验直接返回既有结果，不继续增加执行版本，避免正常重试与并发读取互相制造 CAS 冲突。若后来已清理暂存，仍走原来的条件释放与释放凭据流程。

对于已经有有效停止凭据的回合，暂存恢复先复用该凭据，再由现有存储租约处理上次中断留下的文件事务，最后发布执行锁释放；不能把存储收尾前置成“先释放会话”的条件。实际删除完目录、清单和清理标记后，若 API 在释放执行锁前退出，重试会对账原存储操作并释放，不再要求已经删除的清单重新出现。

## 验收方式

显式真实模型入口：

```powershell
platform/server/.venv/Scripts/python.exe platform/server/live_llm_cli_staging_recovery_acceptance.py --source-profile-id 26
platform/server/.venv/Scripts/python.exe platform/server/live_llm_cli_staging_recovery_acceptance.py --source-profile-id 26 --postgres
```

使用已有 Linux CLI、授权 DeepSeek / `deepseek-flash`、独立 SeaweedFS、独立数据库及隔离 API。Linux 程序通过部署环境 `VIBEFLOW_CODEX_WSL_PATH` 显式指定。本地模型密钥只读自授权配置，不打印、不写入证据或源码快照。

场景按实际行为分列：

- **真实模型**：CLI 实际选择读取命令并等待审批；恢复完成后，新 CLI 回合实际读取恢复文件，回答未出现在提示词中的随机值，验证流式答案与落库答案一致。
- **实际基础设施**：三次真实 API 退出及重新启动、原生范围关闭、S3 条件同步、双 API 重复恢复、文件创建/修改/删除、持久执行锁释放及对象存储退出。
- **故障输入**：测试直接修改隔离暂存目录，代表未同步编辑，不宣称这些编辑由模型产生；实际删除部分本地文件后注入 OSError，使生产清理留下自己的真实清理标记。另改变测试配置 bucket 验证拒绝错误目标。这些故障不替换模型输出或 CLI 工具选择。

[初版 SQLite 完整流程](../../tmp/live-cli-staging-recovery-qiy_goro/live-result.json)已通过 5 个检查、2 个真实 CLI 回合；该版本尚不含后续新增的存储地址检查，不能替代最终版本验收。对象存储停止记录见[初版收尾](../../tmp/s3-attempt-store-8x_10tzu/cli-recovery-result.json)。

保留的返工记录：`9y5q6cun`（SQLite）和 `ts5y8vof`（PostgreSQL）在准备阶段收到存储额度结算忙 409，均未启动模型回合；测试后来仅针对该明确拒绝增加 30 秒内的限时重试，每次拒绝均记入 `quota_admission_retries`，其他错误和模型结果不自动重试。`ymwv6fty`（PostgreSQL）通过实际退出及双 API 重复恢复，但存储地址变化检查得到 `file_recovery_required`，原因是校验发生在 case 事务对账之后；随后把原存储地址检查前移到范围恢复发布之前。上述原始 `tmp/live-cli-staging-recovery-*/live-result.json` 保持失败，不改写为通过。

`m38df1ks` 的存储地址拒绝已通过，但下一项期望文件冲突时先收到额度结算忙；限时重试后来也应用于“期望其他 409”的检查，仍要求最终错误码精确匹配。`aakvpvlc` 则实际遇到未对账存储操作先阻止执行锁发布的问题，促成上述“复用停止凭据 → 对账存储 → 释放执行锁”的调整。这些 PostgreSQL 失败运行均已按夹具删除临时库，原失败报告保留，不推定未执行的后续检查通过。

## 交付边界

`8686l0up` 的最终源码 SQLite 运行通过前六项实际恢复检查，但最后的真实 CLI 回合报告 `stream disconnected before completion: error sending request`，且 `willRetry=false`，该轮保持失败。模型先报告了实际 token 用量，随后流中断；这不等同于成功读回。收尾 DELETE 的 502 覆盖了顶层异常文字，原模型失败仍完整保存在 `final_turn` 和 `actual-cli-events.jsonl`，不能根据六项基础设施检查把整轮判成通过。后续使用同一源码重跑 SQLite，不替换模型输出。

最终源码的 PostgreSQL 验收已通过：[7 项实际检查、2 个真实 CLI 回合](../../tmp/live-cli-staging-recovery-m55rm4wt/live-result.json)，[对象存储已停止](../../tmp/s3-attempt-store-fj3dx6r6/cli-recovery-result.json)，[临时 PostgreSQL 已删除](../../tmp/cli-staging-recovery-postgres-451byfmu/result.json)。同源码的 [SQLite 最终验收](../../tmp/live-cli-staging-recovery-h0zsojt8/live-result.json)也通过 7 项检查及 2 个真实 CLI 回合。对应源码登记为 `tmp/N4-05-cli-staging-recovery-review-v11-20261115.json`；两个最终通过运行合计 4 个真实 CLI 回合，没有模拟 LLM，CLI 内部 HTTP 请求总数未统计。验收始于 11 月 15 日，SQLite 重跑于北京时间 11 月 16 日完成；早期失败记录保持原样。

`mmiu0uwm` 证实仅筛掉无文件消费者的通知仍不足：旧文件事件也会先申请额度，再被进行中的存储操作拒绝。随后把现有存储操作排他检查前移至后台索引额度申请之前；该检查只能提前拒绝并让 outbox 重试，真正读取仍经过原来的 case 锁检查。未跳过索引消费、广播确认或原有事务验收。

额度争用的追加诊断：`ec448_7w`、`pk23vfe_`、`ord9gh1d` 和 `ftqt4m73` 保留失败结果。最后一轮的实际调用栈确认，后台 `durable_events.publish_to_local_bus` 对没有文件消费者的通知也进入 `case_read_capacity.reading`，反复申请账户存储额度；恢复本身又产生存储操作通知，导致持续争用。修复把已有消费者的事件筛选提取为同一个 `requires_files` 判断：只有确实要更新文件索引的事件才申请原有读取额度及事务锁，其余已提交通知直接广播。未放宽额度上限、文件事务检查或模型验收要求。`ftqt4m73` 的诊断版源码哈希因编码参数错误在启动后才写入，明确只作诊断证据，不作为最终源码验收。

最终 SQLite [对象存储收尾记录](../../tmp/s3-attempt-store-p2x7o0i1/cli-recovery-result.json)已确认停止；[独立进程核验](../../tmp/N4-05-cli-S3-store-exits-20261115.json)逐一检查本轮 14 个测试存储的 PID、启动时间及用户身份，原进程全部不存在。最终运行及历史结果清单见[验收输入](../../tmp/N4-05-cli-S3-final-evidence-20261115.json)。

本次新增正式后端恢复操作。聊天界面的恢复入口仍需接入；复杂审批/秘密输入、原生 fork/startup 提交中断、未登记历史和其他原资源预算条件仍按原计划推进。没有把此后端流程当作 FF-F41 或整个 N4-05 完成。主线 **48/53、剩 5 项**；资源 **11/17、剩 6 块**；295 项全功能目标保持。

## 2026-11-19 暂存清单逐项核验

恢复使用已有本地清单和同一份存储租约。扫描逐项核对工作目录，已匹配的路径从本次内存中的初始清单消去，实际有变化的路径才进入提交计划。清单上有而工作目录里缺少的成员，仍可形成显式文件删除。若整个工作目录缺失、扫描中被替换，或已观察到的成员在读取前消失，当前同步拒绝提交并保留清单。正文计算由原请求期限约束，文件枚举继续使用既有条目数和扫描期限。[扫描实现](../server/app/services/staging_sync.py) · [文件树枚举](../server/app/storage.py)

用 4,000 个实际本地文件和同一布局的 508,066 字节清单做对照。文件内容均匹配准备好的测试清单，两次扫描均未产生待同步文件。Python `tracemalloc` 峰值由 **6,272,178 B** 降至 **4,426,769 B**，减少 29.42%。两次耗时分别为 11.34 秒和 11.97 秒。这组验证实际本地扫描，不访问对象存储，无模型调用。[旧版](../../tmp/staging-inventory-y8ra5ktv/result.json) · [固定版](../../tmp/staging-inventory-fqcrqieu/result.json) · [验收入口](../server/staging_inventory_acceptance.py)

本次生产入口回归使用 SQLite、认证 SeaweedFS 和非特权 WSL。实际停掉原 API 后，将本次隔离工作目录暂时移开，保留原清单。正式 `:reconcile-staging` 返回 409，远端原件及移动后的本地编辑均保持。将工作目录归位后二者冲突仍正确返回 409；恢复远端测试基线后，真实创建、修改和删除均成功，部分清理故障及再次退出后的重启只补齐清理与释放。后续新回合实际读取恢复后的文件，原内容进入流式回答及持久历史。共通过 8 项，含两次真实 `deepseek-flash` CLI 模型回合，模拟 0，CLI 内部 HTTP 次数未采集。[实际回归](../../tmp/live-cli-staging-recovery-wf45iw21/live-result.json) · [运行前精确源码核对](../../tmp/staging-inventory-live-review-20261119.json)

首次向 Windows API 配置了 Linux 挂载路径，503 发生在模型回合启动前。改用现有配置所用的 Windows 宿主路径后复验通过。实际两个 CLI 会话的 18 个已登记进程身份全部退出，两个对象存储及 16 个监听关闭，临时认证配置回收，隔离 API 和同库对照进程已退出。[首次 503](../../tmp/live-cli-staging-recovery-_25hhhkc/live-result.json) · [原生进程](../../tmp/live-cli-staging-recovery-wf45iw21/process-exit-proof.json) · [API 与存储退出](../../tmp/live-cli-staging-recovery-wf45iw21/listener-exit-proof.json)
