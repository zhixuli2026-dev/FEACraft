2026-11-18 工程报告模板：caseDict referenceDoc/CSS接入自动正文与资源封存；真实FS工程5项、S3/独立worker2项、HTML实际渲染1项、默认无模板闭环1组通过，18个原生作业停止核验。模板变更与历史原件、手写稿/备份保持。模型0、模拟LLM0。FF-E02按原条件验收，全功能2/295剩293；主线49/53剩4、资源11/17剩6。 见[工程模板](report-engineering-templates.md)及[逐项核对](report-delivery-reconciliation.md)。下文原审计状态保留历史基线。

2026-11-17 全功能交付核对：FF-E22已按原条件验收，独立交付台账1/295；下文原审计状态为历史能力基线，不作整项交付统计。见 [阅读器验收](document-reader-acceptance.md)。

# VibeFlow 原版 vs 当前 Web：逐项功能差距审计

> 审计日期：2026-10-24；2026-10-25 全面复核当前实现、补入外部 case/几何/CAD/Agent 漏项并更新 UDF/下载/门控等证据；2026-10-26 复核自动预览、MPI、网格质量、文件事件和 WP06 后处理；2026-10-27 验证 cadscene 实时协议与 CAD 浏览器闭环。目标：按功能项逐项对照，不用单一百分比概括。
> 覆盖范围：原版 0.11.0 的文档、资源、StandardCase、技能与当前平台源码/测试/运行时。静态材料不能证明的交互行为标为未知，不把文件名直接当功能已实现。

> **最新范围（2026-11-02）**：用户要求完整实现所有功能。以下符号继续记录现有证据，不因写计划而改成已完成；原 14 个 ⏸ 项已在 [全功能交付计划](full-function-delivery-plan.md) 重新登记补证/实施或新增扩展，不再自动从最终范围删除。任务台账覆盖全部 295 个 ID；近期执行见 [下一阶段计划](next-stage-mainline-plan.md)。

## 判定口径

- ✅ **已实现且有当前验证证据**：代码路径和对应测试/运行结果均存在。
- 🟡 **部分实现/轻量替代**：主路径可用，但原版的菜单、协议、深度或覆盖范围尚未对齐。
- 🧪 **已有实现但未充分验证**：能定位实现，缺少当前测试、真实用例或目标环境验证。
- ⬜ **未实现**：当前没有对应功能实现。
- ⏸ **历史决策/缩减记录**：保留过去的暂停或替代事实；最新全功能目标下逐项恢复范围复核，实现状态未因计划变更而提升。
- ❓ **证据不足**：现有材料不足以确认原版语义或当前状态，后续需要运行时/二进制/人工复核。

## 总结（先看这里）

本台账包含 **295 个功能项，另有 12 个决策/替代项**：

| 状态 | 数量 | 含义 |
|---|---:|---|
| ✅ 已验证实现 | 159 | 有当前代码和对应测试/运行证据，但不等于整个模块完全对齐 |
| 🟡 部分实现/轻量替代 | 118 | 主路径或部分子项可用，语义、覆盖或深度仍不足 |
| 🧪 有代码但未充分验证 | 2 | 有实现，缺当前测试、真实用例或目标环境验证 |
| ⬜ 未实现 | 0 | 没有当前实现，或只有技能文本/空脚手架/规划 |
| ⏸ 历史缩减/暂缓 | 14 | 已重新登记范围复核/实施或新增扩展，保留历史证据状态 |
| ❓ 证据不足 | 2 | 原版启用状态或具体语义无法确认 |

**总体判断**：当前是可继续开发的本机 Web 工作台，不是原版全功能对等实现，也不是团队生产系统。最大缺口集中在：通用 UDF 阶段工作流与更多 UDF 类型、MPI/多后端、Flotherm、PPT、通用 Agent web 工具与 Goal、完整 caseDict schema/门控、server-manager/ParaView 长尾、cadscene 重连/版本与多副本、S3/硬沙箱/多副本生产部署。

**⬜ 完全未实现的领域分布**：A 0 项、B 0 项、C 0 项、D 0 项、E 0 项、F 0 项、G 0 项、H 0 项、I 0 项、J 0 项，共 0 项。I 类含可选 SCNET，J 类含 B/S 生产化目标，均不能混算成桌面原版缺失率。

## 运行时基线（历史调查记录）

以下是早期调查与专项证据汇总；最新分组验收、报告快照和运行限制以 `progress.md` 顶部为准。逐项旧备注与新切片冲突时，按当前源码和具体证据复核，避免重复开发或把历史限制直接当作已解除。

- 当前测试集合 165 个后端/专项套件；2026-10-30 完整 **152/154（两个失败修正后分别通过，最终修复项4/4）** 通过、退出码 0；随后新增 C12/J05/J18/J11/J19/J20/J06/D15/J10/J21/I10A 专项均在主 PostgreSQL/真实 HTTP 复验通过，主端口浏览器专项也通过**。Crossref web_search/web_fetch 均 LIVE_OK；Wikipedia、example.com 与 Semantic Scholar 公共 API（503）本轮 LIVE_LIMITED。实库已备份并临时恢复校验后 `stamp head=6fb6c18fb7ee`，未在实库 downgrade；viz/job/LLM/storage 四类配额均有 429 与还原证据。
- 技能注册表在线 41 个，但技能文本不等于求解器安装或真实任务验证。
- WSL `vibeflow-linux`：系统仍保留 1912，另已通过官方 APT 源安装 **OpenFOAM v2406 开发环境**；显式 source v2406 后 `wmake`、`foamEtcFile` 和 icoFoam 可用。`mpirun` 的 2 子域分解/求解/重构链已验证；`snappyHexMesh` 存在但未形成流程验证，gmsh、cfMesh 当前均未找到。
- 真实验证已有 icoFoam 2D/3D、最小 functionObject 编译→加载→`functions` 执行、CadQuery、VTK/trame、LaTeX、Agent 双协议、双用户归属、caseDict 统一写入和单段 Range/206。
- Docker 引擎曾恢复运行，但目标机镜像构建和容器业务验收未完成，当前状态见 `docker-repair.md`。

## 逐领域功能台账

以下各节由“原版证据 → 当前证据 → 状态 → 剩余差距”逐项组成。最后增加遗漏复核清单，避免只看此前记分卡。

### A. 项目、文件、caseDict 与工作流

原版证据：`share/doc/vibeflow-studio/docs/help.md`、`agent-paths-sessions.md`、`build.md`，以及 `StandardCase/0-caseDict/caseDict`。原版模板字段是 schema 契约，不自动等于每项都有专用 GUI。

| ID | 原版功能/子项 | 状态 | 当前实现与剩余差距 |
|---|---|---|---|
| A01 | 创建/选择平台算例与 StandardCase 脚手架 | ✅ | 注册、工作区、算例创建、平台内选择和脚手架均有 smoke；创建即写目录和初始 caseDict。打开外部既有目录另列 A25。 |
| A02 | Project 文件树与 Open Editors | ✅ | Open Editors 文件标签、切换/关闭、全部保存/关闭、存储/相对路径复制和 `@path` 加入聊天均通过主浏览器 E2E；专用文件路由已覆盖。桌面 Show in File Explorer 采用复制存储/相对路径的 Web 安全替代，不开放服务器目录。 |
| A03 | 按扩展名路由到内部页/可视化 | ✅ | 文本/Markdown/图片/PDF/CSV/HTML/DOCX、`.foam`、VTK 家族均按专用路径打开；STEP/STP 只读 importStep 进 CAD，`.msh` 经 meshio→VTU 进 viz。source hash、scene 不变、归属/上限、真实文件树 STEP 与 Gmsh 网格渲染均通过。 |
| A04 | 求解日志、残差图、报告与后处理产物 | ✅ | 两种时刻/Courant/多求解器日志、结构化摘要、增量游标和残差双序列/坐标切换/PNG 均有验证；报告/LaTeX/证据门禁和版本化产物完整；VTK 家族、STEP 与 Gmsh 均可直接预览。 |
| A05 | caseDict 唯一参数源与 A/B/C/D/E/V 状态 | ✅ | **后端**文件事实源、状态读写、DB 镜像、REST/Agent 统一写入及变体保护已验证；Monaco 保存和 errors 硬门控分别见 A19/A20，字段齐备/网格质量等完整门控由其他项单独跟踪。 |
| A06 | caseMeta 富 schema（学科、分析类型、维度、执行方式等） | ✅ | 枚举/status 映射之外，新增 analysisType↔timeBehavior、可选字段真实类型、未知量纲、NaN/Inf 和整数 lint；支持 length/mass/time/temperature/angle/force/pressure/energy 的确定性换算 API，非法输入/跨用户和 revision 不变负例通过。 |
| A07 | materials/regions/interfaces/boundarySets/initialConditions/loadCases | 🟡 | parser 可保存嵌套结构；constant/table/harmonic/ramp/pulse 标量 profile 有生成器、有限参数/重复/时长校验和真实 foamDictionary 解析。接触/周期/overset、moving/porous 区域及 thermal/mechanical/FSI/EM/acoustic 耦合仍未系统覆盖。 |
| A08 | provenance、assumptions、decisionLog、errors.entries/extensions | ✅ | errors.entries 支持 investigating/fixProposed/fixed/verified/open 与字段约束、原子 422、summary 自动重算；CasePanel 修复/验证/重开及重载通过。每次修订自动写 intent/决策 provenance 哈希链和篡改检测。extensions 独立稳定 schema 由 CB16 跟踪。 |
| A09 | StandardCase 占位目录与脚本文件 | ✅ | `0-caseDict/A-geo/D-post/E-report/UDF/Ref` 等脚手架已生成；占位文件不是内置算法，不据此增加完成度。 |
| A10 | UDF 开发目录、README 与 D1 需求→D2 设计→D3 实现→D4 编译→D5 集成 | 🟡 | case 内可生成 D1–D3 文档/源码且不覆盖，CasePanel 执行 D4 wmake+CDLL 与 D5 controlDict 注册/icoFoam；真实 P0_UDF_OK、新时刻 U 和 UI 阶段状态均通过。更多 UDF 类型和完整 G5 矩阵仍待做。 |
| A11 | 编辑器外部修改冲突保护 | ✅ | 保存前比较 mtime 的代码已有确定性 E2E：取消冲突保存保留外部内容，确认后覆盖编辑器内容；不是实时 watcher 或自动合并。 |
| A12 | 项目文件变更监听与树增量刷新 | 🟡 | `/ws/events` 以每 case 2 秒快照产生 created/modified/deleted 的 `files.changed`，联动 FTS 并触发 TreePanel `loadTree()`；无变化时 disconnect 立即退出，快照/索引移入线程，事件 smoke 和主浏览器 E2E 均通过。当前是单进程轮询+全量树刷新，不是 Redis/多副本总线或增量树 patch。 |
| A13 | 全局路径文本格式设置 | ✅ | 设置页支持 Windows 反斜杠与 Linux/POSIX 正斜杠，复制存储/相对路径按设置转换，canonical storage key 和 `@path` 仍用 POSIX；Node 测试和浏览器切换/恢复均通过。 |
| A14 | caseMeta.status 与 workflowState 的完整映射/门控 | ✅ | 2026-11-02 普通 REST/Agent 保存禁止改阶段，专用 advance 强制确认/revision/不跳级并同步已有 status；B 检查当前几何，C 检查 Mesh OK+网格/日志，D 检查真实 solve 输入/场/日志，E 检查本算例成功后处理和报告门禁。三条真实 A→E 闭环、隔离负例和浏览器反馈通过；旧 exit 0 不再算求解完成。 |
| A15 | `.macros/` 宏脚本约定 | ⏸ | 原版只有 ParaView 原生宏菜单，没有自研宏管理器；当前不把“缺宏管理器”算作缺口，但 ParaView 原生宏菜单仍另列。 |
| A16 | 失败回退与溯源（缺参/几何/网格/求解/报告回相应阶段） | ✅ | Job.type 映射 A/B/C/D/E/V，失败仅在目标早于当前状态时回退，同阶段/更晚目标只记录证据；统一 caseDict 写入和 revision/DB 镜像，workflow.fallback 记录 job/type/exit/log/target/regressed。真实 solve D→C、open errors 内退、convert 不推进均通过。 |
| A17 | geometry sourceType/topology/scale/symmetry/periodic/watertight/manifold 与 validation artifacts | ✅ | preview/export 生成 validation.json；sew/fix/mirror/periodic/scale 均有真实修复和 STEP+validation 产物。支持均匀缩放与 GTrsf XYZ 非均匀缩放、覆盖保护、非对称镜像和边界拓扑验证；复杂/大型模型性能边界另行跟踪。 |
| A18 | intent.targetOutputs/acceptanceCriteria/assumptions/limitations/openQuestions/decisionLog | ✅ | 每次 caseDict 修订自动写 provenance.jsonl，保存语义化 intent/假设/决策、revision/state/source/caseDict hash 和前序事件 hash；读取端点校验链完整性并分页。两次修订、开放问题变化、跨用户 404 和中途篡改检测通过。 |
| A19 | Monaco 整文 caseDict 保存 | ✅ | 保存已改走专用 `/caseDict`，其他文件仍走 `/file`；保存后刷新 case 镜像。浏览器验证 marker、revision 和原文恢复均通过。 |
| A20 | errors.summary.open/blocking 阶段推进硬门控 | ✅ | 统一写入在 workflowState 真正变化时检查 `errors.summary.open/blocking`，存在即 409 且文件/revision 不变，结清后可推进；字段齐备度和网格质量门控由 A14/B16 等另列，不纳入本项完成声明。 |
| A21 | 普通二进制下载按钮 | ✅ | 已改为 Bearer fetch→Blob→objectURL，带文件名并在下载后释放；浏览器实测 DOCX 下载成功，未授权请求仍由 raw 鉴权拒绝。 |
| A22 | case 状态变更审计 / case_events | ✅ | `0-caseDict/case-events.jsonl` 记录 case.created 与 caseDict.updated，含 schema/event_id/created_at/source/revision/previous_state/workflow_state；创建和 REST/Agent 统一写入共用，非法写入不新增。只读 API 最新优先、offset/limit/has_more 和跨用户 404；单进程锁保护追加，未新增 DB 表。 |
| A23 | workspace/case 重命名、删除、移动、复制 | ✅ | 既有管理之外，新增 fs 回收站与恢复：文件移动而 DB 行/revision/历史保留，普通 API 对删除项 404；活动 Job/Agent/Goal/viz/终端和未收尾执行器 409。真实 API/UI 删除恢复、跨用户、文件与历史保留均通过；永久清除、S3 与多副本回收站另行限制。 |
| A24 | 块级编辑的注释与格式保真 | ✅ | `set_block` 已改为 token 级局部替换，保留块内注释、其他键和 workflowState；全量删键、局部改键、缺失块/键报错均通过。 |
| A25 | 打开/导入外部既有 StandardCase | ✅ | 安全 ZIP 导入与 TreePanel UI 均通过：单根目录解包、caseDict 镜像、二进制、重复/跨用户/恶意 ZIP 拒绝；新 case 实际运行 v2406 生成 0.1/U，浏览器文件输入导入后自动打开并显示 marker。 |
| A26 | `@path` 行列范围引用 | ✅ | `pathRefs.ts` 支持行、单行和行列范围，格式/越界拒绝且不回退整文件；Node 边界测试、正式 build 和浏览器实际发送均通过，消息只含选中行。 |

