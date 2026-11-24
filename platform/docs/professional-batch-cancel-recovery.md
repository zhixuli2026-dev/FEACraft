# 专业批次运行中取消与恢复

验收入口 `live_llm_professional_batch_cancel_acceptance.py` 先建立实际 MPI/UDF 成功基线，再创建三个正式工作流成员。MPI 与 UDF 使用 10000 单元网格和长物理求解，实际观察两个非 root MPI ranks 及已加载本次库的 UDF icoFoam；第三个串行成员在批次/用户并发上限 2 下等待。现有准入可能让步骤保持 durable ready 而尚未建立 Job，也可能已经建立 queued attempt；验收记录实际所在层，并验证取消后没有启动。

另一 API 与观察 API 并发请求整批取消，校验单次取消版本、跨用户拒绝、真实 worker 归属、观察到的原生进程出生身份退出、持久停止证明、排队成员未启动，以及旧求解头与场数据保留。原 API 退出后，独立恢复 API 读取原历史及同幂等键，随后按原接口明确重置阶段、恢复 400 单元输入并提交新批次。新 MPI、UDF、串行结果必须完成实际场对比、D/E 确认和后处理提取。

FS/PG 与 S3/PG 完整命名场景均已通过；准备运行的同算例批次取消，以及 worker 运行中猝死继续验收，当前不宣布 N3-07 整项通过。模型只在工程场景成功后使用已授权的真实 DeepSeek 独立读取结果证据，不模拟模型回复、工具选择、流式输出或原生执行。

FS/PG 成功证据 `tmp/live-cli-startup-cjr59p52`：真实长 MPI/UDF 和等待串行成员的跨 API 并发取消、全体观察到的出生身份退出、旧求解头/场文件保留、等待成员不启动及原 API 实际终止通过。独立恢复 API 保持旧批次历史和幂等，明确新批次的 MPI Job 39、UDF Job 44、串行 Job 42 各完成 400 单元求解、D/E 确认和实际后处理，场差与平均压力保持原阈值。两次真实 DeepSeek 回读均为 HTTP 200；模型仅回读证据。四个 worker（含基线）、临时 PG 和自建 Redis 已退出/删除/停止，清理错误为空。

`mpi-replacement-parent-log-receipts.json` 进一步绑定该 MPI 实际 helper 摘要和三个日志的本次原生摘要。三个旧日志在原始封存和查询来源中的旧摘要完整保留，本次发布内容匹配新执行摘要，二者不同；未忽略旧文件或放宽其他来源校验。

第一轮成功基线在确认接口暴露了已提交阶段被忙响应掩盖的问题，实际反例与修复见 [提交后清理](confirmation-cleanup-after-commit.md)。第二轮基线已完成，但组合脚本没有保留基线函数结束前的原账户，后续请求被正确拒绝为 404；失败位于 `tmp/live-cli-startup-ev749zql`，现已恢复原账户上下文继续真实验收。两次失败均没有模型调用。

第三轮 `tmp/live-cli-startup-oecj16hx` 已观察实际长 MPI/UDF 求解，但脚本误要求第三个成员必须已有 queued Job。清理也过早把公开 cancelled 状态当作原生清理已完成；后续实际探测确认四个 worker、原生出生身份均退出，两个私有原生目录已移除。保留原失败及清理错误，不将事后探测写成原清理通过。现改为等待持久终态、已启动进程的停止证明和成果结算后再关闭 worker。

第四轮 `tmp/live-cli-startup-8uxmmtqu` 已完成跨 API 并发整批取消、所有观察到的原生出生身份退出、旧成果与场数据保留、等待成员未启动和原 API 终止。随后新批次的 B 确认已提交，但因真实账户存储争用返回 `workflow_dispatch.status=pending`；脚本只允许并发额度等待而误判失败。该轮真实模型调用为 0，两个新 worker 正常退出、临时数据库删除、自建 Redis 停止均有回执。现在仅允许明确的并发上限或 `storage_quota_busy`/`validation_busy` 等待，仍要求实际 SQL 调度器推进和全部新结果完成；不重复提交已经确认的阶段。

