# 真实 LLM 联调记录与后续测试要求

2026-11-07（业务许可与早期中断）：SQLite与PG各四处真实API退出恢复、已许可但缺少宿主身份的阻断场景，各实际调用DeepSeek2次，合计6次25225 tokens，HTTP均200、模拟0。前两组证明早期恢复后的真实模型读取；第三组仅证明保留快照仍可供模型读取，不代表被阻断的清理已完成。严格纯字符串格式三组均未通过。[逐组调用、九次实际退出和保留边界](native-scope-admission-recovery.md)。

2026-11-07（业务作用域持久生命周期）：新范围分配/入场/撤销已接入原镜像清理请求。SQLite取消链路真实DeepSeek2次8449 tokens、PG取消链路2次8561 tokens、SQLite两次真实API退出恢复链路2次8493 tokens，合计6次25503 tokens，HTTP均200、模拟0。模型实际选择工具读取恢复文件并续答，三组严格纯字符串格式仍未通过；[逐组证据及尚未覆盖的中断窗口](native-scope-business-lifecycle.md)。基础设施和证据故障单独计数，未运行含模拟LLM或审查哈希已过期的历史集合。

2026-11-07（创建前握手兼容）：北京时间13:47:03/13:47:05，当前控制实现下的既有SQLite恢复链路调用 `api.deepseek.com` / `deepseek-flash` 2次，HTTP均200，共8439 tokens，模拟0。[调用证据](../../tmp/sandbox-recovery-ui-retirement-2qo18hsz/live-result.json)包含实际工具选择、随机恢复文件读取及续答；严格纯字符串回复仍为false。新分配机制的7项基础设施和1项协议故障单独计数，不计作模型调用；业务数据库接入尚未完成，因此不能称新分配恢复已完成端到端模型验收。见[本阶段证据与接入顺序](native-scope-allocation.md)。

2026-11-07（用户再次提醒后现场复验）：北京时间13:40:23—13:40:28，运行已审查的 `live_llm_acceptance.py --source-profile-id 26`，实际向 `api.deepseek.com` 的 `deepseek-flash` 发出4次推理请求，全部HTTP 200，共11580 tokens，模拟调用0；另有1次模型列表查询，不计入推理次数。翻译、流式问答、模型自主选择 `read_text_file` 读取随机文件及后续真实模型回复、会话持久化通过。[本次调用回执](../../tmp/live-deepseek-s9r_f445/live-result.json)和同目录 `upstream.jsonl` 保存上游响应ID、服务日期与用量。执行进程退出码0，隔离API的50264端口已关闭，开发库未修改。本次只证明上述模型链路，不代表尚在实现的cgroup持久恢复通过；未新增主线完成项，主线仍33/53完成、剩20，295项全功能最终验收标记仍待完成。后续每阶段分别报告真实模型调用、实际基础设施及协议/故障测试；真实服务失败时记录失败或未验证，不回退到模拟。

2026-11-07（持久启动实现绑定）：SQLite实际恢复链路调用DeepSeek2次8624 tokens，[证据](../../tmp/sandbox-recovery-ui-retirement-1sjhl5ao/live-result.json)；PostgreSQL链路调用2次8429 tokens，[证据](../../tmp/sandbox-recovery-ui-retirement-yyhngjwp/live-result.json)。合计4次真实调用17053 tokens，api.deepseek.com/deepseek-flash、HTTP均200、模拟0；两组严格纯字符串回复仍未通过。独立cgroup控制通道检查无模型行为；实际descriptor尚未接入请求，因此这不是新cgroup持久恢复的端到端验收。见[当前实现和下一步](native-scope-control.md)。

2026-11-07（Windows原子Job创建）：生产宿主启动方式改变后，实际PG准备取消重试链路调用DeepSeek2次8391 tokens，[证据](../../tmp/sandbox-recovery-ui-retirement-oiyp7pli/live-result.json)；SQLite实际API在created/native_ready退出后的恢复链路调用2次8724 tokens，[证据](../../tmp/sandbox-recovery-ui-retirement-rlr67p2a/live-result.json)。合计4次真实调用17115 tokens，服务api.deepseek.com、模型deepseek-flash、HTTP均200、模拟0；两组严格纯字符串回复均未通过。新Windows入口5组实际基础设施检查无LLM行为，不能混算模型次数；WSL持久范围恢复尚未接入。见[边界与后续](windows-job-creation.md)。