#### A-1. caseDict 原始 16 块逐块对照

原版 16 块来自 `StandardCase/0-caseDict/caseDict`。当前脚手架已生成全部 16 块，新建 case 与既有 case 不覆盖均通过 smoke。**通用 dict 可保存仍不等于这 16 块的 schema 和语义都已对齐。**

| ID | 原始块 | 状态 | 当前脚手架、读写、专用语义与验证差距 |
|---|---|---|---|
| CB01 | caseMeta | 🟡 | 有模板和 v1 枚举校验：status/discipline/analysisType/dimensions/fidelity/executionProfile/units；完整状态/学科/分析/维度/执行方式类型、跨字段约束和 lint 仍不足。 |
| CB02 | intent | 🟡 | 有模板；targetOutputs/acceptanceCriteria/openQuestions/decisionLog 没有专用验收追踪。 |
| CB03 | physicalProblem | 🟡 | problemClass/timeBehavior 已纳入 v1 枚举校验；坐标/重力/参考量/耦合控制模型没有逐类后端映射和验证。 |
| CB04 | geometry | 🟡 | 有模板和 CadQuery；topology、对称/周期、水密/流形、validation artifacts 未闭环。 |
| CB05 | materials | 🟡 | 有模板；命名材料、相/热物性/弹性/电物性、来源和 confirmed 校验未实现。 |
| CB06 | regions | 🟡 | 新建 case 已生成 regions 块；区域材料/网格/物理关联、moving/porous 等执行语义仍未实现。 |
| CB07 | interfaces | 🟡 | 新建 case 已生成 interfaces 块；contact/coupledWall/periodic/cyclic/overset 和热/机械/FSI/EM/声学耦合未实现。 |
| CB08 | boundariesLoadsInitials | 🟡 | 有模板；边界角色、物理类型、时间 profile、restart/mapped 和确认状态未完整实现。 |
| CB09 | mesh | 🟡 | 有模板和 blockMesh/checkMesh；质量目标、单元族、边界层、adaptivity、convertedMesh/报告未完整实现。 |
| CB10 | numericalMethod | 🟡 | 有模板；线性化/稳定化/求解器/预条件/收敛/时间控制/并行分解没有通用后端映射。 |
| CB11 | execution | 🟡 | 有模板和本机作业；serial/parallel/batch/interactive/remote、restart、pre/post steps、监控和 artifacts 未闭环。 |
| CB12 | postProcessing | 🟡 | dataExtraction 六类操作、AST 公式、referenceValues/同网格参考场 L2、基础 uncertainty、comparisonTargets 类型链、快照及版本化产物已验证；自动实验/前次运行数据采集、异网格/实验 validation、数组公式/高级不确定度传播、多序列图和报告章节自动组织仍未实现。 |
| CB13 | report | 🟡 | 已有 caseDict 自动 9 章、证据门禁、pandoc/LaTeX 和构建快照；完整章节 schema、各分析类型模板、自动插图与完整资源依赖仍需补齐。 |
| CB14 | provenance | 🟡 | 新建 case 已生成 provenance 及 createdBy/At、updatedAt、sources、assumptions、decisions、artifactTrace 结构；自动写回、来源/决策/artifact 追踪和生命周期仍未实现。 |
| CB15 | errors | 🟡 | entries 的 stage/category/severity/status/blocking、summary.total/open/blocking/resolved/lastErrorId、fixOptions/resolution/verification 已验证；summary 与 entries 一致性和结清证据门控通过。完整错误编辑/结清 UI 和 provenance 自动追踪未实现。 |
| CB16 | extensions | ✅ | 原空块兼容，新增 extensions v1：schemaVersion/entries、命名空间、enabled/version/settings 校验；PUT/DELETE 生命周期、原子 422、跨用户 404 和 provenance source=extensions 均通过。 |

**A 类主要缺口**：专用文件路由、Open Editors/编辑器完整操作组、富 schema 校验、文件 watcher、通用 UDF 阶段工作流与更多类型。

### B. 几何、CAD 与网格

原版证据：`share/vibeflow/python/vibeflow_geo/runner.py`、`resources/cadviewer/index.html`、three-cad-viewer 4.3.7，以及 caseDict mesh schema。

| ID | 原版功能/子项 | 状态 | 当前实现与剩余差距 |
|---|---|---|---|
| B01 | CadQuery build/export 脚本钩子与对象返回 | ✅ | build_geometry/build(ctx)、result/objects 回退、顶层自动探测、ctx.stage/show、dict/list/tuple/单对象均经真实 preview 验证；export/manifest 和 preview 内联导出抑制通过。 |
| B02 | CASE_ROOT/脚本路径等几何上下文注入 | ✅ | 按原版 runner 补齐 `__file__`、Path 类型 CASE_ROOT/A_GEO_DIR/case_root、VIBEFLOW_CASE_ROOT/GEOMETRY_SCRIPT、show_object/show/debug；真实 preview 验证路径、环境、mode/tolerance、options 颜色/opacity 和消息，完整 geometry_smoke 通过。 |
| B03 | preview 曲面离散、OCP payload、失败回退与 tolerance | 🟡 | preview 和 scene.json 已验证；tolerance 选项、OCP 失败后的完整回退语义未形成同等的可见配置/测试。 |
| B04 | 逐对象 STEP/STL 与 geometry_manifest | ✅ | 几何 smoke 已验证 STEP 头、STL 和 manifest。 |
| B05 | 保存 geometry.py 自动预览 | ✅ | 保存触发作业后 GeoPanel 自动等待并刷新 scene；成功出现新对象，失败显示错误并保留上一有效场景，主 E2E 与聚焦探针均通过。 |
| B06 | cadscene WebSocket：scene.replace/scene.clear | ✅ | `/ws/cadscene` 校验 JWT/case 归属并写入 `A-geo/scene.json`；replace/clear 广播、GeoPanel 对象树刷新、非法面索引拒绝和单客户端背压超时均经 smoke/E2E 验证。重连快照、版本号/乱序丢弃和多副本扇出仍未实现。 |
| B07 | Base64 场景载入、Fit、边线、透明、主题接口 | ✅ | `vfLoadSceneFromBase64/vfFit/vfSetEdges/vfSetTransparent/setVibeFlowTheme` 已接入 GeoPanel 和 UI；Base64 UTF-8、扁平/嵌套数据、FOV/aspect Fit、偏好跨重载、非法输入保留旧场景和事实源不变均经纯函数/浏览器验证。完整 OCP meshData 与整个应用主题系统不在本项宣称范围。 |
| B08 | CAD 树、对象选择、折叠、可见性及状态保留 | 🟡 | 已实现 CAD 对象树、折叠、选择高亮、可见性切换和 scene 重载后可见状态保留，浏览器验证通过；不是完整 CAD 文档树、属性模型或复杂选择集。 |
| B09 | 相机预设、坐标网格、轨道、灯光/材质/环境 | 🟡 | Orbit/Trackball、+X/+Y/+Z/等轴测、透视/正交 FOV-aspect Fit、四宫格 scissor 多视图、RoomEnvironment、ambient/direct/metalness/roughness、shadow map 和单视图 SSAO 均经 `e2e-cad-view.mjs` 验证；studio env rotation/tone mapping/exposure/AO intensity/4k maps/texture mapping、完整多视图控制和更多材质选项未实现。 |
| B10 | Distance/Properties 测量与 bbox 属性 | ✅ | 已实现选中对象 bbox、尺寸、中心、顶点/面数和两对象 bbox 中心距离，双对象距离 5.000 已验证；与原版一致为 bbox center 语义，不是 B-rep 精确测量。 |
| B11 | 三轴剖切、平面助手与端盖 | ✅ | GeoPanel 对源三角面做三轴 CPU 剖切，交点焊接闭环并用 ShapeUtils holes 生成真实端盖；面积/法线/全保留/全丢弃、带孔平板截图和源 geometry 恢复均通过。当前是三角网格实现，非流形/超大模型和 B-rep 精确布尔运算不在本项宣称范围。 |
| B12 | 爆炸视图及动画切换 | ✅ | 爆炸按对象中心偏移，新增真实时间插值和可关闭的动画过渡；测试观察中间进度、关闭过渡立即到位，普通动画启动时爆炸进度立即归零并恢复源位置，完整 CAD 回归通过。没有独立的时长/缓动编辑器。 |
| B13 | CAD 关键帧动画（平移/旋转/重复） | ✅ | 新增 `vibeflow.cadquery.animation.v1` 和 JSON 轨道编辑器，支持 t/tx/ty/tz/q/rx/ry/rz、selector、严格递增关键帧、线性/四元数插值、播放/暂停/停止、时间 scrub、导出和非法输入保留场景；浏览器验证真实 transform。没有完整 ParaView 全类型动画/属性轨道系统。 |
| B14 | CAD 截图回传 | ✅ | GeoPanel 可将当前 Three.js 视图导出为 PNG data URL，浏览器验证文件名和图像内容；`shots/wp08-cad-focused.png` 已人工核对。 |
| B15 | 网格来源、质量预设、单元类型、边界层与自适应 | 🟡 | 可通过脚本/字典表达部分参数，但没有 generated/imported/solverNative/external 的完整工作流，也没有 smoke/coarse/production/meshIndependence 预设和全类型支持。 |
| B16 | skewness/nonOrtho/Jacobian/aspectRatio 等质量门禁 | ✅ | checkMesh 指标解析为 `B-mesh/quality.json`，含 mesh hash；A/B→C 的 fail/warn/allowWithReason、理由、缺报告/指标和参数变脏均验证，C→B 不被误拦截。 |
| B17 | blockMesh/snappy/gmsh/cfMesh 工具链 | 🟡 | blockMesh 已实证；snappyHexMesh 存在但未验证；gmsh/cfMesh 当前不可用。 |
| B18 | 网格产物、convertedMesh/qualityReport/boundaryReport | 🟡 | 目录和日志存在，结构化转换报告、质量/边界报告及与 caseDict artifacts 的联动未实现。 |
| B19 | preview 不自动调用 `export_geometry` 钩子 | ✅ | `export_geometry` 已移到仅 export 分支；preview 不再产生该钩子写出的导出标记，显式 export 仍调用钩子并生成 STEP/STL/manifest。脚本内联导出抑制是另一条契约，见 B21。 |
| B20 | GeoPanel STEP/STL 一键导出按钮 | ✅ | GeoPanel 新增真实 `geometry:export` 作业按钮，完成后刷新文件树并显示 A-geo/exports；浏览器验证 STEP 和 STL 均生成。 |
| B21 | preview 抑制脚本内联 `cq.exporters.export` | ✅ | preview 在 exec 与 build_geometry 调用期间临时禁用内联导出并 finally 恢复；geometry_smoke 验证 preview 不产生内联 STEP，显式 export 保留内联 STEP，B19 钩子和场景回归通过。 |
| B22 | CAD Zebra 条纹分析与运行时配置 | ✅ | 按原版实现 blackwhite/colorful/grayscale、reflection/normal、角度方向、count/opacity、奇数条纹半宽和色相连续；shader 真实像素变化、opacity=0、原材质恢复和 colorful 截图均通过。没有自定义配色编辑器或完整 studio texture mapping。 |
| B23 | caseDict 几何参数与 `ctx.data/params/domain` 注入 | ✅ | geo_script 读取 caseDict 并注入完整 data、geometry.parameters/domain 和 mode；脚本用 width/depth/scale 实际生成 105×75×6 模型，preview/export 均通过。 |

**B 类主要缺口**：CAD 实时协议和大多数高级交互、剖切/爆炸/Zebra/CAD 动画、网格来源/类型/边界层及 gmsh/cfMesh。

### C. 求解器、UDF 与多工况

原版证据：caseDict 的 physicalProblem/numericalMethod/execution schema、OpenFOAM v2406 补全资产及迁移辅助审计。schema 声明的后端数量不等于原版已验证数量。