第五轮 `tmp/live-cli-startup-n5mbpqje` 保留相同取消证据，并实际通过调度等待进入新批次求解。MPI 已完成分解、icoFoam 和重构，但查询收集器发现旧 `mpi-decompose.log` 变化而报错。这三个日志由原生父程序打开，在子进程 strace 范围之外；同目录明确重跑时，原完整封存包含旧日志，目录读取又使其成为来源依赖。修复仅向现有 `derived_inputs` 传入本次三个实际日志的最终大小与 SHA256，限定为原封存已有路径，完整原始来源仍保留，其他输入校验不变。修复前 `mpi_native.py` 和收集器在 `tmp/MPI-parent-log-before-fix-20261109`，实际失败 Job 38、完整 traceback 和正常清理均保留；模型调用 0。该修复与原 S3 UDF 单次 409 无归因关系。

第六轮 `tmp/live-cli-startup-ubvy14tx` 在成功基线的共用确认函数遇到与第四轮相同的合法存储等待，尚未走到 MPI 修复验收。先前只改新场景而漏改共用函数造成一次额外返工。现由同一个 `confirmed_dispatch_pending_kind` 检查两处调用，记录真实等待并继续依赖持久调度器；未知 pending 原因仍拒绝。原失败、数据库删除、自建 Redis 停止均保留，模型调用 0。

第七轮 `tmp/live-cli-startup-wkdorv5v` 的新 mesh Job 35 已提交成果，但实际 worker 记录兼容复制发生 HTTP 503、code 为 None，结算保存了 `alias_error=HTTPException`，随后 C 确认拒绝旧 `B-mesh/boundaries.json`。模型调用 0，取消与清理证据保留。原日志缺少底层异常，不能根据后续通过反推此 503 的精确原因。配额失败日志现记录盘点原因及异常类型链，不输出底层网络异常文本、密钥或文件内容。

独立实际文件系统反例 `tmp/storage-inventory-churn-syi4axd0/result.json` 证明原盘点会在真实 `pending.tmp` 原子替换后因实际 lstat 的 FileNotFoundError 拒绝整次库存。修复只对这一类消失重扫整树，最多三次，保持原 deadline 与累计条目预算，不跳过条目。`tmp/storage-inventory-churn-bjvby2t5/result.json` 验证一次替换后准确计入 83 字节、连续三次变化仍拒绝、条目上限不放宽及故障后稳定盘点恢复；这是纯文件系统/故障证据，LLM 调用 0。该专项与原第七轮的 503 不建立未经记录的因果关系。

主线仍为 36/53、剩 17 项；N3-07 原四条 remaining 和 295 项全功能范围完整保留。进程清理、当前完整结果及源码归档在本阶段验收结束后记录。

## S3及归档结果

S3/PG完整通过证据 tmp/live-cli-startup-04kggbl4。新MPI Job 42、UDF Job 48、串行 Job 45及实际后处理通过，恢复API使用独立空缓存；两个真实DeepSeek回读HTTP 200。上述专项、失败和两套完整通过累计8次真实模型回读、34460tokens。13个临时PG删除、3个自建S3停止、9个自建Redis停止、30个worker出生身份退出及46个API端口无残留已核验。源码与全部Python AST见 tmp/mainline-N3-07-confirmation-professional-evidence/validation.json。

2026-11-09（专业批次取消与明确重跑）：FS/PG与S3/PG真实长MPI/UDF整批取消、等待串行不启动、所有观察出生身份退出、旧成果/历史/幂等保留通过；原API退出后新API明确三模式400单元新批次、D/E及实际后处理通过，S3独立空缓存。提交后清理真实SQL锁缺陷、MPI父进程重写旧日志来源及实际文件盘点原子替换缺陷已修复并分项验收。新增8次真实DeepSeek、34460tokens，仅独立回读，模拟0；纯文件系统故障另列。七轮组合失败与一轮S3专项错误保留；第七轮503底层原因未记录，不反推归因。主线36/53、剩17，worker猝死、同case准备批次取消、新跟踪范围及原UDF409继续，N3-07不关闭。见[实际范围与失败](professional-batch-cancel-recovery.md)。