2026-11-07（共享启动门兼容）：北京时间12:48:11/12:48:13，经真实平台恢复/清理/文件写回链路调用 `api.deepseek.com` 的 `deepseek-flash` 2次，HTTP均200，共8530 tokens，模拟0。[调用证据](../../tmp/sandbox-recovery-ui-retirement-rfjcv5tx/live-result.json)包含上游流ID、usage及实际工具读回结果；严格只回复凭证字符串仍未通过。新cgroup内核/门的10组测试本身没有LLM行为；持久恢复集成尚未完成，不能把本次兼容结果当作其端到端验收。见[当前边界与后续](native-cgroup-startup.md)。

2026-11-07（准备取消与存活API）：真实复现已终止请求被存活API误阻塞，新增terminal_request所有权隔离依据，仍要求已知子进程实际退出；修复准备取消后786432bytes节点预留残留。SQLite/PG最终代码各3项基础设施和1项证据故障检查通过，活动所有者双API回归5项通过。本轮真实DeepSeek4次16938 tokens，模拟0；首个验证脚本PID假设失败已单独保存。出生身份缺失和未知WSL窗口仍拒绝。主线33/53完成、剩20；资源11/17完成、剩6；295项最终验收目标保持。 见[准备取消与预留释放](sandbox-image-live-owner.md)。

2026-11-07（清理启动观察与双库取消）：新增持久启动意图和created/host_ready/native_ready实际观察，created与native_ready之后API退出、未完成登记时的独立退出对账已通过；完全缺失出生观察或缺少WSL身份仍拒绝。SQLite/PostgreSQL双API竞争、连续三次取消、实际停止后接管和唯一回执通过；最终代码另复核PG。本轮真实DeepSeek4次、17092 tokens、模拟0，严格纯字符串格式仍未通过。未知WSL身份、PG启动对账、原生定点故障/权限/重启等继续；主线33/53完成、剩20，资源11/17完成、剩6，295项最终验收不缩减。 见[启动证据与双库验收](sandbox-image-startup.md)。

2026-11-07（原镜像清理中断恢复及界面）：已登记清理者的实际所有者/子进程退出核验、独立对账凭证和原日志保留已实现；七处真实API退出、9项基础设施和1项证据故障检查通过。新增界面确认/断网重开只查询/显式重试，实际S3联动删除及13项镜像浏览器检查通过。本轮2次真实DeepSeek调用8580 tokens，模拟0；严格只回复字符串仍未通过。未登记身份窗口、PG/竞争/取消及原生定点故障等继续；主线33/53完成、剩20，资源11/17完成、剩6，295项最终验收目标保持。 见[清理中断恢复与界面](sandbox-image-reconciliation.md)。

2026-11-07（原镜像注册清理后端）：新增terminal.cleanup原生请求、固定外层gate核验与volume.ext4删除、持久意图/完成回执及恢复互斥。FS/S3真实删除、完整镜像哈希变更拒绝、实际flock/只读loop占用拒绝、元数据/快照保留通过；新增6项基础设施、6项协议/故障、双库12项schema检查。实际删除后真实DeepSeek读回恢复文件，2次调用8513 tokens，模拟0。清理者猝死独立对账和界面入口待完成。新迁移c6a1de7402ef仅隔离库验收，开发库未迁移；主线33/53、剩20，资源11/17、剩6，295项最终验收继续。 见[注册原镜像清理](sandbox-image-cleanup.md)。

2026-11-07（暂存准备中断/并发取消）：清理意图改为先提交哈希，再写清单；只续写完整原暂存能重建出的已知前缀，未知字节不覆盖。9个实际API退出位置通过；SQLite/PostgreSQL两API竞争及连续取消3次，均先排空I/O再接续并只提交一次回执。新增14项实际基础设施、3项协议/故障检查，2次真实DeepSeek调用8397 tokens，模拟0；PG首次遗漏迁移失败保留，修复后复验通过。镜像/其他副本清理与全量资源边界继续，主线33/53、剩20，资源11/17、剩6，295项最终验收继续。 见[准备中断与并发取消](sandbox-staging-restart.md)。