| ID | 原版功能/子项 | 状态 | 当前实现与剩余差距 |
|---|---|---|---|
| C01 | fluid/solid/thermal/electromagnetic/acoustic/coupledPhysics 问题描述 | 🟡 | caseDict 可存这些描述；跨学科后端输入生成和逐学科验证没有实现。 |
| C02 | 数值方法、时间控制、线性/非线性求解与自适应步长 | 🟡 | 通用字典可存，未形成各后端的方法映射、参数校验和自动生成。 |
| C03 | serial/parallel/batch/interactive/remote 执行模式 | 🟡 | 当前只有本机 Windows/WSL 作业；remote/HPC/SSH/Slurm 和 interactive 的完整执行配置未实现。 |
| C04 | OpenFOAM v2406 开发环境与真实求解验证 | ✅ | 已通过官方 APT 源安装 v2406；显式 source 后 `wmake`、`foamEtcFile` 和 icoFoam 可用，2D/3D golden 与最小 UDF 在新环境通过。runtime probe 与通用 WSL 作业已统一 --exec/v2406；部分黄金脚本仍固定 1912，统一迁移在 WP05 继续。 |
| C05 | icoFoam 2D/3D 基础求解 | ✅ | 2D 100 步和 3D 27000 单元均通过 blockMesh/checkMesh/icoFoam、时刻产物及残差解析。 |
| C06A | 手写 decomposePar/mpirun/reconstructPar 命令 | ✅ | 2 子域真实执行、processor 结果和重构时刻已验证；串并行 U 场相对差异 1.14e-08。 |
| C06B | MPI 参数生成、自动分解/并行/重构编排与验证 | 🟡 | `mpi:run` 生成缺失 decomposeParDict、执行 2 子域并回传结果；`mpi:status` 检查 processor 和重构时刻。nobody 临时目录执行，串并行差异通过。进程组取消、force/旧 processor 清理和集群调度未实现。 |
| C07 | 更多 OpenFOAM 求解器与物理模型 | 🟡 | 通用命令可调用，snappyHexMesh 存在；没有覆盖多相流、传热、湍流、可压缩等系统实测矩阵。 |
| C08 | CalculiX/Code_Aster/Elmer/SU2/MOOSE 等开源后端 | 🟡 | CalculiX 2.23 官方 ccx 与 SU2 8.5.0 官方 Windows OpenMP 已接入，均有显式路径/fail closed、输入限制、真实 solve 作业和 golden；CalculiX 验证 C3D8/DAT/FRD/STA/非零位移，SU2 用官方 QuickStart NACA0012 验证 restart_flow.dat、flow.vtu、surface_flow.csv、history.csv。Code_Aster/Elmer/MOOSE 等仍无 adapter 或真实 golden。 |
| C09 | Fluent/STAR-CCM+/Abaqus/COMSOL/HyperMesh 等商业后端 | ⏸ | 目前仅接口预留，要求组织自配 license/remote/HPC，不捆绑；没有实际求解验证。 |
| C10 | UDF 自定义求解器/库的 wmake 编译验证 | 🟡 | 最小 functionObject 的 D1–D5/G5 垂直切片已通过：v2406 `wmake` 编译/链接、动态加载、`functions` 注册执行、日志标记和新时刻产物。自定义求解器、其他 UDF 类型、通用阶段工作流及完整 G5 验收矩阵仍未实现。 |
| C11 | 多工况目录派生 | ✅ | `/runs:derive` 精确保留初始 `0/`，跳过其他时刻/日志/.foam；`0/U`、`0/p` 均保留，派生工况已实际运行 icoFoam 并产生新时刻。 |
| C12 | 多工况批量提交、队列与洪泛控制 | ✅ | `/jobs:batch` 支持 1–50 项、每批并发 1–4 和用户级配额等待；case 内 manifest+jobs 表持久化，批次列表/计数、整批取消与按 case 统一取消均有真实 API/Windows 子进程验证；JobsPanel 多行提交和重载恢复浏览器通过。篡改清单不能读取/取消其他 case 作业；200 作业负载洪泛仍归 J21，多 worker 归 J14。 |
| C13 | restartPolicy、作业监控与 artifacts 关联 | 🟡 | 有状态、日志和产物目录；没有完整重启策略、restart 链路和 caseDict artifacts 关联。 |
| C14A | 残差下降、网格/时刻产物和运行行为验证 | 🟡 | 2D/3D golden 已验证正常结束、Mesh OK、残差下降、Uz 和时刻目录；这不是参考场数值对拍。 |
| C14B | 参考场 verification/validation、L2 误差与更多黄金用例 | 🟡 | 同网格参考场 L2/来源/hash 和一单元制造解已验证；2026-11-02 新增真实 laplacianFoam 80 单元导热，与独立 T(x)=300+100x 比较，输出精度内 L2=0 K、容限 1e-6 K，并走完报告闭环。仍无实验 validation、异网格映射或 pitzDaily/forces、damBreak、UDF、Flotherm 等更多黄金对拍。 |
| C15 | optimization/parametric/UQ/modal/harmonic 等分析类型 | 🟡 | schema 可表达，实际求解/后处理流程未覆盖。 |
| C16 | 远程资源/HPC executionProfile | 🟡 | `/remote:execute` 仅 admin 可用，强制 VIBEFLOW_REMOTE_HOSTS allowlist、client key/known_hosts、禁 agent 和超时；本地临时 SSH server 实测公钥认证、stdout/exit_status 和非法主机拒绝。未接文件同步、持久作业/Slurm/PBS、结果回收或真实 HPC。 |
| C17 | 标准算例 Allrun/Allclean、0.orig 与 canonical 输入保留 | 🟡 | 当前主要通过 API 逐条提交命令，未形成标准的 Allrun/Allclean/Allrun.pre、0.orig 和可重跑输入生成与清理契约。 |
| C18 | 坐标系、重力、参考量和跨物理控制模型 | 🟡 | 通用 schema 可存，坐标/重力/参考量及连续介质、流体、固力、热、电磁、声学控制模型没有完整后端映射和验证。 |
| C19 | 通用 JobsPanel 的 runtime 选择与 WSL 提交 UI | ✅ | JobsPanel 已提供 Windows/WSL 选择并正确传递 runtime；浏览器实际提交 WSL `pwd` 返回 `/mnt/` 路径。 |

**C 类核心缺口**：多后端、通用 UDF 工作流与更多类型、批量与远程执行、并行取消/集群调度、数值结果对拍。

### D. 后处理与 ParaView 可视化

原版证据：内置 ParaView 5.11、help/build 文档、scenes.py 契约和 caseDict postProcessing。Sources/Filters、28 插件、完整 Color Map/Animation 细节缺少直接清单，以下保留这个边界。

| ID | 原版功能/子项 | 状态 | 当前实现与剩余差距 |
|---|---|---|---|
| D01 | 内置 ParaView Render View 与可视化会话 | 🟡 | 当前以 VTK/trame 远程渲染替代，2D/3D 已实证；不是完整 ParaView 5.11 工作区，wslink 数据面也未独立做当前契约测试。 |
| D02 | Pipeline Browser 与活动对象选择 | 🟡 | 已有按类型分组的 Pipeline、活动对象选择、可见性和删除；五类对象浏览器创建和 Inspector 选中已验证。多选、完整树状态图标和右键语义未实现。 |
| D03 | Inspector 属性编辑 | 🟡 | 按对象类型的参数表单已实现，切面位置编辑/应用已写回场景并经浏览器验证；没有 server-manager XML 自动面板和全量属性覆盖。 |
| D04 | Information 数据元信息 | 🟡 | 点/单元/bbox/场范围/时刻已有，补充点/单元数组的名称、关联、分量、VTK 类型和字节数，VTP 的 p=12 B 已在浏览器验证；完整内存统计、更多网格元信息和 server-manager Information 未实现。 |
| D05 | Sources / Filters 菜单与完整过滤器目录 | 🟡 | 已有 surface/slice/contour/stream/glyph 五类 Sources/Filters 添加按钮，浏览器创建、分组和 Inspector 编辑均通过；没有完整目录、命令面板或 XML 自动比对，流线/矢量也仍受输入向量场限制。 |
| D06 | Color Map Editor | ✅ | 预设、范围、反转、对数、不透明度已有；自由控制点规范化、RGB 插值、v1 导入导出和非法输入通过。viz custom LUT、默认/导入/导出与 `D-post/colormap.json` 已接入，浏览器应用黑—黄—黑渐变并核对截图；RGBA 预设兼容回归已修复。高级映射未覆盖。 |
| D07 | Animation View | ✅ | 时间播放、fps 和 PNG 帧序列已有；新增 `vibeflow.animation.camera.v1` 关键帧校验、位置/焦点/up 插值、异步播放/暂停/循环和逐帧导出。真实导出 5 帧且首末 hash 不同，最后视角和导出后视图恢复均已人工核对；不是完整 ParaView 全类型轨道编辑器。 |
| D08 | Python console | 🟡 | 已有受限 VF Python 控制台：仅白名单函数和字面量参数，支持 surface/remove 等；import、属性、下标和推导式被拒绝，任意 pathlib 写文件无副作用。不是通用 Python 控制台或硬沙箱。 |
| D09 | scenes.py 文件监听与当前会话增量执行 | ✅ | watcher、同一命名空间、VF_* 复用、执行日志已实现；`__file__` 和 `VIBEFLOW_PARAVIEW_SCRIPT` 已注入并由真实 watcher 验证。`ViewSize` 在 exec 前拒绝、记录失败且不终止 watcher，后续脚本可继续增量执行；这不是宿主 Python 硬沙箱。 |
| D10 | VF_surface/slice/contour/stream/glyph/color/remove/time/reset_camera/shot/load/list 契约 | ✅ | 自有 facade 覆盖上述函数，真实 watcher 的逐函数契约矩阵已验证日志、对象列表、删除、时刻、截图和非法 load 后继续执行；D09 的执行上下文也已验证。 |
| D11 | pointProbes/lineSamples/surfaceIntegrals/volumeAverages/modalQuantities/frequencySpectra | ✅ | caseDict.postProcessing 快照驱动专用作业；六类操作均有真实 VTK/NumPy 提取和制造解数值断言，含域外点拒绝、CSV 重算和版本化产物。当前为 internalMesh 外表面面积分与探针时间 POD，不扩展宣称任意 patch 通量或全场模态。 |
| D12 | formulas/referenceValues/uncertainty/comparisonTargets | ✅ | AST 白名单公式（无 eval/exec）、referenceValues/L2、standard/coverage uncertainty 均有数值与负向测试；comparisonTargets 的 experiment/benchmark/previousRun/requirement 映射、reference ID 和 readiness 全部通过，类型错配会阻断。数组公式、高级不确定度传播和自动采集实验/前次运行数据源属于剩余深度。 |
| D13 | slices/contours/isoSurfaces/streamlines/vectors/deformation/modes | 🟡 | 前五类有实现；deformation、modal 和更完整的体等值面/变形分析未覆盖。 |
| D14 | postprocess.py 数据分析与报告图件 | 🟡 | 已有 caseDict 驱动的专用后处理执行器、metrics/CSV/manifest、Agg PDF/SVG/PNG 出版图和失败保留；原 postprocess.py 脚本兼容、多序列复杂版式及报告自动插图仍未实现。 |
| D15 | pvpython/pvbatch 离线兼容 | ✅ | ParaView 5.11.2 两个真实可执行后端已接入 `/paraview:run`，`.py` 上限/归属/fail closed、argv 作业和日志均验证；server-manager Sphere 226 点、VTP 与 RenderView PNG 在 pvpython/pvbatch 均通过，非 pip VTK 替代。完整 server-manager 通用属性 UI 仍归其他 D 项。 |
| D16 | 截图与相机控制 | ✅ | `VF_shot`、重置视角已有；新增 +X/+Y/+Z/等轴测预设、保存/恢复视角和截图。实测预设图像不同、恢复后 PNG hash 与保存时一致，应用截图已人工核对；不含完整 ParaView 相机书签/多视图状态。 |
| D17 | Catalyst / Extractors | ⏸ | 已明确有意砍掉，非当前开发遗漏。 |
| D18 | Comparative / Memory / Material Inspector | ⏸ | 已明确有意砍掉，具体原版字段仍需补证。 |
| D19 | ParaView 原生宏菜单 | ⏸ | 原版没有自研宏管理器；当前也不支持该原生菜单，列为有意长尾缩减。 |
| D20 | 28 个插件长尾 | ⏸ | 有意砍掉；名称、默认加载状态和子项目前均未完整枚举。 |
| D21 | 协作面板 | ⏸ | 有意砍掉，原版连接方式仍未知。 |
| D22 | 多用户/大数据可视化性能与内存回收 | 🧪 | 有会话并发限制和进程退出，但没有 50 用户负载、内存曲线、空闲回收完整性和交互延迟验收。 |
| D23 | 出版级图表：单位、色盲友好、PDF/SVG、300–600dpi PNG、Agg | ✅ | caseDict `artifacts.plots` 可从探针/线采样生成 PDF/SVG/300–600dpi PNG；Agg、Okabe–Ito 配色、轴单位、图内 case/job/run/caseDict 来源及 manifest hash 均有真实文件断言，300dpi PNG 实测 1800×1200 并已人工查看。多序列/复杂版式仍属后续增强。 |
| D24 | 后处理证据表、图表追溯与 readiness | ✅ | 版本化指标、参考 L2/CSV/图表来源/readiness 已验证；提取前快照及发布复核，报告 compose/build/LaTeX 核对工程、求解文件、参考、目录及产物哈希，空/坏清单拒绝。续作已验证报告构建期间变更拒绝与旧稿保留；跨文件/多副本事务和任意 LaTeX 外部依赖仍未完成。 |
| D25 | 通用 VTK/VTU/XDMF/Exodus/STL 结果读取 | ✅ | `result_readers` 支持 VTK/VTU/VTP/VTM/XDMF/XMF/Exodus/STL/series；真实文件逐格式验证点/单元、series 时刻、file_path viz 和路径安全。sid 路径 cookie 修复 trame 子资源/wslink 鉴权，VTP 浏览器实际显示三角形、3 点/1 单元和 p=[1,3]，截图已人工核对。多块选择和超大文件仍未覆盖。 |
| D26 | viz 持久会话、多副本共享与空闲回收 | 🟡 | 会话记录 `last_activity`，proxy/ws 请求续期，后台按超时回收进程；`viz_idle_smoke` 验证旧进程终止/移除/日志关闭和活跃会话保留。会话仍只在单进程注册表，未落库、不能跨副本共享或恢复。 |

**D 类主要缺口**：完整 Sources/Filters、server-manager 属性体系、Python console、数据提取/派生指标、自由色图与关键帧、ParaView 原生脚本兼容。

### E. 报告、LaTeX、文献与文档查看器

原版证据：caseDict report schema、features-0.10.0.md、vibeflow_document_conversion.py、resources/texo、PDF/Markdown 资源。**TikZ 可视编辑器已在原版后续版本删除，不计入 0.11 缺口。**

