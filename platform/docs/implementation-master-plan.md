# VibeFlow 全功能实现总体计划

2026-11-25，用户移出 SCNET 专项 FF-I11 至 FF-I21、Codex 登录及账户 FF-F55、Studio OAuth FF-J02、支付与订阅 FF-J06、CI/CD 与每日回归 FF-J22、Semantic Scholar FF-E07。审计保留 295 项，当前交付范围 279 项。通用远程集群和 53 项主线继续生效。 [当前交付范围](full-function-delivery-plan.md#21-最新范围规则)

> **2026-11-02 全功能范围更新**：用户已要求完整实现所有功能，并先记录两部分计划。当前详细执行入口为 [下一大阶段主线计划](next-stage-mainline-plan.md)（53 项任务与 24 个故障场景）和 [全功能交付计划](full-function-delivery-plan.md)（295 项逐条任务、12 条历史决策复核与完整交付门槛）。原 ⏸ 项重新进入范围复核/实施队列。本文 v1.0 保留架构与主 WP 映射，旧估算/迭代安排不作为当前日历。

> **执行优先级更新（2026-11-02，用户要求接手并沿稳定 CAE 产品主线推进）**：先收敛 WP03–WP06/WP09 的“几何→网格→求解→后处理→报告”，用真实作业、独立数值参考和输入一致性门禁验收。已完成阶段保存旁路修复、求解/后处理证据追溯、报告快照/发布复核、隔离回归和三条真实闭环，详情以 `progress.md` 顶部为准。下一步是统一任务编排/取消/重启恢复与单实例并发写入边界；长尾功能仍保留在计划，按主线依赖排队。当前已接入Job幂等、原子claim和持久attempt，依赖/失败传播/确认核心已验收，N2-01写入协调已验收，下一单元补N2-02前端基线、封存输入写保护与剩余写入；详见 [case-write-coordination.md](case-write-coordination.md)。以下 v1.0 估算、历史验证数字和功能汇总不是本轮全量验收结论。

> 版本：2026-10-25，v1.0  
> 状态：当前执行基线；每个里程碑入口做范围与 ADR 复核，不把估算当成日期承诺。  
> 输入基线：[295 项功能差距审计](feature-gap-audit.md)、[功能对等摘要](parity-checklist.md)、[C/S→B/S 目标架构](plan-cs-to-bs.md)、[当前进度](progress.md)。  
> 适用范围：VibeFlow Studio 0.11.0 的 Web 重建，以及已明确的 B/S 多用户与生产化目标。  
> 执行状态（2026-10-27）：**WP01 已完成**，WP00 审计校验工具已落地，本轮 **35/35** 后端/专项回归通过（本次 Crossref/arXiv 均返回有效结果）。WP02 外部导入、Open Editors、`.foam`、冲突、路径格式、范围附件、文件事件和 case 复制已验收；A23 删除未实现。WP03 原始 16 块、schema v1、status 映射和 errors.entries 已验证。WP04 参数注入、返回形态、自动预览和网格质量门禁已验证。WP05/C06A MPI 串并行通过，C06B 自动编排已验证但取消/调度未实现。WP06/D11 六类后处理、D12 公式/uncertainty/comparisonTargets、D23 出版图、D24 报告门禁和 C14B 同网格参考场已验证，异网格/实验 validation、实验数据自动采集、多序列图和报告章节自动组织继续实施；增量已重跑 postprocess/report/latex。WP07/D25 通用结果 reader、series、file_path viz 和浏览器实际渲染已验证，sid 路径 cookie 修复 trame 子资源/wslink 鉴权；D06 自由控制点色图、导入导出和实际应用已验证；D09 scenes.py 执行上下文和 ViewSize 负向行为已验证；D10 VF_* 逐函数契约矩阵、截图和错误恢复已验证；D07 相机关键帧、播放/暂停/循环和真实 PNG 导出已验证；D02 分组 Pipeline、D03 Inspector 写回和 D05 五类 Sources/Filters 已验证；D16 相机预设、保存恢复和截图 hash 已验证；D08 受限 VF Python 控制台及任意 Python 副作用拒绝已验证；D26 viz 活跃续期和空闲回收已验证，多副本持久化与 server-manager 继续 spike。WP08/B06 cadscene replace/clear、文件事实源、GeoPanel 刷新、归属与背压已验证，重连快照、版本/乱序和多副本继续实施；WP08/B07 Base64 当前视图、Fit、边线、透明、主题接口及 watcher 代次防竞态已验证，完整 OCP meshData 仍归 B02/B03；WP08/B08 CAD 对象树与可见性、B14 截图、B20 STEP/STL 导出已验证；B11 三轴真实剖切、闭环/带孔端盖和源网格恢复已验证，非流形/超大模型性能待做；B12 爆炸时间插值、可关闭过渡及与普通动画互斥已验证，时长/缓动编辑待做；B22 Zebra blackwhite/colorful/grayscale、reflection/normal、角度、count/opacity 和纹理边界已验证，自定义配色编辑器和完整 studio texture mapping 待做；B09 透视/正交、Orbit/Trackball、四宫格真实视口、RoomEnvironment、shadow map、单视图 SSAO 和材质控制已验证，完整 studio 配置与多视图独立交互待做；B10 bbox 属性/中心距离和 B13 八类关键帧轨道编辑、播放/暂停/停止、时间 scrub、导出及互斥已验证，完整 ParaView 属性轨道待做。WP09/E01 自动 9 章和 E02 HTML/DOCX XML 内容已验证，E03 三种 LaTeX recipe 与 biber 已验证，E09 表格生成/插入备份已验证，E10/E04 及后续报告/文档工作流继续实施。

## 0. 计划目的与使用方式

本计划把当前“本机单用户 Alpha”推进到“可验证的 Web CAE 工作台”，再推进到“可部署的团队系统”。最终范围不因分阶段交付而缩减；但每个版本必须可运行、可回退、有测试证据，禁止为了勾选条目而一次性堆入未验证代码。

本计划替代旧方案中“3–4 人约 26 周”的工期估算。旧方案的目标架构仍可参考，但当前 295 项证据台账、真实遗留复杂度和生产缺口要求重新排期。

三层执行方式：

1. **里程碑 M0–M7**：决定先后顺序和发布门槛。
2. **工作包 WP00–WP17**：明确实现内容、文件入口、依赖、测试、回退和人日。
3. **审计 ID**：295 个功能项全部映射到且只映射到一个主工作包；跨包协作在正文说明，不重复计算工作量。

执行时每两周把下一迭代拆成 0.5–2 小时的原子任务或小型 PR。不得直接把“实现 ParaView”“实现 Agent”等大工作包当成一个提交。

**持续自主执行约定（2026-10-27 用户明确确认）**：

1. 按本计划、功能审计和 `progress.md` 连续完成未完成任务；一项实现并验证后，直接开始下一项，不等待用户逐步提醒，也不把阶段汇报当作停止点。
2. 技术故障、测试失败和可从前述文档/代码确定的需求细节由执行者自主定位、修复和复验；不要只写规划、技能文本或占位目录代替真实功能。
3. 只有确实需要用户决策的问题才暂停询问，例如真实 PostgreSQL 迁移、生产或不可逆外部动作、未授权凭据，以及无法从代码和既定计划决定的需求分叉。既有安全、备份、证据和不覆盖用户数据的约束持续优先。

## 1. 当前基线与不可破坏的契约

### 1.1 已有基础

- 当前runner枚举174个Python套件；最新offline16、core14、CAE3、execution3通过，并有双数据库迁移各20项、状态/outbox各14项及真实API被杀重启证明。前端build/Node25和浏览器为前轮证据，本轮没有重跑。开发库仍为12表/旧Alembic基线，新9表仅临时库验证；历史171/168/152/154不代表全集重跑，细节以`progress.md`为准。
- 已有 PostgreSQL、本地 fs、FastAPI、React 19、dockview、Monaco、xterm、VTK/trame、CadQuery、WSL OpenFOAM v2406。
- 已验证 caseDict 后端统一写入、Monaco 保存、errors.open/blocking 门控、2D/3D icoFoam、2 子域 MPI、网格质量、保存自动预览、文件事件、多工况派生、最小 functionObject D1–D5/G5、pandoc/LaTeX、Agent 双协议和审批、终端/作业、FTS、单段 Range/HEAD/Bearer 下载；WP09 已补 LaTeX 诊断/生命周期、PDF 页码、HTML/DOCX 阅读器和可编辑 PPTX 垂直切片。
- 当前审计：✅159、🟡118、🧪2、⬜0、⏸14、❓2。数量只用于追踪，不用于计算“对等百分比”；汇总由 `validate_audit.py` 按 295 行实际状态校验。历史 ⏸ 已在新计划重新登记，不能据此永久排除。

### 1.2 必须保持

1. **case 文件是事实源**；caseDict 的 DB 字段仅为镜像。任何新功能不得改成 DB 覆盖文件。
2. caseDict 写入顺序保持 parse → 文件写入 → DB 镜像/事件 commit；普通文件接口不得绕过专用写入。
3. 所有资源必须校验用户/组织归属；跨用户资源默认 404。
4. 危险工具在副作用前完成会话、case、tool_call、profile 和权限校验。
5. 不读取或复用未经授权的 LLM 凭据；默认使用 mock。真实模型测试需要单独授权。
6. 真实 PostgreSQL 的 stamp/upgrade 必须先备份、审查差异并明确执行，不得由测试或启动脚本自动完成。
7. 第三方资产按当前项目决策直接使用；这不替代功能与运行验证。
8. 测试通过只代表断言覆盖，不自动把审计项改成 ✅。

## 2. 范围、排除和决策边界

### 2.1 核心交付范围

WP00–WP16 覆盖核心 Web 工作台、CAE 主链、Agent、Flotherm、团队与生产化，共 258 个审计条目，其中包含需要保持回归的已完成项。

### 2.2 扩展包范围

WP17 单独管理以下能力，避免阻塞核心版本，但仍有明确入口和验收：

- 其他开源求解器后端与远程/HPC。
- 深度 Codex stdio、登录、原生恢复/fork、复杂审批和容量重试。
- SCNET 可选远程资源工作台。
- 崩溃上报、语音输入等新增需求。
- 商业后端仅在组织自备 license/remote/HPC 条件具备后启动。

### 2.3 历史暂缓项重新登记

A15、C09、D17–D21、E11/E12、G07/G10、I21、J03/J24 的旧 ⏸ 是历史证据状态，不再作为永久排除理由。按最新全功能目标，逐项补证、恢复实施或单列原版未提供的新增扩展。K01–K12 按新计划第 2 节复核；保留 Web 架构不等于可以删掉可见能力。商业运行环境、真实账户和原版未知行为的依赖单独记录，不以 mock 宣告完成。

### 2.4 需要先补证的未知

- J02：Studio OAuth 是否存在及其准确语义。
- I11：SCNET 在当前安装包是否启用。
- ParaView 完整菜单、28 插件、Color Map/Animation 细节。
- update_goal 的精确预算/停止条件、web_search 提供商、会话导出格式、图片附件完整输入方式。

每个未知先做时间盒研究，禁止把推测写成原版事实。

## 3. 团队、估算与迭代假设

### 3.1 建议团队

| 角色 | 建议投入 | 主责 |
|---|---:|---|
| 技术负责人/架构 | 0.5–1 FTE | ADR、接口、依赖、评审、发布门槛 |
| 后端工程师 ×2 | 2 FTE | case/file/Agent/job/storage/org |
| 前端工程师 ×1–2 | 1–2 FTE | Open Editors、Agent、CAD、ParaView、报告 |
| CAE 工程师 ×1 | 1 FTE | OpenFOAM、网格、MPI、UDF、数值验证、Flotherm |
| QA/DevOps | 0.5–1 FTE | CI、E2E、黄金算例、安全、部署、性能 |

低于 4 FTE 时不应承诺全范围并行；优先完成 M0–M3，再评估后续。

### 3.2 估算口径

- 1 人日 = 一名工程师的一天有效工作量，包含实现、测试、评审和文档，不含请假与管理 overhead。
- 工作包估算为区间；ParaView、Flotherm、SCNET、真实数值对拍的不确定性为 ±35% 或更高。
- 核心 WP00–WP16：**约 1510–2380 人日**。另留 15%–25% 集成与发布储备。
- 若启用 C08、C16、深度 Codex、SCNET、G11/G12：在核心之上另增约 **450–865 人日**；商业后端和暂停项不计入该总数。
- 以 5–7 FTE 并行、依赖顺利为前提，核心版本是约 18–36 个月量级，而不是旧估算的 7 个月。只承诺下一至两个迭代的具体日期。

## 4. 总体路线与发布门槛

| 里程碑 | 主工作包 | 可交付版本 | 必须通过的门槛 |
|---|---|---|---|
| M0 安全与证据地基 | WP00、WP01 | Local Alpha Hardened | 当前 21 套件稳定；新增副作用/命令/环境/存储负向测试通过；无已知默认断裂启动 |
| M1 项目与 caseDict 核心 | WP02、WP03、WP13 | Foundation Beta | 外部 case 导入、Open Editors、16 块、case_events、watcher/index/events 形成闭环 |
| M2 Agent 与运行时 | WP10、WP11、WP12 | Agent Workbench Beta | web 工具安全、WSL/沙箱 runtime、取消/checkpoint/恢复、reasoning、Goal、图片、长历史通过 |
| M3 CAE 主链深化 | WP04、WP05、WP06 | CAE Pro Beta | 几何参数、网格质量、MPI、通用 UDF、多工况、后处理指标与黄金数值证据通过 |
| M4 可视化与文档 | WP07、WP08、WP09 | Studio Experience Beta | 通用阅读器、Sources/Filters、CAD 高级交互、报告/PPT/文档阅读通过 |
| M5 Flotherm | WP14 | Converter Release | G6 输入矩阵、逐文件 diff、功率平衡、覆盖报告、取消/覆盖安全通过 |
| M6 团队与生产 | WP15、WP16 | Production Candidate | 组织 ACL、硬沙箱、S3/fs 一致、Redis/worker、备份恢复、TLS、监控、负载与渗透通过 |
| M7 扩展包 | WP17 | 独立 Add-on | 每个后端/远程/Codex/SCNET 包单独验收，不影响核心稳定性 |

M1–M4 可按团队并行，但必须遵守第 5 节依赖。M6 的安全设计从 M0 开始，不能到最后才补。

## 5. 关键依赖顺序

```text
测试证据/安全热修
  ├─> 统一 v2406 与命令执行策略 ─> 几何/UDF/MPI/Flotherm
  ├─> case/file canonical 与归属 ─> 外部导入/Open Editors/watcher
  ├─> caseDict 16 块+事件 ─> 几何/网格/求解/后处理/报告自动编排
  ├─> 作业取消+checkpoint ─> Goal 自动循环/长会话恢复
  ├─> web_fetch SSRF 与出口策略 ─> web_search/联网 Agent
  ├─> 通用结果读取+指标模型 ─> ParaView/出版图/报告/PPT
  ├─> 统一存储适配 ─> Redis/worker/多副本/生产部署
  └─> 组织 ACL+硬沙箱+审计 ─> 不可信用户/公网部署
```

不得倒置的依赖：

- 未做命令隔离和审批前，不扩大 Agent/CLI 命令能力。
- 未做存储一致性前，不以 S3 生产默认启动完整 CAE 链。
- 未建立参考数据 provenance 前，不写“数值验证通过”。
- 未完成取消与配额前，不启用 Goal 自动续轮。
- 未完成备份恢复演练前，不执行实库纳管或破坏性 migration。

## 6. 工作包总表

| WP | 名称 | 主审计条目数 | 人日估算 | 里程碑 |
|---|---|---:|---:|---|
| WP00 | 测试、证据与交付基础设施 | 横切 | 30–45 | M0/全程 |
| WP01 | 确定缺陷与安全热修 | 5 | 45–70 | M0 |
| WP02 | 项目、外部 case、Open Editors 与文件体验 | 11 | 55–85 | M1 |
| WP03 | caseDict 16 块、schema、状态与审计 | 28 | 90–130 | M1 |
| WP04 | 几何上下文、网格与质量门禁 | 11 | 80–120 | M3 |
| WP05 | 求解、UDF、MPI 与多工况 | 17 | 130–210 | M3 |
| WP06 | 后处理指标与数值证据 | 7 | 90–140 | M3 |
| WP07 | ParaView、通用读取与会话 | 16 | 140–220 | M4 |
| WP08 | CAD 实时协议与高级交互 | 11 | 80–130 | M4 |
| WP09 | 报告、文档查看器、Texo 与 PPT | 21 | 90–150 | M4 |
| WP10 | Agent 聊天、provider 与展示 | 23 | 80–120 | M2 |
| WP11 | Agent 工具、Goal、会话与恢复 | 31 | 90–140 | M2 |
| WP12 | CLI、终端与运行时 | 16 | 70–110 | M2 |
| WP13 | 作业、索引、补全与事件总线 | 22 | 80–130 | M1/M6 |
| WP14 | Flotherm2foam Studio/CLI | 14 | 140–220 | M5 |
| WP15 | 组织、订阅、配额与安全策略 | 13 | 90–150 | M6 |
| WP16 | 存储、worker、沙箱、部署与运维 | 12 | 130–210 | M6 |
| WP17 | 后端、Codex、SCNET 与可选扩展 | 37 | 分包估算 | M7 |

## 7. 工作包详细设计

### WP00：测试、证据与交付基础设施

**目标**：建立后续所有功能共用的验收体系，先解决“代码存在就算完成”的问题。

**主要入口**：[server 测试目录](../server)、[浏览器工具](../tools)、[测试矩阵](testing.md)、[部署基线](deployment.md)。

**任务**：

1. 固化当前 21 个套件的依赖、数据隔离、SKIP 语义和输出归档；每个失败必须显示首个真实错误。
2. 建立 unit/API/browser/CAE/security/performance 六层测试目录和命名规则；smoke 不得依赖数据库“第一条 case”。
3. 建立黄金数据 manifest：输入 hash、来源、环境版本、期望产物、数值容差、是否为参考对拍或仅回归基线。
4. 建立浏览器截图+DOM+网络+实际产物联合断言；截图必须人工查看。
5. 建立 CI 分层：快速单元/静态检查每次提交，API/浏览器每日，真实 CAE/安全/性能按里程碑。
6. 增加依赖完整性检查，不能只依赖 pip check；检查 requirements 中直接依赖可导入。
7. 文档状态自动校验：审计 ID 唯一、状态计数一致、计划映射无遗漏/重复。

**验收**：干净环境可重复运行；失败有日志；SKIP 不写成 PASS；295 项映射脚本输出 MISSING=[]、DUPLICATES={}。

**回退**：CI 分层和新增测试均为增量；不通过的新门禁先标 required=false 并记录到期日，不得永久 optional。

### WP01：确定缺陷与安全热修（已完成，2026-10-25）

**主条目**：B21、G13、H20、H21、J29。

**原则**：先写 RED，再做最小修复，不借热修重构整个系统。

| 原子任务 | 文件入口 | 成功标准 | 预计 |
|---|---|---|---:|
| 内联导出 RED：preview 顶层调用导出不得产生文件，export 必须产生 | [geometry_smoke.py](../server/geometry_smoke.py) | 旧代码失败，断言覆盖两种模式 | 1–2h |
| preview 用 try/finally 临时禁用内联导出，恢复原始函数 | [geo_script.py](../server/app/services/geo_script.py) | 内联导出被抑制；B19 钩子和显式 export 回归通过 | 2–4h |
| 抽取共享命令安全策略并用于 Jobs 与 Agent | 新增 server/app/services/command_safety.py；[jobs.py](../server/app/routers/jobs.py)、[tools.py](../server/app/services/tools.py) | 高危命令在创建进程前拒绝；安全 echo/pwd 通过；无 Job 半成品 | 2–4h |
| runtime probe 统一 v2406，WSL argv 使用 --exec | [runtime.py](../server/app/routers/runtime.py)、[jobrunner.py](../server/app/services/jobrunner.py) | command -v 指向 v2406；变量在 source 后展开；wmake 检测正确 | 2–4h |
| prod 默认存储增加确定性保护 | [docker-compose.prod.yml](../infra/docker-compose.prod.yml)、[compose_smoke.py](../infra/compose_smoke.py) | S3 未全链路支持时 fail-fast 或显式安全 fs；不允许静默断裂 | 2–4h |
| 修复无扩展名 OpenFOAM 场文件语言识别 | [EditorPanel.tsx](../web/src/panels/EditorPanel.tsx) | 0/U、0/p、controlDict 和普通无扩展名文件各有正确语言 | 1–2h |
| 补 viz 429 与 OpenFOAM 环境实测 | [quota_smoke.py](../server/quota_smoke.py)、[runtime 测试](../server/smoke_test.py) | viz 超限 429、还原成功；不把 1912 误报为目标环境 | 2–4h |
| 回归与文档同步 | [testing.md](testing.md)、[feature-gap-audit.md](feature-gap-audit.md)、[progress.md](progress.md) | 全部相关套件通过；只修改有证据的状态 | 1–2h |

**回退**：所有改动保持向后兼容；存储保护可用显式配置退出，但启动日志必须显示不安全原因；不迁移数据库。

### WP02：项目、外部 case、Open Editors 与文件体验

**主条目**：A01、A02、A03、A04、A11、A13、A19、A21、A23、A25。

**实现步骤**：

1. **外部 case 导入**：Web 默认采用 ZIP/目录上传导入，不开放任意服务器路径。校验 caseDict/StandardCase、ZIP slip、符号链接、大小、配额、重复名称；导入到当前用户的新 case ID。
2. 如保留服务器目录打开，仅允许管理员配置的 allowlist 根目录，并采用复制/登记分离策略；生产禁止任意路径注册。
3. **Open Editors**：利用现有 openFiles/activeFile 状态，新增标签条、切换、关闭、关闭其他/全部、Save all、未保存标记和外部修改冲突提示。
4. 补编辑器操作组：Add to chat、Copy Path/Relative Path；Show in File Explorer 在 Web 以复制 canonical path/下载替代，本机服务端打开必须单独授权且生产禁用。
5. 文件路由增加 OpenFOAM 结果、STEP/STL、VTK 系列的专用入口；未知格式回退安全文本/下载，不执行内容。
6. workspace/case 重命名、复制、移动、删除：先定义软删除、运行中作业约束、文件与 DB 一致性及审计；禁止直接删除唯一用户数据。
7. 实现 `@path:start-end` 的行列解析、预览和取消引用；Windows/WSL 路径只做显示转换，不改变 canonical storage key。
8. 外部修改冲突使用时序 E2E 固化，必要时增加 content hash；不尝试未定义的自动合并。

**主要文件**：[workspaces.py](../server/app/routers/workspaces.py)、[files.py](../server/app/routers/files.py)、[store.ts](../web/src/store.ts)、[TreePanel.tsx](../web/src/panels/TreePanel.tsx)、[EditorPanel.tsx](../web/src/panels/EditorPanel.tsx)；新增 case_import、OpenEditors 组件与导入 smoke。

**验收**：外部 golden case 可导入并运行；恶意 ZIP/跨用户/超配额被拒绝；多标签保存/关闭/冲突通过；复制/删除在文件或 DB 失败时可恢复且不丢原 case。

**回退**：导入永远新建 case；删除先软删除；新标签 UI 可回退单编辑器。

### WP03：caseDict 16 块、schema、状态与审计

**主条目**：A05–A09、A14、A16–A18、A20、A22、A24、CB01–CB16。

**实现步骤**：

1. 建立版本化 block registry：每个块的字段类型、必填、枚举、单位、默认值、跨字段约束和向后兼容策略。
2. 脚手架补齐 regions/interfaces/provenance/extensions；保留未知块、注释、扩展字段和原有 workflowState，禁止模板覆盖用户文件。
3. 为 materials、regions、interfaces、boundarySets、initialConditions、loadCases 建立专用校验和生成器；不支持语义必须返回可读错误，不得静默保存成“看似有效”。
4. 建立 caseMeta.status 与 A/B/C/D/E/V 映射表、允许迁移、用户确认、字段齐备、网格质量和 errors 门控。
5. errors 增加 entries、blocking、fixOptions、resolution、verification 的生命周期；结清必须记录证据，不是简单把计数改零。
6. 新增 case_events：记录 actor、旧/新状态、revision、原因、时间和关联 job/artifact；与 DB 镜像同事务，文件与 DB 失败通过 reconciliation 标记并恢复。
7. intent/provenance/decisionLog/artifactTrace 与几何、求解、后处理和报告产物关联。
8. V=UDF 阶段读取 UDF/README 和编译/集成结果，不直接把目录存在当作 D1–D5 完成。

**主要文件**：[casedict.py](../server/app/services/casedict.py)、[models.py](../server/app/models.py)、[migrations](../server/migrations)、[CasePanel.tsx](../web/src/panels/CasePanel.tsx)；新增 casedict_schema、case_events、reconciliation 服务。

**验收**：16 块模板/读写/注释保真；所有合法/非法迁移有测试；open/blocking、缺字段、网格失败均阻断；事件可追溯；文件成功但 DB 失败时不会假装一致。

**回退**：schema 校验先 warn 再按块启用 fail；新表 additive；原 caseDict 自动备份且可恢复。

### WP04：几何上下文、网格与质量门禁

**主条目**：B01–B05、B15–B19、B23。

**实现步骤**：

1. Ctx 注入 caseDict、data/params/domain、脚本路径和 mode；补 build_geometry/build、result/objects、dict/list/tuple/单对象兼容矩阵。
2. 固化自动预览：保存 → 作业 → scene 更新 → 当前面板刷新；失败保留上一有效 scene 并显示错误。
3. 接入 B21 的内联导出保护；export 产物带输入 hash、参数、版本和 manifest。
4. 增加 topology、scale、symmetry、periodic、watertight/manifold 检查与 validation artifacts。
5. 网格来源统一 generated/imported/solverNative/external；质量预设、单元类型、边界层和自适应有明确后端能力矩阵。
6. blockMesh 保持回归；snappyHexMesh 建立最小真实用例；gmsh/cfMesh 先安装/构建 spike，再决定默认 adapter。
7. checkMesh 指标结构化，qualityTargets 支持 fail/warn/allowWithReason，并接入 caseDict 和 workflow gate。
8. convertedMesh/qualityReport/boundaryReport 写入统一 artifact manifest。

**验收**：几何参数改变能真实改变模型；preview 无导出副作用；网格阈值能阻断；snappy/gmsh/cfMesh 只对实际通过的链路标 ✅。

**回退**：新网格 adapter 可独立关闭；保留原输入、日志和上一可用网格。

### WP05：求解、UDF、MPI 与多工况

**主条目**：A10、C01–C07、C10–C13、C15、C17–C19。

**实现步骤**：

1. 所有 WSL 作业统一 v2406 环境、--exec、路径引用和进程组取消；保留 1912 仅为临时对照，完成后退出默认路径。
2. 建立 solver adapter 接口：输入生成、校验、执行、日志解析、产物和 restart；不允许每个求解器复制一套 job 特判。
3. 多工况增加参数矩阵、批量提交、并发/公平、批量状态和失败隔离；派生仍精确保留 0/。
4. MPI：decomposePar → mpirun → reconstructPar，支持 2/4 子域、processor 结果、进程组取消、失败清理和串并行结果比较。
5. UDF 按 D1–D5 建立 case 内阶段引导和 artifact：需求、设计、源码、构建日志、加载、最小算例、集成结果。
6. 在最小 functionObject 之后依次增加自定义边界条件、fvOption、湍流模型和求解器；每类独立编译/加载/运行/数值行为测试。
7. 标准 Allrun/Allclean/Allrun.pre、0.orig、canonical 输入保留和 restartPolicy。
8. 扩展 pitzDaily、damBreak 及传热/湍流/可压缩等用例；只按实际运行矩阵更新状态。

**验收**：MPI 与串行结果在规定容差内一致；取消能终止整个进程组；每类 UDF 都有真实执行证据；批量工况互不误写。

**回退**：adapter 按求解器启用；批量默认串行；失败工况不阻塞其他工况，也不删除产物。

### WP06：后处理指标与数值证据

**主条目**：C14A、C14B、D11、D12、D14、D23、D24。

**实现步骤**：

1. 实现专用 postprocess 执行器，统一输入、日志、指标 JSON、图表和 artifact manifest。
2. 增加 pointProbes、lineSamples、surfaceIntegrals、volumeAverages、modalQuantities、frequencySpectra。
3. 支持 formulas/referenceValues/comparisonTargets/uncertainty；公式沙箱禁止任意 Python。
4. 建立参考数据来源：原版输出、公开基准或明确回归基线；没有参考来源时只能称行为/回归验证。
5. 黄金用例按 manifest 比较 U/p/forces/流量/温度等；容差按物理量和网格研究定义，不能所有用例机械套一个数。
6. 出版图支持单位、色盲友好、PDF/SVG、300–600dpi PNG 和 Agg；图中记录 case/run/commit/数据来源。
7. 建后处理证据表和 readiness：缺输入、单位不明、参考缺失、指标失败时阻止报告宣称完成。

**验收**：指标有独立重算测试；黄金场/力/流量对拍通过；图件内容、单位和来源可追溯；无参考时状态不会被写成 V&V 完成。

### WP07：ParaView、通用读取与会话

**主条目**：D01–D10、D13、D15、D16、D22、D25、D26。

**阶段 1：两周技术 spike**

- 验证 pvpython/EGL、OpenFOAMReader、wslink、server-manager XML 导出、内存和当前代理兼容。
- 若完整路线失败，启用 trame-paraview iframe 或当前 VTK/trame 的可交付降级，并明确不对等项，不伪装成完整 ParaView。

**阶段 2：实现**

1. 通用 VTK/VTU/XDMF/Exodus/STL 阅读器和文件选择入口；reader 能力与技能声明一致。
2. Pipeline 树、活动对象、多选、状态图标和完整 Information。
3. server-manager XML → JSON schema → React 面板；Top-20 高频过滤器手工精修。
4. Sources/Filters 由 XML/注册表生成命令目录，记录原版未确认清单。
5. Color Map 自由控制点、导入导出、高级映射；Animation 关键帧和相机轨道。
6. scenes.py 注入 __file__、VIBEFLOW_PARAVIEW_SCRIPT、禁设 ViewSize；逐函数测试 VF_* 契约。
7. pvpython/pvbatch 和受控 Python console；console 在 viz kernel 沙箱，不进入宿主任意 Python。
8. viz 会话持久化、短期票据、空闲回收和多副本方案；完成内存/延迟/50 用户专项。

**验收**：每类 reader 有真实文件；XML 面板可创建/修改/删除 proxy；scenes 上下文契约通过；长时运行无泄漏，跨副本不丢所有权。

**回退**：通用面板按 filter 回退手写面板；新 reader 失败不影响 OpenFOAM；保留轻量 VTK 路径直至门槛通过。

### WP08：CAD 实时协议与高级交互

**主条目**：B06–B14、B20、B22。

**实现步骤**：

1. 接入 cadscene WS：scene.replace/scene.clear、重连快照、版本号和乱序丢弃。
2. 保留并直接使用现有 CAD 资产，完成 Base64 场景、Fit、边线、透明、主题接口。
3. CAD 树、选择、折叠、可见性和状态保留；对象 ID 与 scene manifest 一致。
4. 相机预设、正交/多视角、trackball、studio 环境、阴影/AO 和材质控制按优先级分组。
5. Distance/Properties、三轴剖切/端盖、爆炸视图、CAD 关键帧、截图回传。
6. Zebra 条纹分析及 count/opacity/direction/color_scheme/mapping_mode 控制。
7. GeoPanel 增加 STEP/STL 导出按钮，复用后端 export 和 Bearer Blob 下载。

**验收**：协议回放一致；浏览器实际点击选择/剖切/爆炸/Zebra/动画并检查像素或场景状态；导出文件可下载且内容有效。

### WP09：报告、文档查看器、Texo 与 PPT

**主条目**：E01–E10、E13–E22、F65。

**报告与 LaTeX**：

1. 从 caseDict 和证据表组织报告章节、假设、方法、结果、限制、V&V 和结论。
2. DOCX 解包检查文本/表格/图片，不只检查文件存在；HTML 使用安全消毒。
3. xelatex/lualatex/pdflatex、biber 分 recipe 实测；禁 shell escape、超时、防抖、过期结果丢弃。
4. LaTeX 错误结构化、源位置、PDF 回跳；表格生成器处理转义/booktabs/边框。
5. 引用样式、Semantic Scholar 429/退避和失败行为；不阻塞离线引用。

**阅读器与 Texo**：

1. PDF.js 接入现有 Range/HEAD，增加目录、搜索、页面状态和大文件性能测试。
2. PDF 页码生成保留原文件并输出新 artifact；验证页数、位置和内容未破坏。
3. HTML/DOCX 专用安全阅读器；DOCX 不执行宏或嵌入脚本。
4. CSV 使用标准解析器处理引号/转义；大表分页/虚拟化。
5. 图片查看器补独立硬断言和 panzoom；Texo 建准确率样本集。
6. 屏幕框选采用浏览器 getDisplayMedia/用户授权的区域截图替代，明确与桌面全局框选差异。

**PPT 子链**：

1. python-pptx 生成/编辑真实 PPTX，先实现文本、形状、图片、表格和模板。
2. 接入 Beautify、原生填充和 187 presets；每项有 manifest，不以 preset 数量代替渲染验证。
3. 图片重建和叙述/视频分别建独立任务；无真实产物不得标完成。
4. 解包验证 PPTX XML，使用可用渲染器检查页面截图和可编辑性。

**回退**：新阅读器与报告生成均为增量；原 report.md、PDF、DOCX、PPTX 永不覆盖唯一副本。

### WP10：Agent 聊天、provider 与展示

**主条目**：F01–F12、F14–F18、F29、F33、F34、F67。

**实现步骤**：

1. 固化 text/reasoning/tool delta、非流式和错误事件 schema；旧格式通过版本化 adapter。
2. reasoning 单独显示、折叠和持久化，只处理 provider 实际返回的 reasoning/summary，不伪造推理内容。
3. 流式 Markdown 保留稳定 DOM 块，完成后权威重渲染；补长回答/代码/缓存降级。
4. 工具卡补齐 queued/cancelled/interrupted；消息复制、修改摘要、回底部和用户消息导航。
5. 空回复/占位/伪工具调用/DSML/sentinel 清洗，保留真实修改摘要。
6. profile 编辑、连接测试、动态模型、推理强度、失败回退；endpoint 默认 HTTPS，仅回环 HTTP 例外。
7. 上下文用量、跨环境交接、模型窗口/显示/日志分离和有界回放。
8. 图片附件使用 case 归属上传、MIME/大小/配额校验和 provider 适配；禁止把任意 file:// 路径直接发给前端或模型。
9. 统一超时、大小限制、断开取消和秘密脱敏。

**验收**：OpenAI/Anthropic mock 契约、reasoning、图片、长历史、停止/断开、秘密扫描和浏览器实际交互全部通过。

### WP11：Agent 工具、Goal、会话与恢复

**主条目**：F19–F28、F31、F32、F35–F40、F42、F43、F45、F46、F48–F51、F60–F62、F66。

**工具与网络安全**：

1. read/list/search 补直接 smoke 和统一重型目录排除。
2. run_shell_command 增加明确 runtime；开发期支持 Windows/WSL，生产只映射到受控 sandbox。
3. web_search 使用 provider adapter、runId 隔离、共享预算和取消；默认 mock，不偷偷使用真实搜索服务。
4. web_fetch 实现 DNS 解析与 IP 校验、私网/环回/链路本地拒绝、重定向复查、固定 IP 连接、Host/SNI/证书一致性、8MiB 和超时。
5. update_plan 约束最多一个 in_progress 并持久化；文件提案增加基线 hash、原子写、外部修改保护和只读历史。

**Goal 与会话**：

1. Goal 数据模型包含目标、验收条件、预算、状态、停止原因；update_goal 只产生事件，不直接启动无限循环。
2. Goal runner 依赖取消、配额、checkpoint 和最大迭代；每轮重新评估停止条件，用户可立即终止。
3. 每会话停止、运行配置锁、后台任务注册和页面离开恢复。
4. checkpoint 采用事件序号和幂等写入；覆盖 750ms/16KiB 策略、终态 flush、半截回答和进程重启。
5. 新会话恢复摘要、任意已完成回答分支、会话重命名/删除/导出；运行中禁止危险删除。
6. 长历史归档、显示代次恢复和每轮计时；DB 事件不因显示截断而丢失。
7. Plan 模式只读优先、批准和恢复；memory.md 有明确写入/压缩策略。
8. 技能目录增加用户/组织 fallback、安装/更新/打包和 openai.yaml 元数据；技能声明通过能力测试。

**验收**：SSRF/重定向/大小攻击全部拒绝；审批前无副作用；重启后恢复不重复执行工具；Goal 超预算/取消/完成均停止；导出可重放。

### WP12：CLI、终端与运行时

**主条目**：F52、F54、F57、F58、F59、F63、F64、G01–G06、G08、G09、G14。

**实现步骤**：

1. 终端对齐 pending/chunk/scrollback、滚动跟随、IME、浅色 SGR 和光标行为；用 vi/htop/TUI 做人工验收。
2. 终端选区/截图/输出一键加入 Agent，复用图片/文本附件链。
3. codex-wsl compatibilityProfile 作为独立配置，不影响普通 cmd/WSL。
4. Light/Dark 全局主题和各面板同步；主题不改写第三方场景资产。
5. Setup 支持 Python、WSL 发行版、OpenFOAM bashrc、路径转换、VIBEFLOW_<TOOL> 注册和环境刷新。
6. CLI 发现支持显式路径、版本目录、.cmd/.bat、WSL Node 和预发布比较。
7. opencode 保持深度契约；其他 CLI 只按实际能力声明。深度 Codex 归 WP17。
8. CLI 容器孵化与 WP16 共用沙箱、出口代理和审计；无 docker socket 暴露给被控 agent。
9. 按技能建立真实工具链矩阵，未安装/未验证的技能明确降级。
10. 统一进程 supervisor、按 session 取消和清理；孤儿恢复接入持久状态。

**回退**：新 CLI adapter 可独立禁用；普通终端始终保留，不因容器失败影响本地开发回退。

### WP13：作业、索引、补全与事件总线

**主条目**：A12、H01–H19（H20 已在 WP01）。

**实现步骤**：

1. Job parsed_summary 自动写回、ended_at 从 DB 完整返回、孤儿 queued/running 恢复有真实重启测试。
2. 日志解析扩展 TimeStep/FATAL/WARN/多求解器和增量 fingerprint；残差图探针改硬断言。
3. fs watcher+去抖+轮询兜底；Agent、上传、caseDict、普通写删和作业结束统一发 indexing/file events。
4. S3 不具备本地 watcher 时采用 outbox+定期 reconciliation，不能假装有实时文件监听。
5. FTS building-UUID、跨进程锁、事务、WAL/checkpoint 和半成品清理；索引损坏可重建。
6. OCR receipt/hash/CacheStorage 维护与旧缓存清理。
7. 从 v2406 重建补全，保留来源/hash/confidence；补 C++14、宏、registrations、compoundKeys、FoamFile、环境分类和父键上下文值。
8. /ws/events 统一 files.changed、jobs.updated、runtime.degraded、scene.available、quota.warning；按用户/组织授权过滤。
9. tus/分块上传、流式配额、断点续传和 multipart Range；上传不先读入完整内存。
10. 资源硬限制、公平队列和多 worker 与 WP16 共用调度契约。

**验收**：外部修改到树/索引/events 的延迟可测；并发索引无半成品；补全数量不是唯一断言，来源和上下文正确；200 作业洪泛公平且无跨租户事件。

### WP14：Flotherm2foam Studio/CLI

**主条目**：I01–I10D。

**阶段 F0：规格与夹具**

1. 建立 Studio 与 CLI 能力矩阵，明确 Pack/force/keep-stage 差异。
2. 为 ECXML、V2310 PDML、组合输入、XTXML/XTXMLA 建立最小合法/非法夹具和 hash。
3. 定义 IR、coverage、case-manifest、required-overrides、power-balance 和日志 schema。

**阶段 F1：转换核心**

1. 输入识别、配对名称/bbox/唯一性审计和 blocker 预检。
2. 几何、材料、风扇/阻力/散热器/外壳、Monitor/Region、热源和边界映射。
3. settings.json、objectOverrides、refinement、parallelSubdomains 和单位/语义确认。
4. 输出 converted/equivalent/unsupported/missing-info 报告，禁止静默丢部件。

**阶段 F2：OpenFOAM 与辅助库**

1. 生成 case、Allrun.pre/serial/parallel/Allclean、0.orig 和网格门禁。
2. 辅助库检测、源码复制、wmake libso、FOAM_USER_LIBBIN 安装和版本冲突检查。
3. open/channel、laminar/realizableKE、各向同性/正交各向异性导热、区域温度/wallHeatFlux/流量/probes。
4. 转换期与求解后功率平衡分别验证，不互相替代。

**阶段 F3：Studio、取消与安全**

1. 异步转换、日志、打开输出、取消和部分产物保留。
2. case/IR 输出目录必须不同、不嵌套且为空；重转使用新目录。
3. --force 只清理带 .flotherm2foam-generated 标记的输出；Studio 禁止不支持的 force/keep-stage。
4. G6 对每个输入做逐文件 diff、语义检查、icoFoam 实际运行和取消/覆盖负向测试。

**回退**：每类输入/映射可独立开关；任何 blocker 都保留原输入和日志，不覆盖上一有效输出。

### WP15：组织、订阅、配额与安全策略

**主条目**：J01、J04–J11、J18、J19、J23、J25。

**实现步骤**：

1. 组织资源 ACL：workspace/case 增加组织归属和角色判定，迁移个人资源时保留原所有权语义。
2. seat/member/admin：邀请、禁用、角色、viewer 权限和 admin console；所有资源路由补负向测试。
3. 订阅生命周期：续费、升降级、到期、seat 计费；支付先抽象 provider 和 mock，不在测试产生真实扣款。
4. OIDC/OAuth 账户关联、首次登录、解绑和既有邮箱冲突处理。
5. 组织 provider profile 和 KMS 包裹；密钥轮换、权限和审计，不回显秘密。
6. 不可变审计中心：命令、审批、配置、组织、登录、导出和远程变更，支持查询/过滤/脱敏。
7. 配额覆盖 viz/job/LLM/storage 及几何/报告/文本写/Agent 写/翻译/压缩；expires_at 参与执法。
8. Redis token bucket 或等价限流覆盖用户×provider×model 和关键 REST/WS。
9. TLS、CSP、CORS、安全 header、短期窄 scope ticket、query token 日志脱敏和请求大小限制。
10. Python 精确锁、Docker digest、SBOM 和多 harness 配置备份。

**验收**：跨组织/角色/禁用/到期均有负向测试；viz 429 等四维及所有入口有实证；密钥/令牌不出现在日志；支付测试无真实外部扣款。

### WP16：存储、worker、沙箱、部署与运维

**主条目**：J12–J17、J20–J22、J26–J28。

**实现步骤**：

1. 备份/恢复演练：PostgreSQL、case 文件、.vault_key、配置；恢复后登录、解密、文件和权限可用。
2. Alembic 实库纳管只在 staging 备份和 schema_diff 审核后执行；补外键/唯一约束前清理并审计异常数据。
3. 统一 Storage API：文件、作业、索引、几何、报告、终端和 viz 不再默认读本地路径。
4. S3 执行采用受控 materialize/sync：输入快照、hash manifest、临时工作目录、结果回传和冲突检测；fs 开发路径保持。
5. S3 Range/HEAD/presign/下载做真实业务 smoke；分片和生命周期清理完整。
6. Redis/arq 作业队列、pub/sub 事件扇出、job attempts 和公平调度；API/worker 重启可恢复。
7. PTY/viz/session 持久化或明确的 sticky+重建策略；多副本不能重复执行工具/作业。
8. 所有执行入口进入硬沙箱：shell、Agent、CLI、几何、UDF、LaTeX、转换、后处理。
9. 沙箱非 root、只读 rootfs、cgroup、PID/内存/CPU、仅挂当前 case、默认无公网、无 Docker socket。
10. Linux PTY、OpenFOAM v2406、pandoc、migrations、EGL 和 vault 持久化进入镜像/部署。
11. TLS 网关、集中日志、指标、告警、健康/就绪探针和运行时 degraded 状态。
12. CI/CD、每日 replay、授权真实模型回归、SBOM、镜像扫描和发布签名。
13. 按目标执行 k6、容器逃逸、路径 fuzz、跨租户、SSRF、egress、50 用户和 200 作业洪泛。

**生产门槛**：完整备份恢复、升级回滚、真实 CAE/viz/WS、负载与安全测试全部通过；docker compose ps 不能作为验收。

**回滚**：蓝绿/金丝雀、additive migration、文件/S3 版本化、旧 worker  drain；回滚同时考虑应用、DB、文件和密钥一致性。

### WP17：后端、Codex、SCNET 与可选扩展

**主条目**：A15、C08、C09、C16、D17–D21、E11/E12、F13、F30、F41、F44、F47、F53、F55、F56、G07、G10–G12、I11–I21、J02、J03、J24。

| 子包 | 估算 | 入口条件与验收 |
|---|---:|---|
| C08 开源后端 | 每后端 20–45 人日 | 先确认安装与输入格式；每后端一个真实 golden、日志、产物和 adapter 测试 |
| C16 远程/HPC | 60–120 人日 | SSH/Slurm 测试账号、文件同步、状态/取消/回收、凭据隔离和断线恢复 |
| 深度 Codex | 80–160 人日 | 用户重新启用深度 harness；stdio、登录、resume/fork、复杂审批和容量重试用真实授权环境验收 |
| SCNET | 180–300 人日 | 先 3–5 人日确认原版启用和测试账号；不自动创建付费资源；逐域验收作业/文件/容器/Notebook/桌面 |
| G11 崩溃上报 | 15–30 人日 | 明确隐私、脱敏、同意和保留策略；禁止上传 case/密钥 |
| G12 语音输入 | 15–30 人日 | 浏览器权限、识别provider、离线/在线边界和准确率测试 |
| C09 商业后端 | 每后端 20–60 人日 | 组织自备 license/remote/HPC；不捆绑，不做授权绕过 |
| ⏸ 项 | 默认 0 | 仅在 ADR 重新启用后估算和实现 |

## 8. 数据、API 与兼容性设计

### 8.1 数据库变更原则

- 新表优先考虑 case_events、goals/checkpoints、audit_events、job_attempts 和需要跨副本的 session；先做最小 schema，不为未来猜测加表。
- 对 agent_events 增加 (conversation_id,seq) 防重前，先检查并清理异常数据；provider_profile_id/branch_head_event_id 外键需独立迁移评审。
- JSON Text → JSONB 不作为前置大重写；只有确有查询/索引需求时逐字段迁移并提供读写兼容。
- 所有迁移通过临时 SQLite、staging PostgreSQL、备份恢复和回滚评审。

### 8.2 API/事件兼容

- REST 保持 /api/v1 兼容；新事件带 schema/version，未知事件前端可忽略而非崩溃。
- SSE/WS 重连使用 cursor/event_id；多副本事件必须去重，不能只依赖内存顺序。
- 文件 artifact 使用 canonical path、size、hash、producer、source revision 和 created_at。
- 所有异步操作统一 queued/running/succeeded/failed/cancelled/interrupted、exit_code、ended_at 和错误结构。

### 8.3 前端状态

- openFiles、activeFile、case revision、job/viz/agent session 分别有明确 owner；禁止一个全局布尔 busy 表示所有任务。
- 大面板采用懒加载；新 UI 必须有键盘可用、错误态、空态和加载态。
- 浏览器不能等价执行桌面 OS 行为时，采用安全 Web 替代并记录差异，不开放服务器任意路径。

## 9. 验证体系与发布门槛

### 9.1 测试层级

| 层级 | 必测内容 |
|---|---|
| 单元 | parser/schema/状态机/安全策略/指标/格式转换 |
| API 集成 | 归属、配额、错误码、DB+文件一致、迁移、取消与重连 |
| 浏览器 E2E | Open Editors、Agent、CAD、ParaView、报告、设置；DOM+网络+实际产物 |
| CAE golden | G1 cavity、G2 pitzDaily、G3 damBreak、G4 几何、G5 UDF、G6 Flotherm |
| 安全 | 路径/符号链接、SSRF、XSS、命令注入、容器逃逸、跨租户、egress、秘密扫描 |
| 性能 | 大文件、长历史、大索引、50 用户、200 作业、viz 内存和交互延迟 |
| 运维 | 备份恢复、升级回滚、重启恢复、多副本、监控告警 |

### 9.2 黄金用例门槛

- G1/G2/G3：有参考来源时做数值对拍；否则明确为行为/回归验证。
- G4：对象、场景、STEP/STL 和参数来源正确，preview 无内联导出。
- G5：每种 UDF 都有 D1–D5 文档、构建、加载、执行和行为结果，不只 wmake 成功。
- G6：逐文件 diff+语义检查+实际求解+取消/覆盖安全，不能只比较文件名。
- agent replay 必须确定性；真实模型结果只作单独授权的非阻断或专门门禁，不与 mock 混写。

### 9.3 建议性能门槛

50 并发用户各运行 1 路 PTY、1 路可视化、1 路 Agent SSE，记录 REST p95，核对 SSE 首 token p95<2s、viz 交互 p95<200ms，以及 200 个积压作业时的配额公平性。2026-11-25 用户决定将原 REST p95<300ms 移出交付门槛。REST 仍核对业务成功率并报告实测耗时、运行环境和容量限制。 [实测与决策](FF-J21-rest-load.md)

## 10. Definition of Ready / Done

### Ready

- 审计 ID、原版证据、当前差距和验收方式明确。
- 依赖、数据迁移、权限、秘密、外部服务和回退已评审。
- 未知行为已有时间盒 spike，不靠猜测实现。
- 测试夹具不会污染真实用户 case 或生产服务。

### Done

- 代码、单元/API/浏览器/真实环境必要测试全部通过。
- 有负向测试和实际产物；文件存在不等于内容正确。
- 文档、OpenAPI、测试索引、progress、feature-gap 状态同步。
- 新依赖已入 requirements/lock，部署与回退说明完整。
- 无秘密、真实凭据、任意系统修改或未授权外部副作用。
- 未完成子项仍保留 🟡/🧪/⬜，不得为了里程碑整体改 ✅。

## 11. 回退与发布策略

1. 新 adapter、reader、求解器、Goal、S3、多副本均有 feature flag 或配置开关。
2. 文件写入保留原子策略和上一有效版本；导入、转换、报告不覆盖唯一输入。
3. DB migration 默认 additive； destructive 变更单独窗口和备份。
4. 先 staging，再小范围用户，再全量；真实 CAE、WS、恢复和安全门槛不通过则不发布。
5. 回滚包括应用版本、worker、DB、文件和密钥，不允许只回滚前端后声称恢复。
6. 每次发布生成证据包：测试日志、截图、黄金结果、性能、安全、迁移和已知缺口。

## 12. 风险登记与触发动作

| 风险 | 早期信号 | 触发动作 |
|---|---|---|
| ParaView 全对等失控 | EGL/XML/内存 spike 失败 | 两周内降级 iframe/轻量路线，重估范围，不写假完成 |
| Flotherm 映射不完整 | coverage 缺项或功率不平衡 | blocker 停止输出“可求解”，补映射与 golden，不静默忽略 |
| 数值参考缺失 | 找不到原版/公开参考 | 改为回归基线并标未知，另建参考采集任务 |
| Agent/CLI 命令逃逸 | 可访问其他 case/宿主机/公网 | 立即停止扩展执行能力，修复沙箱和 egress 后重测 |
| Goal 无限续轮 | 预算、取消或 checkpoint 失效 | 默认关闭 Goal，恢复手动模式，审计已产生副作用 |
| S3 与本地不一致 | 作业读不到输入或结果丢失 | 回退 fs，冻结 S3 生产配置，完成 materialize/sync 验证 |
| 长历史/图片成本失控 | token、内存、provider 费用异常 | 有界上下文、附件配额、速率限制和预算告警 |
| 多副本重复执行 | 工具/Job 重复、事件乱序 | 回退单 worker，加入幂等键/leader/锁后重验 |
| 范围持续扩大 | 新需求进入当前迭代 | 进入 WP17/后续 backlog，不挤占当前发布门槛 |
| 排期失真 | spike 超估 35% | 重新估算并调整并行/版本，不通过减少测试压缩工期 |

## 13. 前六个双周迭代

| 迭代 | 目标 | 主要交付 | 出门条件 |
|---|---|---|---|
| I0（第1–2周） | WP00+WP01 热修 | 内联导出、Jobs 安全、v2406、存储保护、0/U、viz 429 | 当前+新增测试全绿，审计状态更新 |
| I1（第3–4周） | 外部 case+Open Editors | 安全导入、标签/保存/关闭、外部修改 E2E | 外部 golden 可运行，恶意导入被拒绝 |
| I2（第5–6周） | caseDict 16 块+events+watcher | schema v1、case_events、文件/索引事件 | 16 块与迁移/门控/事件闭环 |
| I3（第7–8周） | 几何/网格基础+Agent 安全设计 | 参数注入、网格质量、web_fetch SSRF 测试先行 | 几何参数真实生效，SSRF 负向通过 |
| I4（第9–10周） | Agent runtime/cancel/checkpoint+MPI spike | WSL/沙箱 runtime、停止恢复、2/4 子域验证 | 不重复执行、进程组取消、串并行比较 |
| I5（第11–12周） | Goal/长历史+后处理/通用 reader | Goal 有界续轮、指标 v1、VTK/VTU reader | 超预算停止、指标重算、reader 实物通过 |

I6 起按依赖选择 UDF 扩展、ParaView spike、Flotherm 核心或生产沙箱；不得同时开启超过团队并行容量的 HIGH 风险工作包。

## 14. 295 项审计 ID → 主工作包映射

以下映射已用脚本校验：**295 个审计 ID 全部出现一次，MISSING=[]、EXTRA=[]、DUPLICATES={}**。WP00 为横切工作，不重复占有 ID。

| WP | 审计 ID |
|---|---|
| WP01 | B21、G13、H20、H21、J29 |
| WP02 | A01、A02、A03、A04、A11、A13、A19、A21、A23、A25、A26 |
| WP03 | A05、A06、A07、A08、A09、A14、A16、A17、A18、A20、A22、A24、CB01–CB16 |
| WP04 | B01、B02、B03、B04、B05、B15、B16、B17、B18、B19、B23 |
| WP05 | A10、C01、C02、C03、C04、C05、C06A、C06B、C07、C10、C11、C12、C13、C15、C17、C18、C19 |
| WP06 | C14A、C14B、D11、D12、D14、D23、D24 |
| WP07 | D01、D02、D03、D04、D05、D06、D07、D08、D09、D10、D13、D15、D16、D22、D25、D26 |
| WP08 | B06、B07、B08、B09、B10、B11、B12、B13、B14、B20、B22 |
| WP09 | E01–E10、E13–E22、F65 |
| WP10 | F01、F02、F03、F04、F05、F06A、F06B、F07、F08A、F08B、F09、F10、F11、F12、F14、F15、F16、F17、F18、F29、F33、F34、F67 |
| WP11 | F19–F28、F31、F32、F35、F36、F37A、F37B、F38、F39、F40、F42、F43、F45、F46、F48、F49、F50、F51、F60–F62、F66 |
| WP12 | F52、F54、F57、F58、F59、F63、F64、G01、G02、G03、G04、G05、G06、G08、G09、G14 |
| WP13 | A12、H01、H02A、H02B、H03、H04、H05、H06、H07、H08、H09、H09A、H10、H11、H12、H13、H14、H15、H16、H17、H18、H19 |
| WP14 | I01、I02、I03、I04、I05、I06、I07、I08、I09、I10、I10A、I10B、I10C、I10D |
| WP15 | J01、J04、J05、J06、J07、J08、J09、J10、J11、J18、J19、J23、J25 |
| WP16 | J12、J13、J14、J15、J16、J17、J20、J21、J22、J26、J27、J28 |
| WP17 | A15、C08、C09、C16、D17–D21、E11、E12、F13、F30、F41、F44、F47、F53、F55、F56、G07、G10、G11、G12、I11–I21、J02、J03、J24 |

K01–K12 是决策/替代项，不是额外功能实现条目；按 [全功能交付计划](full-function-delivery-plan.md) 第 2 节逐项复核和承接。

## 15. 文档与状态维护

每个迭代结束必须同步：

- [progress.md](progress.md)：实际修改、命令、通过/失败/SKIP、未执行项和下一步。
- [feature-gap-audit.md](feature-gap-audit.md)：只按证据改变状态，重跑 ID/计数校验。
- [parity-checklist.md](parity-checklist.md)：更新领域摘要，不用主观百分比。
- [testing.md](testing.md)：新增套件、前置、外部服务和副作用。
- [HANDOVER.md](HANDOVER.md)：架构/契约/恢复变化。
- [security.md](security.md) 与 [deployment.md](deployment.md)：新执行入口、秘密、存储、沙箱和上线状态。
- 本计划：记录估算偏差、ADR 和范围调整，不删除历史未完成项。

N0五项及N1-01/02已验收，见 [mainline-task-status.json](mainline-task-status.json) 和 [持久模型说明](workflow-persistence.md)。下一实施单元为N1-04原子claim/幂等/尝试编号、JobAttempt实际执行桥接，再完成依赖/确认调度与剩余outbox消费者。后续按并发、执行/取消、恢复和UI依赖推进，WP01保留回归；其余功能按R0–R7推进。