2026-11-07（原始S3暂存清理）：已实现持久清理意图/回执、逐项核验删除、实际API删除中途退出后重试与界面确认；未知/变更文件阻止清理，快照及原镜像保留，成功后解除这份暂存的管理阻塞。新增3项实际基础设施、4项协议/故障和13项浏览器检查通过；清理后恢复文件由真实DeepSeek读取续答，2次真实调用8422 tokens，模拟0。原镜像、其他暂存/恢复副本清理和剩余故障矩阵未完成；主线33/53、剩20，资源11/17、剩6，295项最终验收继续。 见[原始暂存清理](sandbox-staging-cleanup.md)。

2026-11-07（真实流式丢字修复/快照独立读取）：实际DeepSeek暴露平台1334字符只剩1220字符的异步消费竞态，已修复；两轮真实上游/SSE/持久化逐字一致，取消/断线后续聊通过。原镜像目录实际移走后，FS/S3快照下载和确认写回通过，S3真实模型读回通过。本轮11次真实推理，其中3次属于修复前失败场景、8次属于通过场景；已报告39808 tokens，两次取消/断开usage未知；模拟LLM0。自动清理仍待实现，主线33/53、剩20，资源11/17、剩6，295项最终整项验收仍待。 见[真实流式验收](agent-stream-integrity.md)及[快照独立读取](snapshot-source-independence.md)。

2026-11-07：新增2次真实DeepSeek推理（8,334 tokens），模型读取真实WSL猝死后恢复并确认写回S3的随机内容，工具结果及回复均持久化。模型附带额外说明，严格简短格式不算通过；[本轮实际证据和边界](sandbox-retirement.md)。

2026-11-06，用户明确要求以后不允许模拟 LLM 调用。该要求已写入根目录 AGENTS.md。涉及模型回复、流式推理、模型工具选择或 Agent 模型回合的后续验收必须使用授权的真实模型；失败如实记录，不回退到模拟通过。历史模拟证据不能证明真实模型对接完成。

最新记忆/流生命周期增量增加 23 次真实推理，验证文本替换、记忆写入与回忆，并发现、修复真实断线导致会话无法释放的问题。累计 62 次实际推理请求，已报告 usage 至少 252,520 tokens；其中 4 次取消/断开没有最终 usage，未按零消耗计算。回忆漏字和修复前断线失败均保留，详见[本批实现、失败与复验](agent-memory-stream-lifecycle.md)。

最新读取增量修复了 S3 Agent 读文件/列目录/搜索只访问本地路径的缺陷，修复后 S3 与本地文件各 6 次真实推理通过，另保留复现缺陷的 2 次推理。累计 39 次真实推理、172,851 tokens；[读取实现、验收与未覆盖范围](agent-storage-read-tools.md)。

后续 S3 增量已经定位并修复远端提案基线缺陷，完成 SQLite/PostgreSQL 下的写额度和自动写入验收。新增 16 次真实推理，其中 1 次用于复现缺陷、场景失败，修复后 15 次属于通过场景；累计 25 次实际推理、112,782 tokens。[完整 S3 证据与剩余范围](agent-s3-write-capacity.md)。下面保留前两批的原始范围与计数。

## 本次实际执行

复用开发库中已经配置的 DeepSeek profile 26，只读提取并在内存中解密；通过隔离 API、SQLite、测试算例和独立加密 vault 执行，不修改开发账户配置和原会话。测试没有替换模型响应，没有启动 mock provider。

- 实际服务：`https://api.deepseek.com/v1`。
- 实际模型：`deepseek-flash`，模型列表接口确认可用。
- 模型列表请求 1 次，实际 `/chat/completions` 请求 4 次，均 HTTP 200。
- 非流式翻译通过正式 `/llm/translate` 返回“入口速度为 2 m/s。”。
- 流式会话通过正式消息接口获得模型回答，完成事件和 assistant 消息已落库。
- 工具回合中，模型实际选择 `read_text_file`，读取只存在于测试算例文件中的随机字符串，再经第二次模型调用返回该字符串；工具结果和最终回答均已落库。
- 上游返回的 4 个响应 ID、实际模型及 token 用量保存在验收回执，合计 11,588 tokens；没有把模型列表请求算成推理调用。