| ID | 原版功能/子项 | 状态 | 当前实现与剩余差距 |
|---|---|---|---|
| E01 | 报告章节/schema（问题、假设、几何、网格、材料、方法、结果、限制等） | ✅ | `report_builder.py` 从 caseDict、quality.json、metrics/manifest 自动组织 9 章，含验收、假设、几何/材料、网格/方法、结果、verification/validation 区分、不确定度、限制和证据索引；真实 compose/HTML 验证通过。 |
| E02 | pandoc HTML/DOCX 报告 | ✅ | 中文/指标/证据链接与 DOCX XML 已验证；新增 Markdown/图片快照、编译私有输出、发布复核/最终 hash 清单、旧作业不覆盖新稿。真实并发修改、取消保留旧稿通过；HTML 内嵌 SVG/MathML 和 DOCX 图片浏览器通过。仅 fs/本地资源；复杂版式另列 E22。 |
| E03 | XeLaTeX/LuaLaTeX/pdfLaTeX 编译与 PDF | ✅ | 三种 recipe 分别生成真实 PDF；biber 端到端生成 `.bbl` 且日志可追踪，不再只验证 xelatex 或接口参数。临时目录、禁 shell escape、超时和并发语义另列 E04。 |
| E04 | LaTeX 临时目录、禁 shell escape、超时、防抖、过期结果丢弃 | ✅ | 独立源快照、复制 hash 复核、排除旧 build/out；禁 shell escape/norc、120 秒 timeout。新增发布前工程/源清单检查，真实编译中变更保留旧 PDF；latest-wins、失败保留、清理和前端代次已有测试。任意动态/绝对外部依赖冻结仍未完成。 |
| E05 | 公式调色板、预览和插入编辑器 | ✅ | 分类符号、KaTeX 预览、包裹格式、插入 Monaco 已实现。 |
| E06 | refs.bib、Crossref/arXiv 检索与引用插入 | ✅ | 检索、去重追加 BibTeX、插入 cite 已实现；本次 Crossref/arXiv 均返回结果。 |
| E07 | Semantic Scholar 文献源 | ✅ | 固定 Graph API、字段/BibTeX 规范化、UI 来源和错误显示完整；mock 验证两次 429 后成功、Retry-After/指数退避、持续 429→503、网络 502。2026-10-28 完整 67 项回归中公共 API `LIVE_OK`，在线返回 1 条。 |
| E08 | 引用样式、DOI/作者年份细节和失败行为 | 🟡 | BibTeX 使用正式 `doi`、`eprint/archivePrefix` 字段；APA/IEEE/Chicago 轻量预览、作者 initials/et al./年份/链接和检索错误显示均有测试。尚不支持任意 CSL 文件或完整出版商样式。 |
| E09 | LaTeX 表格生成器 | ✅ | `latex_table.py` 与 `/latex:table` 支持逐字转义、矩形校验、booktabs、边框和 l/c/r align；LatexSection 可生成并插入 main.tex，插入前备份原文件，API、纯函数和浏览器插入均通过。 |
| E10 | LaTeX 错误结构化与 PDF 回跳 | ✅ | `/latex/errors` 提供诊断和 Monaco 源行跳转；构建发布必需 `.synctex.gz`，`/synctex:query` 用真实坐标映射源文件/行号。精确 `(134,157)→main.tex:7`、双击 L7、快照路径映射和越界测试通过；旧 PDF 无 sidecar 时需重编。 |
| E11 | TikZ 可视编辑器 | ⏸ | 原版已删除该入口，**不是当前缺失功能**。 |
| E12 | MinerU ZIP→Markdown helper | ⏸ | 用户已明确不接；本地文件安全解压/引用重写也未实现，按决策记录。 |
| E13 | 为现有 PDF 添加页码（number-pdf） | ✅ | argv worker 用 pypdf 追加每页底部居中/9pt/灰色 `page / count`，保留原文、页数和元数据；源哈希不变，输出独占创建。真实两页 API、内容流、浏览器文本层和页底截图均通过。 |
| E14 | Markdown/MathJax 预览 | 🟡 | 文件 Markdown 已用独立 Marked+KaTeX 支持四类 inline/display 分隔符、代码排除和 `trust:false`，真实预览与脚本/危险链接负例通过；不宣称完整 MathJax 扩展兼容。 |
| E15 | PDF 阅读、翻页、缩放与文本层 | 🟡 | HEAD+Bearer Range transport 只接受 206 并校验长度；目录跳页、全文搜索匹配页、页码/缩放恢复和渲染取消有真实测试。378,936 字节 PDF 以 3 次 Range 读取 182,328 字节；超大文件性能、搜索高亮和旋转页细节仍未验收。 |
| E16 | PDF 划词翻译与复制 | ✅ | 当前浏览器专项在 PDF 第 2 页实际选择文本，mock 非流式翻译卡出现预期结果，并通过剪贴板权限读取确认复制内容；后端双协议 mock 翻译也有回归。 |
| E17 | 图片查看 | ✅ | Bearer Blob 加载、错误状态、按钮/滚轮缩放、拖拽、复位均有硬断言；新增 90° 步进旋转和 Fullscreen API/固定定位降级，实际包围盒、进入退出与旋转复位通过，截图已核对。 |
| E18 | CSV/表格查看 | 🟡 | Papa Parse 支持引号、转义引号、字段内换行/分隔符和空格，保留排序/搜索/数值对齐/2000 行截断，增加 warning 与 stale 响应保护；大表性能和 AG Grid 级功能仍未实现。 |
| E19 | Texo 离线截图公式识别 | 🟡 | 本地模型、选图、识别、LaTeX 编辑、复制和插入均有实现；E2E 主要断言结果非空，不是公式识别准确率验证。 |
| E20 | Texo 屏幕框选取图 | 🟡 | 用户点击后 getDisplayMedia，拖动裁剪视频帧为 PNG 并进入现有预览/OCR，支持停止共享；坐标边界单测和 mock captureStream 的 Edge 拖动/PNG/轨道停止通过。未自动或后台截屏，真实显示器授权捕获尚未验收。 |
| E21 | PPT 相关功能 | 🟡 | `/ppt:build` 和 CasePanel 已实现可编辑 PPTX 垂直切片：文本/bullets、原生形状/表格/图片、只读模板、版本化 spec、真实 worker 和下载；Open XML、模板哈希及 UI spec 均验证。Beautify、187 presets、图片重建、叙述/视频和渲染器截图仍未实现。 |
| E22 | HTML/DOCX 专用阅读器 | ✅ | `DocumentPanel` 提供 HTML/DOCX 语义阅读、表格/图片、heading 目录、页内/跨文件导航和原件 Bearer Blob 下载；Mammoth+DOMPurify+Shadow DOM，不执行宏/脚本，危险标签/事件/样式有负例，算例图片用 Bearer Blob。不是 Word 复杂分页/版式逐像素复刻。 |

**E 类主要缺口**：Semantic Scholar 在线成功与任意 CSL、超大 PDF/大表性能、完整 MathJax/出版商兼容、Texo 准确率与授权屏幕取图，以及 PPT 的 Beautify、presets、图片重建、叙述/视频和渲染验证。

### F. Agent、LLM、工具与会话

原版证据：`resources/agent/agent-conversation.html`、`agent-v2-architecture.md`、`agent-branches-stability.md`、`agent-paths-sessions.md`、`codex-local.md`、`codex-agent-development.md`、`CLAUDE.md`。原版直接确认 11 个内置工具；`request_approval` 是模式，不是第 12 个工具。

#### F-1. 对话、模型与展示

| ID | 原版功能/子项 | 状态 | 当前实现与剩余差距 |
|---|---|---|---|
| F01 | API/Codex Windows/WSL 统一聊天与停止按钮 | 🟡 | 统一 API Agent 页面和当前回合停止/取消已验证，部分内容 cancelled 且可重载恢复；Codex 双后端线程仍未实现。 |
| F02 | text/reasoning/tool delta、SSE 与旧非流式兼容 | 🟡 | OpenAI/Anthropic 流式与非流式 complete 已有；旧 JSONL/旧非流式历史格式兼容未实现。 |
| F03 | Markdown、代码、表格、链接和公式 | ✅ | marked+DOMPurify+KaTeX 已实现，链接/原始 HTML 采用消毒策略，属于替代实现。 |
| F04 | 流式 Markdown 与完成后权威重渲染 | ✅ | `ChatMarkdown` 按围栏感知稳定块渲染，未变块保持同一 DOM；完成后从权威全文替换，流式光标恰为 1、完成清除。真实分段代码块 E2E 和 StrictMode 单份回答均通过。 |
| F05 | 长回答/代码性能降级 | ✅ | 回答仅渲染末 512 KiB 且保留全文复制；代码块超过 32 KiB 改纯文本；渲染缓存最多 64 项/512 KiB 字符并有逐出测试。真实 40KiB 代码和 600KiB 回答/600,018 字符复制通过。 |
| F06A | Reasoning adapter 与 reasoning.delta 事件 | ✅ | OpenAI `reasoning_content` 和 Anthropic `thinking_delta` 均有当前双协议 smoke；默认 off 不发参数，显式强度发送 `reasoning_effort` 或 `thinking.budget_tokens`，并验证事件顺序和最终落库。 |
| F06B | Reasoning 卡、活动工作过程与最终回答分离 | ✅ | reasoning 单独累积到 assistant.payload.reasoning，正文不混入；流式卡展开、完成折叠，可手动展开且重载恢复。浏览器验证单份最终回答、卡片折叠/展开与持久化；不伪造 reasoning。 |
| F07 | 工具卡状态/类型图标 | ✅ | queued/running/awaiting_approval/completed/error/cancelled/interrupted 有统一状态标签；安全与 full-access 工具持久化 queued→running→final，审批返回真实 tool.result，不再伪造“批准即成功”；临时 SQLite 验证孤儿 queued/running 启动转 interrupted。类型图标、旧工具名及旧中文标题映射有纯函数与浏览器证据。 |
| F08A | 审批 diff 增删统计 | 🟡 | 审批卡已有 added/removed 统计和着色预览，属于提案功能，不是聊天消息操作。 |
| F08B | 消息复制、修改摘要、回底部/指定用户消息导航 | ✅ | 用户/助手复制有剪贴板硬断言；指定用户消息定位与持续高亮、真实滚动区回底部、文件提案修改摘要定位均通过。首次会话历史加载完成前禁止发送，修复旧响应覆盖新用户消息。 |
| F09 | 空回复、纯占位内容和真实修改摘要保留 | ✅ | `filterDisplayMessages` 过滤空/常见占位文本，但保留有效短回答、reasoning-only、cancelled、tool_calls 和真实修改摘要；流式与重载均验证，DB 事件不删除。 |
| F10 | 回复提示音 | ✅ | WebAudio 提示音已实现；开关/触发细节未逐项对齐。 |

#### F-2. Profile、模型、上下文与网络边界

| ID | 原版功能/子项 | 状态 | 当前实现与剩余差距 |
|---|---|---|---|
| F11 | LLM profile 配置 | 🟡 | 新建/列表/删除之外，PATCH 编辑、保留/更换密钥、`/models` 动态发现和 `:test` 连接测试均有双协议当前证据，离线回退和跨用户 404 通过；独立完整设置页仍未实现。 |
| F12 | 多 provider 与动态模型/推理强度 | 🟡 | OpenAI/Anthropic 动态模型列表、可编辑新会话模型、按请求 reasoning 强度和离线回退均已验证；保存失败保留编辑内容。环境缓存和跨环境模型设置持久化仍未实现。 |
| F13 | Codex 容量定向重试 | 🟡 | 高层 turn 执行器只识别 at capacity/serverOverloaded，保留线程并发送短继续，原操作仅一次，最多五次退避/默认抖动；非容量错误、取消和总超时不重试。真实异步策略与 turn 测试通过；完整 Agent UI 和真实 CLI 容量故障未验证。 |
| F14 | API key/端点保护：HTTPS 或回环 HTTP、DPAPI、WSLENV、错误脱敏 | 🟡 | 服务端 Fernet 文件保护密钥；base_url 拒绝非回环 HTTP、URL 内嵌凭据，模型列表/连接错误做密钥脱敏并有测试。DPAPI/WSLENV 语义和完整错误预览脱敏仍未实现。 |
| F15 | 上下文用量显示/估算 | 🟡 | 有按历史正文、工具结果和当前输入字符数/4 的实时估算，reasoning 不计入；明确非 provider 计费 token。尚无 Codex 悬停用量或 API 尾沿精确 token 统计。 |
| F16 | 跨后端/环境文字上下文交接 | 🟡 | `/handoff` 以最新压缩摘要或 keep_chars 有界文本开启新会话，保留 case/profile/model/permission，source_conversation_id 与模型消息恢复均验证；尚无 Windows/WSL/Codex 环境切换和 lastTurnId 对账。 |
| F17 | 模型上下文、显示缓存、完整日志分离与有界回放 | 🟡 | 已有 280/1200/6000 事件及 4/8/16 MiB 分级、最新优先、event_window 向前分页和单事件 JSON 总字节截断；history_smoke 与 300 条浏览器加载更早通过。尚无 provider 精确 token/自动模型窗口预算和更长历史性能验收。 |
| F18 | 回答语言、固定追问模板和最终结构 | ✅ | 系统提示同时包含 Status Update/Todo/Next Steps 和 Parameter name/Purpose/Explanation/Recommendation，OpenAI/Anthropic 真实 mock 请求均验证。 |

#### F-3. 内置工具、权限与安全

| ID | 原版功能/子项 | 状态 | 当前实现与剩余差距 |
|---|---|---|---|
| F19 | read/list/search 及重型目录排除 | ✅ | 三个只读工具均有直接执行 smoke；read/list/search 与 subdir、include_internal 生效，node_modules/build/.vibeflow 等统一排除，越界和不存在路径 fail closed。 |
| F20 | `web_search`：后台取消、runId 隔离、共享预算 | ✅ | 每 Agent run 独立 runId、共享有界预算、cancel Event 和 tool_result 持久化；默认 mock 之外新增 Wikipedia/Crossref provider，Crossref 在线返回真实 OpenFOAM 结果 LIVE_OK。Wikipedia 保留 IPv6 阻断记 LIVE_LIMITED；mock/Agent 回归通过。 |
| F21 | `web_fetch`：重定向校验、公网 IP 固定连接、私网拒绝、8 MiB 上限 | ✅ | 自定义 resolver 校验 DNS 并固定公网 IP，直接 IP 单独校验；保留原 Host/SNI/证书校验，禁环境代理/Cookie，逐次重定向复查，8 MiB/超时/内容类型和取消均有测试。SSRF/Agent 私网续跑通过；example.com 证书失败 LIVE_LIMITED，Crossref 公网 safe_web_fetch 返回真实 JSON LIVE_OK。 |
| F22 | `update_plan` 与三状态 Todo | ✅ | 工具、计划条及 pending/in_progress/completed 已有；直接 smoke 验证合法三状态、两个 in_progress 拒绝和非法状态 fail closed。 |
| F23 | `update_goal` 与 Session 目标持久化 | ✅ | update_goal 校验目标/最多 10 条验收条件/max_turns/max_tool_calls/状态和终态原因；append-only goal 事件带 session goal_id 和递增 revision，current_goal 独立返回，下一回合系统提示恢复。goal.updated 与浏览器目标条/重载均验证；只更新目标，不自动续轮（F50 另列）。 |
| F24 | `run_wsl_command` | ✅ | `run_shell_command` 增加 runtime=windows/wsl；真实 v2406 `wsl --exec bash -lc` 验证 source 后变量展开、Windows 默认、非法 runtime 和危险命令拦截，不使用 root flag。 |
| F25 | `run_windows_command` / PowerShell 语义 | ✅ | `runtime=powershell` 以 `powershell.exe -NoProfile -Command` 实际执行，Write-Output、非法 runtime 和危险命令拦截均有专项测试；Windows 默认保留。命令执行仍不是硬沙箱，生产隔离另列。 |
| F26 | `propose_file_update`：diff、理由、批准/拒绝/autoApply/readOnly | 🟡 | diff/理由/审批/full_access 已有；新提案记录 baseline_sha256，批准时再次校验，外部修改拒绝且不覆盖，普通文件同目录 fsync+os.replace 原子写并有 Agent 竞态测试。只读历史/autoApply 的完整变体和 caseDict 原子写统一仍未闭环。 |
| F27 | `propose_text_replacement` 聚焦编辑 | 🟡 | 存在性/唯一性预检、替换和直接执行器测试已有；新增 baseline_sha256 和批准后外部修改保护，文本替换原子写及不存在→存在竞态通过。完整双协议模型审批矩阵仍未覆盖。 |
| F28 | Ask/full_access/request_approval 的不同后端语义 | 🟡 | API 三档权限已有；Ask 不提供工具且违规结构化 tool_calls 也拒绝执行，伪协议统一清洗。Codex Ask 只读沙盒、Plan 只读优先、未批准 Plan 等变体未实现。 |
| F29 | 工具审批卡与一次性按钮 | ✅ | 批准/拒绝、pending 和执行后状态已实现。 |
| F30 | Codex 复杂审批、建议答案、自由输入和秘密字段 | 🟡 | 按公开 schema 支持 command/file/permissions 审批和 `item/tool/requestUserInput` 选项/isOther/isSecret 与答案映射；高层 turn 通过回调处理，未提供回调 fail closed。空答案拒绝，通知不携带秘密值。完整交互卡片和秘密遮蔽 UI 未接，真实 CLI 未验证。 |
| F31 | 文件基线哈希、原子提交和外部修改保护 | ✅ | 编辑器 mtime 检查保留；Agent 文件/文本提案记录 baseline_sha256，批准时二次校验，外部修改和不存在→存在竞态不覆盖。普通文件同目录临时写、flush+fsync、mode 保留和 os.replace 有直接/Agent 测试。caseDict 仍走统一专用写入。 |
| F32 | 工作区、最近父目录与符号链接防护 | ✅ | `_fs_path` 同时约束 storage root 与当前 prefix，使用 Path.is_relative_to 而非字符串前缀；resolve 覆盖最近存在父目录。内部链接允许，跨 case、外部/相似根、broken link、链接下未创建目标和 .. 均有临时目录正负测试；case 管理/Range/提案/caseDict 回归通过。 |
| F33 | Ask 伪协议/DSML/sentinel 清洗 | ✅ | 独立增量 sanitizer 同时用于双协议正文/reasoning、assistant 落库和历史读取/重发；逐字符、闭合/自闭合、无结束 sentinel、重复块通过，普通相似标签保留。Ask 下 provider 违规结构化 tool_calls 明确拒绝执行且无工具卡。 |
| F34 | IPC/模型流/正文/tool call 上限、超时与秘密不记日志 | 🟡 | 双协议实现模型流 64 MiB、正文+reasoning 8 MiB、单 SSE event 8 MiB、单 tool call 2 MiB，权威响应 10 MiB；错误预览 16 KiB 且 Bearer/API key/token/secret/sk-* 脱敏。客户端断开到 provider 取消实测约 0.110s。同步命令/只读扫描的断开取消和多副本 IPC 域仍未完成。 |

#### F-4. 上下文、会话、恢复与分支

| ID | 原版功能/子项 | 状态 | 当前实现与剩余差距 |
|---|---|---|---|
| F35 | Session 独立对话、计划、目标、权限、模型与摘要 | ✅ | 独立对话/权限/模型/摘要已有；update_plan 三状态与 update_goal 目标/验收/预算/revision/恢复均有当前测试，目标条重载保留。Goal 自动续轮不属于本项，见 F50。 |
| F36 | `memory.md` 长期经验记忆 | ✅ | `update_memory` 支持追加/压缩，单批 16 KiB、总量 64 KiB，按行保留最近经验且不截坏 UTF-8；实际原子写+即时索引，下一独立模型请求系统上下文包含记忆。Plan 不下发且 provider 违规调用拒绝，普通模式续跑注入和只读负例均通过。 |
| F37A | 手动压缩上下文 | ✅ | compress 端点、摘要事件和 UI 已验证。 |
| F37B | 新对话恢复压缩摘要 | ✅ | handoff 使用最新压缩摘要，保留 case/profile/model/permission；handoff 事件和 `_build_messages` 均包含摘要，跨用户 404。无摘要时有界最近文本交接，不复制完整历史。 |
| F38 | 统一 JSONL、旧格式与 Session/Run/Request 生命周期 | ✅ | 自有 session/event.v1 JSONL 完整导出/导入；旧 message/tool/request 可读，摘要 ID 重映射。每次模型调用有独立 request_id/request_index，隶属 conversation/run_id，request_state 覆盖 started/completed/failed/timed_out/cancelled/interrupted 和起止耗时；同 Run 多 Request 不串 ID，断开/启动/导入均恢复中断，完成/失败/取消有真实回归。 |
| F39 | 多会话后台并行、归属与离开页面继续运行 | ✅ | 每路 fetch/SSE 绑定 conversation ID 和 ActiveRun 实例，模块级 ActiveRun 生命周期注册表在切换/重开面板时恢复各自缓冲；历史加载有 stale 防护，旧完成回调不能清理新运行。A/B 并行输出不串会话、后台继续、返回恢复，停止 B 仅 B cancelled、A completed。真实浏览器断开仍按 F34 取消，不擅自改为离线后台执行。 |
| F40 | 每会话停止与运行配置锁 | ✅ | `messages:cancel` 设置每会话 Event，LLM next 与取消竞争，部分 assistant 以 cancelled=true 保存；前端协作取消+2 秒旧连接 abort 回退，重载保留取消状态，下一回合可运行。运行中锁 profile/conversation/权限/reasoning。 |
| F41 | Codex resume、异常对账与缺失回答插回 | 🟡 | stdio 支持 thread/resume+lastTurnId；clientUserMessageId 对账区分 confirmed/unconfirmed、缺失回答插入位置和 orphan，已有 turn 不重复，未确认请求 auto_resend=false。尚未把对账结果接入完整 Agent DB/UI 插回，真实 Codex 未验证。 |
| F42 | 750ms/16KiB checkpoint 与异常中断恢复 | ✅ | run_checkpoint 按约 750ms/16KiB 和收尾 flush 写入，run_state 区分 started/completed/cancelled/failed/timed_out/awaiting_approval/interrupted；断开由独立 shield 任务写 interrupted，启动恢复 started。浏览器恢复半截正文/reasoning 且不重复、禁分支。 |
| F43 | 从已完成回答创建分支 | ✅ | fork 接受 head_event_id，按可见事件（含祖先链）校验，只有无 tool_calls、未取消且所属 Run 最终 completed 的 assistant 可作为边界；用户事件、取消/中断/失败/中间消息和跨用户均拒绝。第二回答前历史零拷贝保留，原会话不变，浏览器分支/删除通过。Codex 原生变体见 F44。 |
| F44 | Codex 原生 fork、回合中间/旧记录文字交接 | 🟡 | stdio fork 支持 lastTurnId/excludeTurns，turn/start 携带稳定 clientUserMessageId；只有同环境完整原生回合最后回答允许 native fork，其余走最近 180K 字符文字交接，工具/取消/审批不重放。协议与策略测试通过，尚未接完整 Agent 分支 UI/真实 Codex。 |
| F45 | 分支状态/文件边界 | ✅ | 分支引用父事件、共用而不复制 case 工作目录；活动 ToolCall/Goal 不复制，历史审批可见但在分支 approve 返回 404，源审批/Goal 保留且文件逐字节不变。拒绝分支事务无半提交记录，分支续聊不改源事件。Codex 原生 fork/文字交接另列 F44。 |
| F46 | 会话重命名与运行中删除约束 | ✅ | PATCH 重命名去空白并限长，跨用户 404；活动回合、pending/queued/running 工具和存在 fork 源均禁止 DELETE，取消/终态/先删分支后可删。删除仅清理会话事件/工具记录，不动算例文件；浏览器重命名/删除及目标清理通过。 |
| F47 | 旧 Codex 缓存保留与新 Agent JSONL 边界 | ✅ | 原版明确要求旧 `codex-sessions` 不删除且**不自动搬迁**，不是要求自动迁移。会话重命名/删除仅清理 DB 会话、事件和工具记录；真实旧缓存 sentinel 在重命名、分支删除和源会话删除后逐字节保持不变，测试后仅清理该 sentinel。新会话使用自有 Agent JSONL 导出，旧缓存导入仍按原版不自动执行。 |
| F48 | 长历史有界回放/归档、显示故障恢复、每轮计时 | ✅ | F17 的 280/1200/6000 事件和 4/8/16 MiB 分级、向前分页与单事件截断已验证；run_state 持久化每轮起终点/耗时并按 run_id 显示。ErrorBoundary 在 60 秒窗口最多自动重建显示树 3 次，generation 强制卸载旧视图，持续故障保留手动重载；真实浏览器瞬时/持续故障通过，模型消息 POST=0，不重放运行。 |
| F49 | 会话导出 | ✅ | session/event.v1 JSONL 含完整祖先链、数值 id/event_id/created_at/seq、ToolCall 快照且无密钥；按 Run 导出 eventId/time/workingDirectory/mode/permission/model，用户消息关联后续 Run。原子回导和旧 message/tool/request 可读，摘要 ID 重映射，权限/工作目录不恢复；活动导出 409、314 事件和浏览器下载/导入通过。 |

#### F-5. Goal、CLI harness 与技能

| ID | 原版功能/子项 | 状态 | 当前实现与剩余差距 |
|---|---|---|---|
| F50 | API Goal 自动续轮与 Codex“等待继续”变体 | 🟡 | API `/goal:run` 有界自动续轮、配额/turn/tool 预算、取消与完成停止已验证；Codex 高层 turn 在 goal=True 时完成返回 waiting_continue，不叠加 API 自动循环，停止/超时 interrupt。Codex Goal UI/真实线程续聊仍待接。 |
| F51 | Plan 工作模式、批准与恢复 | ✅ | permission_mode=plan 仅下发只读工具，provider 违规危险 tool_calls 也拒绝且零副作用；`/permission` 在用户明确批准后切回 approve_writes，活动/待审批时禁止修改。只读执行、危险拒绝、批准恢复写入和浏览器按钮均通过。 |
| F52 | AgentHost 有序 IPC、33ms 合并、断开取消 | ✅ | 每 run 的 SSE 带 run_id 和严格递增 seq；相邻同类型 text/reasoning 按约 33ms 合并，工具/完成/错误前强制冲刷且不重排。稀疏 delta 由剩余时间 timeout 主动冲刷，pending anext 复用不并发读取。AB 合并、C→tool.state 顺序和 seq 均通过；断开取消与恢复见 F34/F42。 |
| F53 | Codex app-server JSON-RPC/stdio | ✅ | 独立读取循环按 id 分发响应并持续排队流式通知，支持 thread new/resume/fork、lastTurnId、turn/interrupt、accept/acceptForSession/decline/cancel 审批响应与关闭清理；1 MiB 帧上限、非法审批枚举/ID 拒绝。真实子进程假 app-server 验证异 id 不串响应；未执行真实 Codex，不代表账户/UI/沙盒及完整 Agent 链路已对齐。 |
| F54 | CLI 自动发现、显式路径、版本目录与 WSL Node 检测 | ✅ | `discover_cli` 支持 VIBEFLOW_<TOOL> 显式路径优先、_DIR 版本目录、PATH .exe/.cmd/.bat/无后缀、稳定版>RC>beta>alpha 和 WSL Node 探测；显式错误 fail closed，capabilities 新增 cli_agent_details 且兼容原字段。独立 smoke 与 live capabilities 通过；Codex stdio 握手已由 F53 单独验证。 |
| F55 | Codex 浏览器/设备码登录、账户/额度、环境缓存 | 🟡 | 按公开 schema 支持 browser/device-code start、login/cancel、account/read、rateLimits/read；loginId 隔离迟到事件，取消不被覆盖。runtime/path/distro 展示缓存分键，失败保留旧值、成功清错、身份清理和 20 项剪枝，禁止令牌字段。未启动真实登录/浏览器、未读取真实额度，账户 UI 未接。 |
| F56 | Windows 沙盒状态、显式初始化与 UAC | 🟡 | 按公开 schema 支持 windowsSandbox/readiness 和 setupStart elevated/unelevated；setup 必须 confirmed=True，活动 setup 防重入，完成事件按模式匹配，成功/失败与迟到事件隔离。未执行真实 UAC/沙盒初始化，设置 UI 和本机验收未做。 |
| F57 | 多 harness：Claude/Codex/OpenCode/Kimi 等 | 🟡 | **只有 opencode 有额外契约注入**；通用终端启动器仍按探测结果支持 claude/codex/opencode/kimi，全部没有容器隔离。Codex 已有独立 stdio 协议客户端，但尚未接入通用 Agent 运行链；其余 harness 没有 stdio 深度集成。用户收窄的是深度契约，不是所有启动按钮。 |
| F58 | 容器内隔离 CLI agent | 🟡 | `wsl-sandbox` PTY 必须绑定 case，真实 user/mount/net/PID namespace、nobody、无公网、staging 输出和 WebSocket 命令验证通过；不是 Docker 镜像/每用户长期容器，缺少独立 egress 代理、完整审计和并发 staging 一致性保证。 |
| F59 | `VIBEFLOW_<TOOL>` 工具发现与显式路径 | ✅ | 环境注册表覆盖 Python/Node/npm/pandoc/WSL/OpenFOAM 与 CLI agents，显式路径优先、未设置才 fallback、显式错误 fail closed；A–E/V 阶段最小检查接入 capabilities 和设置页，live 与负例通过。 |
| F60 | `$skill` 发现与弹窗 | ✅ | 目录/frontmatter 注册和 `$` 补全已有；`skill_smoke.py` 验证模型调用 get_skill 后，OpenFOAM 技能正文与 case-Re1000 示例进入下一次请求并驱动最终回答。 |
| F61 | 技能创建/更新/打包/安装与用户目录 fallback | ✅ | 内置 < 组织 < 用户覆盖，支持多组织 membership；用户/组织 admin 创建更新、ZIP 打包/安装、默认不覆盖和覆盖备份。安装限制 100 文件/1 MiB，拒绝穿越/绝对路径/符号链接；组织共享、用户覆盖、冲突和恶意 ZIP 均有测试。 |
| F62 | 六个技能的 `agents/openai.yaml` UI 元数据 | ✅ | 六个技能均有 interface display_name/short_description/default_prompt；解析与 `/skills` 返回一致并纳入 mtime 缓存。`$skill` 弹窗显示 VibeFlow Abaqus 名称/简介，选择后插入 `Use $vibeflow-abaqus...` 默认提示，浏览器通过。 |
| F63 | 原版 37 个技能覆盖 | ✅ | 当前 41 个技能文本覆盖原 37 并扩展 4 个；只证明文本存在。 |
| F64 | 各技能真实工具链与领域验证 | 🟡 | 只有 OpenFOAM/CadQuery/VTK/LaTeX 等部分验证；其余求解器、航空、MOOSE、OpenFAST、PPT 等没有逐项实测。 |
| F65 | PPT：可编辑 PPTX、图片重建、Beautify、模板、原生填充、187 presets、叙述/视频 | 🟡 | `/ppt:build`、版本化 spec、真实 worker、原生文本/形状/表格/图片、只读模板、独占输出和 Bearer 下载已验证，Open XML 与模板哈希通过。Beautify、187 presets、图片重建、复杂模板、叙述/视频和页面渲染验收仍未完成。 |
| F66 | 技能文本与当前实际能力的声明一致性 | 🟡 | VTK/VTU/VTP/VTM/XDMF/Exodus/STL/series 通用 reader 与 file_path viz 已有实测，相关格式声明已有可执行路径。仍需逐技能核对求解器安装、命令、参数和输出，不能把文本覆盖当作全能力一致。 |
| F67 | Agent 用户图片附件与图片消息显示 | ✅ | 支持选择/粘贴 PNG/JPEG/GIF/WEBP，最多 3 张/每张 4 MiB，校验 Base64 与 magic bytes。原图存 `.vibeflow/attachments`，事件仅保存元数据；OpenAI image_url 与 Anthropic base64 图像块均验证，浏览器预览及重载 Blob 显示通过。 |