[实际调用回执](../../tmp/live-deepseek-r09eby83/live-result.json)包含上游响应元数据，另有同目录 `ask-events.json`、`plan-events.json` 和 `upstream.jsonl`。复验入口是 `server/live_llm_acceptance.py --source-profile-id 26`，必须显式指定来源 profile，不自动进入离线回归。单轮最多允许 8 个真实上游 HTTP 请求。

## 真实写工具审批增量

同日通过 `server/live_llm_approval_acceptance.py --source-profile-id 26` 新增 5 次真实 `deepseek-flash` 推理，均 HTTP 200，上游合计 23,942 tokens。累计两批 9 次推理、35,530 tokens，另有 1 次模型列表请求。测试使用隔离 SQLite 和本地文件存储，没有修改开发库，没有替换模型回复或模型工具选择。

- 模型实际提出 `propose_file_update`，审批前文件内容和基线哈希均保持原样，待审批记录已保存。
- 批准后写入准确内容，模型接收实际工具结果并完成续答，工具结果和会话历史已保存。
- 拒绝真实模型提出的修改后，文件保持原样，待审批状态清除。
- 提案后通过正式 API 外部修改文件，再批准时返回 `CaseWriteConflict`，保留外部内容；模型收到真实失败结果后结束回答。
- 三种状态再次批准均返回 409，没有新增上游模型请求。

[实际审批验收回执](../../tmp/live-deepseek-approval-ay1ibene/live-result.json)及同目录各阶段 SSE 文件、`upstream.jsonl` 保存调用证据。首次测试驱动漏传消息接口必填的 profile ID，返回 422，未触发上游调用；修正驱动后重跑上述未完成场景通过。[首次失败记录](../../tmp/live-deepseek-approval-sfeptr3o/live-result.json)保留，不算通过。

## 回归执行限制

`tools/run_regression.py` 在创建隔离服务或执行任何套件之前检查整个选择集合。`tools/regression-llm-review.json` 只允许经源码审查、不涉及 LLM 的套件，并绑定入口及相关辅助文件 SHA-256；未审查、已知模拟、审查后文件变化均拒绝执行，不静默跳过、不报告整套通过，没有命令行绕过选项。当前仅登记源码归档基础设施检查为允许，已确认 `proposal_smoke.py` 的 `ProposalMock` 伪造模型工具选择及回复，因此禁止；其他历史套件继续逐项审查迁移。该登记表依赖实际人工源码审查，不是任意 Python 行为检测器。

`--list` 仍可查看历史套件清单，其数量不是已准许运行或通过的数量。真实模型验收由明确的 `live_llm_*` 入口执行，不自动消费模型额度。[限制检查记录](../../tmp/live-llm-policy-check.json)验证了单独选择、混合选择和整个 offline 集合均在执行前被拒绝，以及辅助源码变化、未登记入口和损坏登记表的拒绝行为；没有启动旧套件或模拟 LLM。

## 还不能据此宣称完成的范围

目前证明模型连通、实际回复、流式协议、只读工具选择/执行/续答、SQLite/本地文件下的写工具批准/拒绝/过期冲突和历史保存。S3/其他数据库下的写工具审批、真实模型下的取消/断线/重试、长对话、完整专业任务、模型参数和每日 replay 尚需真实验收；不能用这些成功请求替代全功能交付。

过去大量存储、进程、数据库和协议检查仍是各自边界的证据，但不是 LLM 能力证据。历史回归集合含模拟模型或模拟 CLI 推理套件，后续不得直接整套运行：必须先审查集合并替换这些调用，逐步换成真实模型验收。纯存储故障注入不属于模拟 LLM，但必须在汇报中明确测试对象。主线仍为 33/53 完成、剩余 20；资源工作块 11/17 完成、剩余 6；295 项全功能范围保持不变。