**F 类不是“聊天已完成”：缺通用 web 工具、Goal、WSL Agent 命令、图片附件、会话取消/checkpoint/恢复、动态模型、长历史与显示控制、Codex stdio，以及 PPT 实际作业链。**

### G. CLI agent、终端与进程运行时

原版证据：`resources/terminal/index.html`、build/help/features 文档、agent-v2/codex 文档和 `VIBEFLOW_<TOOL>` 技能契约。CLI stdio、Codex 登录等已在 F 类列出，不重复计数。

| ID | 原版功能/子项 | 状态 | 当前实现与剩余差距 |
|---|---|---|---|
| G01 | xterm 终端、Fit/WebLinks、复制粘贴、resize 与历史 | ✅ | xterm6、WS PTY、滚动和基础交互已验证。 |
| G02 | 多本地会话、挂接、backlog 与显式关闭 | ✅ | 新建/重连/回放/关闭、所有者隔离和 15min 空闲回收均有实现。 |
| G03 | 快速输入与 IME 兼容 | ✅ | 多行快速输入可用；按原版实现 CJK 标点 beforeinput/compositionend 80ms 去重，普通中文/ASCII 不拦截。Node 硬断言和真实 PTY 双事件、中文及两个多行结果的滚轮历史验证通过。 |
| G04 | 2MiB pending、64KiB chunk、滚动跟随、浅色 SGR、输出时稳定光标 | ✅ | 前端有界 pending/queue 和 64KiB 分块有 Node 硬断言；真实 3MiB PTY 输出保留最终 marker，底部跟随、用户滚动优先、输出期稳定光标和浅色 SGR 均通过。修复 drain 忽略用户中途滚动的问题。 |
| G05 | codex-wsl compatibilityProfile | ✅ | `/runtime/cli-profiles` 提供 codex-wsl 的 WSL/UTF-8/PYTHONIOENCODING/TERM，CLI 启动器复用并切到 WSL；真实 WSL 变量和命令生成通过，不执行 Codex 或读取凭据。 |
| G06 | 终端截图/输出直接加入 Agent 聊天 | 🟡 | 终端“输出到 Agent”优先取真实选区，无选区取最近 20 行，写入共享 agentInput 并激活 ChatPanel；真实 PTY echo 已验证输入框同步。屏幕截图/图片输出仍未实现。 |
| G07 | QTermWidget 终端与选区 Markdown/LaTeX 预览 | ⏸ | 当前只保留 xterm 路线，QTermWidget 双终端及其预览未实现，属有意缩减。 |
| G08 | 全局 Light/Dark 主题与默认外观 | ✅ | 设置页持久化全局主题，根变量/dockview/常见内联面板/Monaco/终端/CAD 同步，重载保持并可恢复 Dark。Monaco 改本地 ESM/workers，消除 CDN 启动依赖；生产预览截图与交互断言通过。 |
| G09 | Setup：Python、WSL 发行版、OpenFOAM bashrc、路径转换与环境刷新 | ✅ | 组织 admin 可编辑 Python/pandoc/WSL/OpenFOAM 配置并原子持久化，非法路径/发行版拒绝、空值恢复默认；WSL 作业和 capabilities 使用新配置，路径继续 win_to_wsl 转换，浏览器原值保存和重探测通过。 |
| G10 | VFProcessSupervisor 看门狗 | ⏸ | 原方案计划以容器编排替代，当前无等价的统一看门狗；只有 PTY 空闲回收和作业孤儿状态修复。 |
| G11 | 崩溃上报 | ✅ | ErrorBoundary、window error/unhandledrejection 统一上报，5 秒去重；后端 append-only 脱敏限量 JSONL 与 admin 回读。真实浏览器双事件、空消息 400 和 Bearer 脱敏通过。 |
| G12 | 语音输入 | ✅ | 用户点击才创建/启动识别，转写进入 Agent 草稿，支持停止与 API 不可用提示；fake SpeechRecognition 验证点击前零启动、转写和停止，未启动真实麦克风。 |
| G13 | 运行时能力自检 | ✅ | capabilities 返回实际包/WSL/CLI 路径、环境注册表和 A–E/V 阶段检查；存储新增真实写读删/bucket 探测与 latency/error，非目录根不再误报 ok。Setup 编辑和硬隔离仍归 G09/F58。 |
| G14 | 原生 Helper 进程与按 Session 的取消/清理 | ✅ | 单后端进程内统一登记 Job/PTY/viz/Agent/Goal，活动列表按所有者过滤；按 case 一次取消并清理，返回 cancelled/remaining/errors。真实四类进程终态及回归通过；多 worker/容器隔离仍归 H18/F58。 |

**G 类主要缺口**：终端高级兼容、Codex 专用档、输出进聊天、全局主题、Setup/环境注册和统一进程监督。

### H. 作业、索引、补全与日志解析

原版证据：JobsDock、VFLogService、VFIndexService、v2406 静态补全 JSON、storage-maintenance-0.9.0.md。**VFIndexService 是工作区文件索引；v2406 补全 JSON 是 Studio 进程内静态资产，两者不是同一服务。**

| ID | 原版功能/子项 | 状态 | 当前实现与剩余差距 |
|---|---|---|---|
| H01 | 作业提交、列表、日志 tail、取消与 SSE | ✅ | 本地/WSL 作业、增量日志、SSE 和取消均有测试。 |
| H02A | 作业状态/exit_code/ended_at 落库 | ✅ | 作业结束状态和退出码有直连 DB 断言。 |
| H02B | 重启孤儿 queued/running -> interrupted | ✅ | 独立 uvicorn 使用临时 SQLite/存储/端口执行真实停止与重启，running 作业恢复为 interrupted 且 exit_code 保持空；测试清理自身进程，不操作开发 PostgreSQL。多 worker 协调仍归 H18/J14。 |
| H03 | 残差 initial/final、对数/线性、导出 PNG | ✅ | 真实确定性作业驱动 Ux/p 两个时刻；浏览器硬断言 2 条 initial 虚线、2 条 final 粗线、摘要与对数→线性坐标变化。实际下载 PNG 校验签名、1280×520 和 61,414 字节，UI 与导出图均已打开核对。 |
| H04 | TimeStep 多格式、Courant、FATAL/Warning、耗时 | ✅ | logparse golden 硬断言 OpenFOAM `Time =` 与 `TimeStep n: Time =`、科学计数、Courant、多个求解器残差、ExecutionTime、CRLF End、两类 FATAL/Warning 及未正常结束；修复第二格式把 `=` 当数值的问题，真实 job_summary 回归通过。 |
| H05 | 日志增量 fingerprint 与更多求解器格式 | ✅ | `/logs/parse?incremental=true` 以完整行 offset+SHA-256 前缀 fingerprint 续读，半行不消费，空增量可复用游标，重写/截断 409；真实 API 验证 DILU 续接，既有 smoothSolver/GAMG/PBiCGStab 与第二时刻 golden 保留。 |
| H06 | Job parsed_summary 自动结构化 | ✅ | 作业收尾按 case 读取真实日志并写回 `Job.parsed_summary`，`/jobs/{id}` 直接返回 schema/times/courant/residuals/summary；时刻、Courant、Ux 残差、警告、ExecutionTime 和 finished_cleanly 均有真实作业断言，Windows CRLF 的 End 也兼容。 |
| H07 | 工作区 FTS 建库、状态、文件名/内容搜索 | ✅ | 单 case FTS5 之外新增 `/workspaces/{id}/search`，轮转合并跨 case 命中并附 case_id/name、total/indexed 统计，跨用户 404；UI 支持当前算例/整个工作区，点击命中切换正确 case 并打开同路径文件，后端与浏览器硬断言通过。 |
| H08 | API 写删和作业后的索引增量/重建 | ✅ | 普通 PUT/DELETE 增量由 index_smoke 复验；独立 case 先建 FTS，真实 Windows 作业生成唯一 marker 文件，在没有 `/ws/events` 订阅的情况下收尾后即可搜索，直接证明 jobrunner 重建钩子生效。 |
| H09 | 外部进程/Agent 写文件的 watcher | 🟡 | 每 case 2 秒快照轮询产生 files.changed，联动 FTS 和 TreePanel.loadTree，断开立即退出；Agent/外部进程写文件可被发现。尚非原生事件或多副本总线，网络盘/跨进程延迟和一致性待验收。 |
| H10 | 索引构建事务、跨进程锁与半成品清理 | ✅ | 每 case building-UUID 锁，活跃构建拒绝，>600s 锁改存 stale 后恢复；BEGIN IMMEDIATE 全量替换、meta building_uuid、WAL/checkpoint 和 finally 锁清理。活跃锁原索引可读，index/events 回归通过。 |
| H11 | OCR 缓存维护、receipt 与旧缓存清理 | ✅ | `TexoReceiptStore` 以图片 data URL 的 SHA-256 绑定 receipt，localStorage/CacheStorage 双写与回读，索引保留最近 20 条并同步删除旧记录。25 条内存专项验证裁剪；真实 Edge 首次识别写 receipt、第二次显示缓存命中且 provider 仅调用一次，截图已核对。 |
| H12 | OpenFOAM v2406 静态补全（31,542 个去重 label） | ✅ | 合并器以原版 completionItems 为精确基准，输出恰好 31,542 个唯一 label 并记录 baseline_sha256；保留原版细分 kind，合入自有通用 kind/parents/contexts/confidence。重复合并 hash 相同，live 与六项补全回归通过。 |
| H13 | C++14/preprocessor、宏、byTag/registrations、compoundKeys、FoamFile、commandLine、environmentAndBuild 等分类 | ✅ | 按原版结构实现 languageSyntax/openfoamCpp、2,895 注册和 13 个非空 runtime tag、275 compound keys、12 FoamFile class、428 规范化命令行 token、environment/wmake；二进制乱码与编译器 -W 误分类已排除，最小 golden、真实连续构建和 live kind 回归通过。 |
| H14 | dictionaryValuesByKey、父键上下文值补全与域探测 | ✅ | completionItems 增加 parents、文件 contexts 和 sourceExamples，dictionaryValuesByKey 记录 occurrences/来源；API 支持 parent+context。controlDict/U 同 key 不同值、fixedGradient 实际上下文、未知 parent/context 均通过，完整物理类型推断不在此声明。 |
| H15 | source-backed 优先级、confidence 和可复核来源 | ✅ | v2 生成器按证据类型赋 confidence 并以 confidence/occurrences/label 排序；API 返回 source、生成器 SHA-256 和去时间戳的确定性 index SHA-256。临时语义变化测试及真实 v2406 连续两次构建均验证，50,409 项 live 回归通过。 |
| H16 | `/ws/events`：files.changed、jobs.updated、runtime.degraded、scene.available、quota.warning | ✅ | 单进程用户级有界扇出与文件轮询并存，按资源所有者推送；jobs.updated、scene.created/closed、quota.warning 429、runtime.degraded（gmsh 缺失）均有真实 WS 证据，其他用户收不到。满队列丢最旧，关闭即退订；多副本总线仍归 J14。 |
| H17 | 大文件分块上传、断点续传与多段 Range | ✅ | 普通上传限 16 MiB；fs 分块会话支持 offset 断点、8 MiB chunk、1 GiB size、SHA-256 完成校验、取消和原子发布。raw 支持最多 16 段 multipart/byteranges、HEAD 一致长度、重叠/非法 416，全部专项及单段回归通过；自有 API 不宣称 tus/S3 协议。 |
| H18 | 作业资源硬限制、公平调度与多 worker | 🟡 | JobProcess 默认 24h 墙钟和 16MiB 日志上限，API 可收紧；真实超时/截断/普通作业验证，批量队列有用户配额公平。cgroup CPU/内存、进程组可靠终止和多 worker/跨副本调度仍未实现。 |
| H09A | 上传、caseDict、Agent 写入的即时索引增量 | ✅ | caseDict 统一写入、multipart 上传和 Agent 普通文件/提案写入均调用 touch_file；独立 case 建索引后，三条路径不连接 WS、不等待即可按唯一 marker 搜索。proposal/case_events/casedict_sync/index 回归通过；多副本仍归 J14。 |
| H19 | 历史作业 ended_at API 完整性 | ✅ | `_job_out` 在无内存进程时回读 DB `ended_at.timestamp()`；真实作业在独立 ASGI 进程的详情和列表均返回持久化时间。重启后 live 历史作业及 jobs 全链回归通过，活跃进程行为不变。 |
| H20 | 通用 Jobs API 高危命令过滤 | ✅ | Agent 与 Jobs 共用 `command_safety`；Jobs 在配额、DB 记录和进程创建前拒绝高危模式，返回 400 且无 Job 半成品。普通提交/日志/取消/SSE/派生回归通过。 |
| H21 | 无扩展名 OpenFOAM 场文件语言识别 | ✅ | `fileLanguage.ts` 按 basename/父目录判定，覆盖 0、0.x、0.orig、带点场名和特殊字典；Node 行为测试、正式 build 及浏览器实际打开 0/U 均验证为 openfoam-dict。 |

**H 类主要缺口**：v2406 富补全、外部 watcher、索引/缓存维护、统一事件总线、分块上传和分布式作业。

### I. 转换器、公式 OCR 与其他独立工具

原版证据：`resources/flotherm2foam`、`docs/flotherm2foam.md`、`docs/scnet-workbench.md`、texo。Flotherm 的 Studio 与 CLI 能力有明确差异；SCNET 是可选编译功能，公共版默认关闭，0.11.0 是否启用未知。

#### I-1. Flotherm2foam（当前整体未接）

| ID | 原版功能/子项 | 状态 | 当前实现与剩余差距 |
|---|---|---|---|
| I01 | Studio 输入、配置、预检、异步转换、日志、打开输出与取消 | 🟡 | `/flotherm:inspect` 与 `/flotherm:convert` 已形成真实异步作业，IR/settings→case+Allrun+报告、日志/文件树产物、归属和非空目录保护通过；取消复用作业杀进程并保留部分文件。完整 Studio 窗口、CLI inspect/export/import/validate/build/check 与库管理未实现。 |
| I02 | ECXML、V2310 PDML、ECXML+PDML、XTXML/XTXMLA+PDML 输入组合 | ✅ | 真实文件格式/32MiB/重复输入检查、同名配对、V2310、SHA-256 和 XML bbox 重复审计均已验证；Pack CLI 可接受、Studio 明确 blocker。仅覆盖输入检查，不代表部件/物理转换已实现。 |
| I03 | Pack/FloSCRIPT 路径及 Studio/CLI 覆盖差异 | 🟡 | Pack/FloSCRIPT 均可由 CLI 输入检查识别，Studio 明确拒绝；force 复用 I10A 生成标记保护。FloSCRIPT 阶段执行和 keep-stage 尚未实现，不把路径识别当成转换完成。 |
| I04 | box/cylinder/凸棱柱/四面体、薄板/PCB/2R/PQ 风扇/阻力/散热器/外壳等映射 | 🟡 | 11 类部件有确定性几何/工程等价映射，PQ 曲线/退化四面体/缺尺寸等进入 missing-info；golden 通过。尚未生成真实 CAD/网格，复杂原始 XML 部件识别仍待接入。 |
| I05 | Monitor/Region、总功率热源和转换覆盖报告 | 🟡 | IR 可生成 `conversion-coverage.json/.md`：逐对象映射/原因/缺字段、region monitor、输入/已映射/未映射功率和 balanced；11W=7W+4W golden 通过。尚未接真实转换流程或求解后热平衡验证。 |
| I06 | settings.json：domain/materials/objectOverrides/power/refinement/parallelSubdomains/fan/resistances 等 | 🟡 | 白名单解析、材料/对象覆盖、W/mW/kW 功率、refinement、domain bbox、并行分解、merge/skewness/scotch/startFrom 与 blocker 已验证，来源哈希和原 IR 不变。尚未接转换任务，fan/resistances 具体物理映射仍归 I04/I10B。 |
| I07 | IR：manifest/objects/materials/assembly/associations/coverage/monitors/source | 🟡 | 新增 `vibeflow.flotherm.ir.v1` 构建/校验/原子读写，含来源 SHA-256、材料导热、assembly 引用/循环、四类覆盖、功率和 monitor 目标；真实 roundtrip/负例通过。尚未由 ECXML 转换器自动生成或驱动 OpenFOAM case。 |
| I08 | OpenFOAM case、case-manifest、required-overrides、power-balance、preflight/mesh_gate | 🟡 | IR 可生成 laplacianFoam blockMesh/topoSet/fvOptions/0/T、manifest、覆盖与预检；WSL v2406 blockMesh+topoSet 和 polyMesh 实测通过，非 box/缺 rho_cp 写 required-overrides。尚未运行 laplacianFoam 或形成转换任务 API。 |
| I09 | 辅助库检测、复制源码、wmake libso、FOAM_USER_LIBBIN 安装 | 🟡 | `/flotherm:build-library` 校验 Make/files+options/.C、限制复制且默认不覆盖，source v2406 后 wmake；真实 libflothermHelper.so 安装和 CDLL c_double 调用通过。原始辅助库完整 API/链接依赖与安装回滚未覆盖。 |
| I10 | Allrun.pre/solve/serial/并行/Allclean 及输出覆盖/取消语义 | 🟡 | 五类脚本已生成；真实 v2406 Allrun.serial 执行 blockMesh/topoSet/laplacianFoam 到 time=100，Allclean 保留 system/manifest。volumeMode、对称 PCG/DIC 和定温散热边界经实测修正。并行未跑，转换任务/强制覆盖/G6 对拍未接。 |
| I10A | CLI `--version` 与带生成标记的 `--force` | ✅ | `flotherm2foam.py` 输出版本并安全准备输出；仅当现有文件与 marker 登记完全一致时才删除，未登记文件、无效 marker、穿越/链接均预检零删除。Studio force/keep-stage 服务层拒绝。真实 golden 通过；转换本体仍归 I01/I03。 |
| I10B | open/channel 边界、laminar/realizableKE RAS、各向同性/正交各向异性导热 | 🟡 | 单一各向同性材料使用真实 DT 并通过求解；正交各向异性和多扩散率有 required-overrides，不再静默压成统一标量。open/channel 边界、RAS 场/模型、张量扩散和分区域 DT 求解仍未实现。 |
| I10C | 区域温度/wallHeatFlux/channel 流量/probes 与转换期功率平衡 | 🟡 | controlDict 生成 region volFieldValue、wallHeatFlux、probes；真实求解生成 volFieldValue.dat 和 probe T，channel-flow 对纯热 case 明确 required-override。转换期 power-balance 已有。wallHeatFlux 独立断言、binary write 和求解后热平衡未完成。 |
| I10D | case/IR 输出目录保护：不同、不嵌套且为空；取消保留部分输出 | ✅ | `validate_conversion_dirs` 拒绝相同/嵌套/非空/链接，`CancellationToken.checkpoint` 写取消记录并保留部分产物，非空目录禁止直接重启；真实临时目录 golden 通过。异步转换任务接入仍归 I01/I10。 |

#### I-2. SCNET 远程资源工作台（原版启用状态未知）

| ID | 原版功能/子项 | 状态 | 当前实现与剩余差距 |
|---|---|---|---|
| I11 | SCNET 账户概览、区域/服务发现、令牌续约与注销 | ❓ | 原版可选编译、默认关闭，0.11.0 启用证据待补。B/S 已接官方账户认证、区域发现、账户概览、凭证续约及本站注销，待授权 SCNET 云端验收。 |
| I12 | 实时/历史远程作业、搜索分页、详情、提交与终止 | 🟡 | B/S jobs.list/history/detail/submit/cancel 经签名 mock HTTP 验证；工作台已有作业搜索分页、详情、提交和终止，真实 Edge mock 流程通过。真实调度器队列、完整分页字段和账户联调未完成。 |
| I13 | 远程文件上传/下载、目录 ZIP、新建目录、重命名、复制、移动、删除 | 🟡 | files 全生命周期有 mock HTTP、URL 解码和浏览器验收；普通文件内容与目录 ZIP base64 分别验证，变更后列表刷新。流式大文件、下载覆盖保护和真实远程文件系统未验收。 |
| I14 | 远程容器 list/detail/create/start/restart/stop/delete/exec/open | 🟡 | 容器全生命周期和 open/exec 通过签名 mock；工作台创建/启动/重启/exec/停止/删除及状态刷新通过真实 Edge。真实资源组/镜像/规格加载和容器打开未联调。 |
| I15 | Notebook create/start/stop(save env)/release/rename/detail/open | 🟡 | Notebook 全生命周期、save_environment、重命名和相对 open URL 通过 mock；工作台创建/开机/关机存环境/重命名/释放已做浏览器验收。真实 Jupyter 镜像、资源选择和打开验证未完成。 |
| I16 | 远程桌面 list/filter/create/detail/open/delete | 🟡 | Desktop list/detail/create/open/delete 通过 mock；工作台唯一资源筛选、详情、打开链接和删除后行消失通过真实 Edge，open URL 只允许 HTTP(S)/相对路径。真实 noVNC 画面与会话未联调。 |
| I17 | AK/SK HMAC、区域凭证、可选 DPAPI 保存、脱敏和自动连接 | 🟡 | B/S 已接官方 HMAC、按区域签发的凭证、加密会话和续约版本校验。既有 Windows DPAPI 保存与恢复有本机回执。真实 SCNET 账户和各区域续约待联调。 |
| I18 | 中文语义化详情、分组折叠、字段搜索、单项/摘要复制与脱敏 | 🟡 | 中文 label、时长/分辨率、overview/advanced 分组、搜索、摘要复制和敏感字段脱敏有 golden；工作台详情卡支持高级展开、字段搜索与复制，作业/桌面详情已进入浏览器验收。真实账户完整字段和分组保真仍需联调。 |
| I19 | 同源重定向、HTTPS、超时、旧回调失效、取消不回滚、变更确认和不自动创建付费资源 | 🟡 | 同源重定向/HTTPS、10 秒超时、session generation、confirmed 变更门控已验证；传输异常受控映射 502，不自动重试或创建付费资源。真实取消语义、UI 二次确认策略和账户生命周期未验收。 |
| I20 | HPC `/hpc` 与 AI `/ai` 根地址补全、msg/message 错误解析和请求头诊断 | ✅ | 根地址仅在空 path 补 hpc/ai，已有/自定义路径幂等；HTTPS/回环限制、data/list/fileList/records、嵌套 error、msg/message、非 JSON、query 脱敏、token/字段长度诊断和 400/431/494 提示均通过。相对访问链接也有协议/凭据检查。 |
| I21 | 原版明确未提供的高级远程管理 | ⏸ | 成员管理、异步目录复制、分片续传、容器规格热更新、自定义 Notebook 服务、模型镜像市场在原版也标为无专用交互，不能算当前缺失。 |

#### I-3. 其他工具

- Texo 离线 OCR 已在 E19–E20 分项；MinerU 在 E12 按用户决策不接；PDF 页码已在 E13 验证；PPT 在 E21/F65 为部分实现，不是证据不足。
- Flotherm2foam 原始外部程序尚未完成逐文件接入，不能把“通用 shell 可以手动执行命令”写成该工作流已接。SCNET 已有 B/S API 和 mock 浏览器工作台，但真实账户与远程资源仍未联调。

**I 类是此前记分卡明显低估的区域：Flotherm 是一整条独立转换产品链，SCNET 也需要作为独立可选工作台核对，而不是一句“转换器未接”。**

### J. 设置、账号、组织、许可与运维

原版证据：Setup/授权状态/更新与辅助迁移审计。**Studio OAuth 没有直接证据；Ed25519 单机 key 目前也只是辅助结论。** J 类同时单独标明 B/S 新增生产要求，避免全部算到桌面版头上。

| ID | 功能/子项 | 状态 | 当前实现与剩余差距 |
|---|---|---|---|
| J01 | 邮箱/密码、JWT 和用户状态 | ✅ | 注册/登录/active 用户和 Bearer 已有，属于 B/S 新增能力。 |
| J02 | Studio OAuth | ❓ | 原版指定材料未确认 Studio OAuth；当前没有 OAuth。Codex 登录和 SCNET AK/SK 都不是 Studio OAuth 的证据。 |
| J03 | Ed25519 单机 key / 离线许可 | ⏸ | 原版算法只有辅助结论，设备/激活细节未知；当前改用订阅，离线文件验签未实现，按路线变更处理。 |
| J04 | organizations/memberships/plans/subscriptions | ✅ | 个人组织、admin membership、free 订阅及回填已有，属于 B/S 新增能力。 |
| J05 | seat 管理、成员/角色/admin console | ✅ | admin 可邀请已注册用户为 admin/member/viewer，邀请预留 seat，接受/拒绝/撤销、禁用释放、重新启用容量检查和最后 active admin 保护均有真实 API 验证；Settings 提供成员/seat 控制台，双账号 Edge 邀请→接受→viewer→disabled 通过。free 默认 2 seats；组织资源 ACL 归 J07，seat 计费归 J06。 |
| J06 | 付费套餐、支付和订阅生命周期 | 🟡 | free/pro/team 目录、mock checkout、续费、升降级 seat 检查、取消、历史和到期失效均有隔离真实 API 验证；Settings 可切换/续费/取消，Edge 通过，审计记录 mock payment ID。明确不真实扣款；支付渠道/webhook/发票、seat 计费和周期结束取消未完成。 |
| J07 | 组织级 workspace/case 授权与协作 | 🟡 | owner 可按组织分享 workspace，viewer 只读、member 可写；共享列表/case/tree/file/raw 与写接口角色拒绝、非成员和撤销均有主 PG 验证，浏览器共享列表通过。其他 GET/后台任务/组织 admin 代理管理矩阵未逐项覆盖，资源仍归 owner，不是完整多 org ACL。 |
| J08 | viz/job/LLM/storage 四维配额 | 🟡 | 代码有四类检查点，本轮 viz/job/LLM/storage 429 与套餐还原均通过；设置页仍只有作业/LLM/存储三条进度条。几何/报告/文本写/Agent 写/翻译/压缩等入口覆盖不全，expires_at 未参与过期执法。 |
| J09 | 组织级 provider profile 与 KMS | 🟡 | owner+org admin 可共享/撤销 profile；active 组织成员在列表和服务端 models/翻译/Agent 会话使用，owner-only 修改删除，非成员 404，密钥/密文不回显。主 PG 与既有 profile/security 回归通过。仍是本地 Fernet vault，不是 KMS 包裹、组织密钥轮换或独立密文所有权。 |
| J10 | OIDC/OAuth 登录 | 🟡 | 通用 authorization code+PKCE S256、单次 state、discovery/token/userinfo、verified email 建号/关联和平台 JWT 已由本地 mock provider API+真实浏览器验证；登录页/回调可用。真实外部 IdP、nonce/ID Token 校验、多 provider、多实例 state 共享尚未完成。 |
| J11 | 审计日志、命令/审批/配置变更审计中心 | 🟡 | 组织私有 JSONL 哈希链已接入成员邀请/接受/拒绝/撤销/角色、Setup 和单/批作业提交；admin 可按 action/actor 分页查询，Settings 审计中心显示链状态。命令只存 SHA-256，篡改测试可检测；浏览器通过。审批/取消等来源尚未全覆盖，单进程文件链不等于多副本 WORM/外部锚定。 |
| J12 | 备份、恢复与升级回滚演练 | 🟡 | workspace 备份/恢复和 PostgreSQL custom-format 全库备份→临时数据库恢复校验均已实测；新 ID 映射、坏包回滚、活动拒绝和核心表验证通过。实库 downgrade/升级回滚未执行，不能据此宣称全量回滚闭环。 |
| J13 | Alembic 建库与实库纳管 | ✅ | 临时库 upgrade/downgrade/再 upgrade 全通过；实库先完成 custom-format 备份和临时恢复校验，SCHEMA_DIFF_COUNT=0 后执行 `stamp head=6fb6c18fb7ee`，current 为 head，未删除业务数据。后续升级仍需逐版本备份评审。 |
| J14 | Redis/arq worker、事件扇出和多副本 | 🟡 | 独立 Redis 7.0.15+两个 arq 进程实测 dequeue、只执行一次、日志文件回读、DB 状态和 Redis cancel；API 可显式启用分布式作业。完整事件总线/PTY/viz 多副本、滚动恢复、负载均衡和跨主机验证未完成。 |
| J15 | S3 全链路文件事实源 | 🟡 | SeaweedFS 实测 case 输入、唯一作业 staging、SHA-256/远端版本预检、不同产物并行、同路径冲突、删除保护、幂等重试和成功清理；日志回读、PTY/viz 关闭同步与索引重建/搜索均有通过证据。跨进程/跨副本原子提交与分布式锁、PTY/viz/索引等长任务一致性和故障恢复仍未完成。 |
| J16 | 每用户容器硬沙箱、cgroup、非 root、默认无公网 | 🟡 | `wsl-sandbox` 使用 pivot_root+user/mount/net/PID namespace，nobody 执行、宿主路径不可见、默认无公网，staging 可写并同步产物；真实 API/主 PG 验证。cgroup 计量、并发 staging 隔离/一致性和所有执行入口接入仍未完成。 |
| J17 | Docker/compose/nginx 生产部署 | 🧪 | 有候选文件和离线检查；引擎恢复不等于目标机镜像构建/业务验收完成。仍需核对 migrations/pandoc、Linux 执行器/PTY、vault 持久化/KMS，并实际运行完整业务、升级与回滚。 |
| J18 | TLS、CSP、安全 header、CORS 与 query token 日志策略 | 🟡 | API 成功/错误均有 nosniff、SAMEORIGIN、no-referrer、Permissions-Policy 和窄 CSP，恶意 Origin 负例通过；nginx 前端 CSP 与只记录 `$uri` 的 noquery 日志格式、生产 uvicorn `--no-access-log` 有离线 compose 检查。TLS 终止、nginx 实际 CSP 运行和短期窄 scope WS/viz ticket 未验收，现有 query JWT 仍需替换。 |
| J19 | 用户×provider 速率限制与统一限流 | 🟡 | 单进程 token bucket 按用户/IP×API/LLM 类别隔离，真实低限额验证 429、Retry-After、安全头、不同用户和类别互不影响；prod 限额可配置。尚未精确到 provider profile，也不是 Redis 跨副本全局限流，转发 IP 信任策略和全入口计费仍待完成。 |
| J20 | 集中日志、指标、告警与运行时监控 | 🟡 | admin `/runtime/monitor` 汇总真实 DB/存储延迟、DB 作业状态、supervisor 进程、限流 bucket 和当前告警；`/runtime/metrics` 输出 Prometheus 文本，Settings 监控 UI 与 Edge 验证通过。外部告警投递、集中日志后端、多副本聚合和历史时序仍未实现。 |
| J21 | 性能负载、安全渗透和 50 用户验收 | 🟡 | 50 用户 REST、PTY、可视化和真实 DeepSeek 混合负载已有本机回执。REST 按 2026-11-25 决定记录成功率与延迟，原 300 ms 仅作历史比较。其他指标及验收边界见 [FF-J21 回执](FF-J21-rest-load.md)。 |
| J22 | CI/CD 与每日 replay/真实模型回归 | 🟡 | runner 默认 offline 10 项、隔离 core 14 项和独立 CAE 3 项及 summary.json；本轮全部通过，Node 离线 25 项/build/Edge 报告阅读验收通过。CI 修复未启动 API 的 schema 依赖，配置纯离线+独立 API 检查。远端 CI 未运行，完整矩阵和每日真实 LLM replay 未配置；没有执行遗留全集。 |
| J23 | 精确依赖锁与发布 SBOM | 🟡 | npm lock 可作为种子；Python 只有直接约束，Docker 基础镜像无 digest，最终镜像 SBOM 未生成。 |
| J24 | 客户端自动更新器 | ⏸ | 当前有意砍掉；原版只有 stable endpoint 和更新入口的辅助证据，完整生命周期未知。 |
| J25 | CC Switch/契约配置备份与多 harness 切换 | 🟡 | 当前只有 opencode 契约注入和 `.bak` 备份，未覆盖多 harness 配置切换；通用终端启动器仍枚举多个 CLI。 |
| J26 | OpenAPI/Swagger 与结构化诊断 | ✅ | `/docs`、`/openapi.json` 已验证，当前 65 个路径均有手工文档对应（含 postprocess）；属于 Web 新增便利能力。 |
| J27 | 单段 Range/206 与真实 fs 下载 | ✅ | 当前新增能力，含 HEAD、suffix 和 416；不是原版已有功能的简单复刻。 |
| J28 | S3 自身的 Range/HEAD/presign/下载 smoke | ✅ | `s3_range_smoke.py` 在真实 SeaweedFS 4.48 验证 closed/open/suffix Range 精确字节、非法/越界 416、完整 GET、HEAD/Range HEAD 空 body 与长度、raw 平台鉴权，以及 presign 带/不带平台 Bearer 下载完整 256 字节。 |
| J29 | prod 默认存储与当前本地执行链的确定性兼容 | ✅ | prod compose 默认持久化 fs，SeaweedFS 移到可选 profile；显式 S3 在未设置 `VIBEFLOW_ALLOW_PARTIAL_S3=true` 时于 DB 初始化前拒绝启动。compose 离线检查和真实子进程启动/临时 DB 不创建均通过。 |

**J 类最大缺口是生产化：组织授权、seat/admin/OIDC、审计、硬沙箱、S3、多副本、TLS/限流、监控和真实部署验收。**

### K. 有意不做、替代实现与 Web 超出版

| ID | 类别 | 项目 | 当前口径 |
|---|---|---|---|
| K01 | 有意裁剪 | Catalyst/Extractors、Comparative/Memory/Material Inspector、协作面板 | 已明确不做；细节待补证，不混入“还没来得及实现”。 |
| K02 | 有意裁剪 | 28 插件长尾、ParaView 宏菜单、QTermWidget 双终端 | 采用有限管线和 xterm，不承诺原生插件/宏/双终端覆盖。 |
| K03 | 有意裁剪 | 客户端自动更新器 | 不做；原版 stable endpoint 存在，但完整更新生命周期未知。 |
| K04 | 用户决策 | MinerU | 不接；PDF 阅读与划词翻译覆盖当前阅读场景。 |
| K05 | 用户决策 | 多 harness | 收窄为 opencode 的深度契约注入；通用终端仍保留多 CLI 启动按钮，其他 harness 无深度协议集成，不能写成“只剩 opencode 可启动”。 |
| K06 | 路线替换 | 单机许可 → 账号/组织/订阅 | 当前已实现订阅骨架，离线 Ed25519 文件验签未实现；不是“授权绕过”。 |
| K07 | 路线替换 | Qt/QWebChannel → React/REST/SSE/WS | 主壳已 Web 化；不追求 Qt 进程和 IPC 形状完全一致。 |
| K08 | 路线替换 | MathJax → KaTeX、QPlainTextEdit → Monaco | Markdown/公式和编辑器体验已替代，Monaco/OpenFOAM 补全在部分面超出原版。 |
| K09 | 原版已删除 | TikZ 可视编辑器、独立 Codex 聊天页 | 不计入 0.11 缺失；普通 LaTeX 与统一 Agent 页面仍保留。 |
| K10 | 当前新增 | 41 个技能文本 | 原 37 个文本均有对应自有重写，新增 `vibeflow-hypermesh`、`vibeflow-openvsp`、`vibeflow-viz`、`vibeflow-jobs` 四项；新增文本不等于工具链验证。 |
| K11 | 当前新增 | Alembic、组织/配额、归属安全、OpenAPI | 这些属于 B/S 生产化新增，不计入原版功能缺失。 |
| K12 | 当前新增 | 单段 Range/206、HEAD、真实 fs 下载、caseDict DB 镜像与统一 canonical 写入 | 当前新增或强化的工程能力，已用专项测试验证。 |

**关于“100% 对等”的边界**：本台账只对照 0.11.0。被原版删除的 TikZ、尚未证实的 Studio OAuth/更新细节、可选编译 SCNET，以及新 B/S 目标必须分开讨论，不能用总数硬凑一个对等率。

## 遗漏复核

本次已分别检查原版 Agent/工作流、原版桌面/可视化、当前实现，并再做一轮只找漏项/误判的复核。A–K 顶级领域均已覆盖，没有新增顶级漏项；随后补入了原始 16 块、JobsPanel runtime、S3 专项验证、技能声明一致性和 prod 默认 S3 的确定性断裂。2026-10-25 再补入 A25 外部 case 打开、B21 内联导出抑制、B22 Zebra、B23 几何参数注入和 F67 Agent 图片附件，并纠正 A02 Open Editors、errors 硬门控、Bearer 下载、quota 复验和 v2406 环境的当前状态。

### 审计时发现的“完成”误判与当前状态

- **已修复**：多工况派生曾漏拷初始 `0/`；当前精确保留初始场并实际运行派生工况（C11）。
- **已修复**：preview 曾自动调用 `export_geometry`；当前该钩子只在显式 export 分支运行（B19）。脚本顶层内联 `cq.exporters.export` 的 preview 抑制也已完成并通过正反测试（B21）。
- **已修复**：Monaco 的 caseDict 保存曾误走普通 `/file`；当前走专用 `/caseDict` 并刷新镜像（A19）。
- **已修复**：普通二进制 `<a href>` 曾缺少 Bearer；当前改为 Bearer Blob 下载（A21）。HTML/DOCX 专用阅读器及原件下载见 E22。
- **仍缺**：`__file__` 和 `VIBEFLOW_PARAVIEW_SCRIPT` 未注入，`ViewSize` 未禁止，scenes.py 上下文不能称等价。
- **部分完成**：设置页只有作业/LLM/存储三条用量条；本轮 viz/job/LLM/storage 四类 quota 均已复验，其他写入口与到期执法仍不完整。
- **仍缺**：reasoning.delta 有后端代码，但前端不处理、assistant 不落库；长历史没有自动 token/window 管理。
- **仍需补证**：多个历史探针只有打印或截图，没有硬失败断言；最新主 E2E 的 PDF 区域为空，不能当作 PDF 全绿。
- **已补齐脚手架**：新建 case 已生成原始 16 个 caseDict 顶层块，既有文件不覆盖；regions/interfaces/provenance/extensions 的专用语义、校验和生命周期仍未闭环。

### 仍需保留的未知/补证边界

- Sources/Filters 全菜单、28 插件名称与默认加载状态、完整 Color Map/Animation 面板细节。
- Studio OAuth、Ed25519 设备许可细节、自动更新生命周期；Codex 登录和 SCNET AK/SK 都不能替代这些证据。
- SCNET 在本安装包是否启用及真实账户联调结果；它只能作为可选编译工作台单列。
- update_goal 参数/预算/停止条件、web_search 提供商和 schema、会话导出格式、图片附件编码方式。
- 独立多 harness 的实际可运行名单、容器隔离边界、多工况批量控制、补全域探测和 v2406 重建入口。
- 外部求解器是否安装和可用不能由技能文本推定；商业后端也只能算接口预留。

### 建议实施顺序

#### P0：先修确定的行为缺陷和核心工作流

1. ~~派生保留初始 `0/` 场并实际运行派生工况~~（已完成：`0/U`、`0/p` 保留，`0.1/1/` 等结果目录跳过，icoFoam 实测成功）。
2. ~~修复 preview 自动调用 export 钩子及脚本内联导出的副作用~~（B19/B21 均完成：preview 不调用导出钩子，也不产生内联导出；显式 export 行为保持）。
3. 修复 caseDict Monaco 保存、块内注释保真和阶段硬门控；补齐 16 块脚手架及 case_events（**部分完成**：Monaco 保存、块内保真、errors.open/blocking 硬门控和 16 块脚手架已验证；case_events 待做）。
4. ~~修复普通二进制下载按钮的 Bearer 问题~~（已完成：Bearer Blob 下载、文件名、objectURL 释放及 DOCX 实测）。
5. 修复 OpenFOAM 环境（wmake、foamEtcFile）（**已完成环境**：官方 v2406 开发包与 2D/3D golden 均通过；后续 P1 的最小 functionObject D1–D5/G5 切片亦已完成，通用 UDF 工作流仍待做）。
6. ~~给 JobsPanel 增加明确 runtime 选择，验证 WSL 提交与取消~~（已完成：JobsPanel runtime 下拉选择、WSL `pwd` 和 `/mnt/` 路径实证；Agent `run_wsl_command` 是独立的 F24 缺口，列入 P1，不在本项虚写完成）。

#### P1：补主要 CAE 与高频功能

7. 在已安装的 v2406 与最小 UDF D1–D5/G5 切片基础上，统一 runtime/作业环境，补通用 UDF 阶段工作流和更多类型、MPI、snappy/gmsh、pitzDaily/damBreak 等黄金用例与参考场对拍。
8. 实现 flotherm2foam 的 Studio/CLI 主链及 G6 逐文件对拍，保留输入/输出/取消/覆盖边界。
9. 补 Agent WSL runtime/`run_wsl_command`、`web_search/web_fetch/update_goal`、Goal 循环、会话取消/checkpoint/恢复、reasoning 显示与落库、图片附件、消息复制和长历史治理。
10. 实现专用 postprocess 执行器、指标/探针/积分/频谱、出版级图件和证据追溯。
11. 扩展 ParaView：Sources/Filters、server-manager 面板、Python console、自由色图、关键帧和通用 VTK/VTU/XDMF 阅读器。
12. 扩展几何/CAD：caseDict 参数注入、cadscene、树/选择、测量、剖切、Zebra、爆炸、CAD 动画、截图与导出按钮。
13. 实现 PPT/PPTX 实际生成/编辑链、模板和产物验证，而不是只保留技能文本。
14. 补报告表格生成器、LaTeX 错误定位/PDF 回跳、PDF 页码和 DOCX 阅读器；验证 pdflatex/lualatex 与 biber。
15. 重建 v2406 补全，补上下文值/域探测；无扩展名场文件语言识别已完成，不再列入待做。

#### P2：团队与生产化

16. prod 已默认持久化 fs 并对未受限声明的 S3 启动 fail-fast；后续修通 Agent/Job/index/terminal/geometry/viz 的 S3 一致性，再允许完整生产切换。
17. 接入硬沙箱、Redis/worker、共享事件总线和作业/会话跨副本模型，补全配额/到期/全入口限流。
18. 补 seat/admin/OIDC、组织 ACL、审计中心、组织 profile/KMS；支付和离线许可按实际产品决策处理。
19. 完成目标机部署：API 镜像补 migrations/pandoc/Linux PTY/vault 持久化，再做真实 CAE/viz/WS/备份恢复、TLS、监控和负载渗透验收。

#### P3：按用户需要再开启

- SCNET 远程资源工作台、商业求解器、Catalyst/插件长尾、协作面板等，只有在明确需要时重启，不建议挤占 P0–P2。

**结论口径**：不要用“已实现 41 个技能”“真实 CFD 跑通”“测试套件通过”推导全功能对等。它们只证明相应文本、主链和断言存在。当前最可信的是本机 fs、统一 caseDict 后端、CadQuery、icoFoam 2D/3D、基础作业/终端、mock Agent、基础 VTK/trame、pandoc/LaTeX、每算例 FTS 和单段 Range。

2026-11-18 全功能交付对账：FF-D15的离线双后端契约经原条件逐项审查及Linux规模/并发/权限/取消/重启补充验收后整项关闭，见[FF-D15完成审查](FF-D15-completion-review.md)。原审计表与其余功能范围保持；全功能3/295、剩292。

2026-11-18 全功能交付对账：FF-F54按原始发现契约整项验收；真实命令、当前公开能力API、部署与DeepSeek回合通过，见[FF-F54完成审查](FF-F54-completion-review.md)。保留其他任务范围；全功能4/295、剩291。
