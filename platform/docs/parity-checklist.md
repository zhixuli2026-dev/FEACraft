# 功能对等清单：VibeFlow Studio 桌面版 vs Web 版

> **当前执行入口（2026-11-02）**：[下一阶段主线计划](next-stage-mainline-plan.md) 与 [全功能交付计划](full-function-delivery-plan.md) 已承接全部 295 项。下方是历史领域摘要，旧“暂缓/不做”不再自动排除功能；最新范围、逐项任务和验收按新计划，当前证据按审计与进展。

> 当前复核：2026-10-27（295 个功能项 + 12 个决策/替代项全面复核）。对照 `REPORT.md`、原版机制审计和当前 Web 代码。
> 状态只表示证据覆盖，不声称桌面版全部细节等价：✅ 主链路已验证｜🔶 部分实现/有明显长尾｜⬜ 未实现｜⏸ 已明确缩减或暂缓｜❓ 原版或当前证据不足。
> 不再使用缺少统一量纲的主观百分比。**300+ 项逐条审计见 `feature-gap-audit.md`，本表只作领域摘要。**

## 一、当前记分卡

| 领域 | 状态 | 当前证据与实现 | 剩余差距 |
|---|---|---|---|
| 项目/文件管理 | 🔶 | 文件树、Open Editors、外部 StandardCase ZIP 导入、`@path` 行列附件、Windows/Linux 路径格式、workspace/case 重命名/移动/深拷贝、上传/删除、Markdown、图片/PDF/CSV、HTML/DOCX 安全阅读器、外部修改冲突检测、单进程 `files.changed` 树刷新、raw 字节流与单段 Range/206、HEAD、Bearer Blob 下载 | 删除/软删除恢复、专用文件路由、多段 Range 和超大文件性能未验收；超大树性能待评估 |
| caseDict 工作流 | 🔶 | 后端唯一文件事实源、状态机、整写/块写、DB 镜像、非法输入拒绝、REST/Agent 统一写入和变体保护；Monaco 保存、块内注释保真、errors.open/blocking 硬门控、16 块脚手架、v1 枚举/status 映射和 errors.entries 生命周期已验证 | 字段齐备度、16 块专用语义、自动 provenance 和完整 OpenFOAM 方言未实现；`case-events.jsonl` 已有创建/修订时间线，跨副本追加与统一审计中心待做；网格质量门控见“网格 B” |
| 几何 A | 🔶 | CadQuery build/export 与返回形态、caseDict `ctx.data/params/domain` 注入、Three.js 预览、cadscene replace/clear 实时同步、Base64 当前视图、FOV/aspect Fit、透视/正交、Orbit/Trackball、四宫格真实视口、RoomEnvironment、shadow map、单视图 SSAO、真实剖切/带孔端盖、边线/透明/dark-light 和材质偏好跨重载、CAD 对象树/选择/可见性、相机预设、bbox 属性/中心距离、剖切/爆炸/DataTexture Zebra、爆炸时间插值/互斥、八类关键帧轨道编辑/播放/导出、Zebra 配色/mapping/角度、CAD 平移旋转动画、STEP/STL+manifest、保存自动预览、参数驱动模型和 preview 内联导出抑制、导出按钮与 PNG 截图；失败保留上一有效场景且有 watcher 代次防竞态 | B-rep 精确测量、实体端盖、完整动画轨道、cadscene 重连/版本/多副本、完整 OCP meshData、完整 studio 配置和完整 Zebra 配置等高级能力未闭环 |
| 网格 B | 🔶 | blockMesh/checkMesh 已在真实 2D/3D 用例通过；质量报告、mesh hash 和 fail/warn/allowWithReason 门禁已验证 | snappy/gmsh/cfMesh 的全流程、网格来源/类型/边界层和自适应未系统验证 |
| 求解 C | 🔶 | 已安装 v2406 开发环境，icoFoam 2D/3D 通过；多工况派生保留初始 `0/` 并可运行；最小 functionObject D1–D5/G5 切片通过；2 子域 MPI、processor 重构和串并行差异通过，runtime 与通用 WSL 作业统一 v2406/--exec | C06B 的进程组取消/集群调度、更多求解器和物理模型实测不足；通用 UDF 阶段工作流、自定义求解器/其他 UDF 类型和批量工况矩阵/提交未实现 |
| 后处理 D | 🔶 | caseDict.postProcessing 快照、VTK 探针/线采样/面积分/体平均、rFFT、POD-SVD、AST 公式、基础 uncertainty、comparisonTargets 四类映射、同网格参考场 L2/hash、版本化 CSV/JSON/manifest、Agg PDF/SVG/300–600dpi PNG 出版图及来源追溯、报告/LaTeX 证据门禁；VTK/VTU/VTP/VTM/XDMF/Exodus/STL/series 通用 reader 与 file_path 路由；分组 Pipeline 与五类 Sources/Filters、受限 VF Python 控制台、scenes.py watcher、轻量 Inspector/Information（含数组类型/分量/字节）、预设与自由控制点色图、时间/相机关键帧动画、相机预设/保存恢复/截图、所有者 proxy 与 last_activity 空闲回收、2D/3D 渲染 | 实验/前次运行数据自动采集、异网格参考场、实验 validation、更多黄金对拍、数组公式/高级不确定度、多序列复杂版式和报告章节自动组织未实现；server-manager 通用面板、高级动画/属性轨道、ParaView 插件长尾仍有差距 |
| 报告 E | 🔶 | 自动 9 章和证据链接、旧稿备份、pandoc HTML/DOCX 内容、三种 LaTeX recipe/biber、表格生成、结构化错误和 Monaco 源行跳转、独立快照/禁 shell escape/120 秒超时/latest-wins、SyncTeX 精确 PDF→源码、HTML/DOCX 安全阅读器、Markdown KaTeX、公式调色板、refs.bib、Crossref/arXiv、Semantic Scholar 适配和常见引用格式 | 任意 CSL、完整 MathJax/出版商样式兼容、旋转页/复杂 CropBox 细节；Semantic Scholar 公共 API 本轮 LIVE_LIMITED（503） |
| Agent 聊天 | 🔶 | 双协议 SSE（run_id/seq、约 33ms 合并与结构前冲刷）、工具卡/diff、fork/compress、从完成回答创建分支、审批、Ask/Plan/需审批/全自动权限、Todo、`$skill`、`@path`、图片选择/粘贴与双协议图像块、附件 Blob 预览、Markdown/KaTeX；固定追问模板/最终结构、read/list/search 重型排除、计划状态约束、Windows/WSL/PowerShell runtime；280/1200/6000 事件与 4/8/16MiB 分级历史、向前分页和浏览器加载更早；工具 queued/running/interrupted 状态、类型图标/旧标题兼容和真实审批结果；web_search runId 隔离、共享预算与取消（默认 mock，真实搜索 provider 未接）；web_fetch 公网 DNS/IP 固定连接、重定向/8 MiB/超时与 Agent 私网负例（公网 LIVE_LIMITED）；update_goal 目标/验收/预算/revision、目标条和下一回合恢复；`/goal:run` 显式有界自动续轮及完成/预算/取消停止（Codex 等待继续变体未实现）；文件/文本提案 baseline_sha256、批准后外部修改保护与原子写；reasoning 强度/事件/持久化与正文分离、流式/折叠卡片、单份回答提交；正文/reasoning 伪 XML/DSML/sentinel 清洗与 Ask 结构化 tool_calls 拒绝；64/8/8/2/10 MiB 分级上限、错误脱敏和断开取消 provider；run_state/checkpoint、started/interrupted 恢复和半截回答投影；消息复制、修改摘要/用户导航、会话历史加载门控、协作取消/cancelled、稳定流式 Markdown、长回答/大代码/缓存降级、空占位过滤、上下文用量估算、压缩摘要/handoff 有界交接、会话重命名/删除约束、session/event.v1 JSONL 导出与真实下载 | Codex Goal 等待继续变体、真实 web_search provider、web_fetch 在线成功验收、Codex 后端、原版导出格式/Run 生命周期兼容、精确 token/自动模型窗口预算和更长历史性能未实现或未验收 |
| LLM 网关 | 🔶 | provider 注册表、BYOK Fernet 加密、OpenAI/Anthropic、流式和非流式；profile PATCH、保留/更换密钥、双协议 `/models` 动态发现、连接测试和离线回退 | 独立完整设置页、环境缓存、Ollama 模型联动和统一限流覆盖 |
| CLI agent | 🔶 | opencode 有额外契约注入和备份；discover_cli 支持显式路径、版本目录、稳定/预发布排序、.cmd/.bat 和 WSL Node 探测，capabilities 返回 cli_agent_details；通用终端仍按探测启动多个 CLI | 容器内隔离孵化、stdio 协议、审计和多 harness 配置切换均未完成 |
| 终端 | 🔶 | ConPTY/xterm、多会话、backlog、cmd/WSL、快速输入、所有者鉴权与隔离；选区/最近输出可一键加入 Agent | Linux 容器 PTY 实现、codex-wsl 兼容档、截图进聊天 |
| 作业 | 🔶 | 提交/取消、增量与 SSE 日志、DB 状态、残差曲线、孤儿 interrupted | Redis/worker、跨副本调度、完整配额覆盖、分布式恢复 |
| 公式识别 texo | 🔶 | 浏览器 WASM 识别、算例/本地图片、编辑/复制/插入 Monaco | 屏幕框选取图由上传替代；模型性能和识别质量需更多样本 |
| PDF 阅读/翻译 | 🔶 | pdf.js、文本层、划词翻译和剪贴板、真实 `page / count` 页码 worker；HEAD+206 Range transport、目录跳页、全文搜索、页码/缩放恢复、SyncTeX 精确 L7 和渲染取消均有当前证据；大 PDF 三次 Range 读取 182,328/378,936 字节 | 超大 PDF 性能、搜索高亮和旋转页/复杂 CropBox 细节未验收 |
| CAD 查看器 | 🔶 | 自研 Three.js 顶点/面/颜色/边线/轨道 | 与 three-cad-viewer 全功能仍有明显差距 |
| 技能生态 | 🔶 | 41 个自有技能文本，mtime 缓存失效，开源系+商业接口预留+PPT；`$skill` 补全和 get_skill 正文进入下一次模型请求的续跑证据；六个 `agents/openai.yaml` 元数据驱动弹窗名称/简介/默认提示；内置 < 组织 < 用户技能目录、创建更新、ZIP 打包/安装和覆盖备份 | 每个领域的真实任务深度需逐套验收；文本覆盖不等于求解器已安装，部分 VTK 可读声明还超过当前 reader |
| PPT/PPTX | 🔶 | `/ppt:build`、CasePanel、版本化 spec、真实 worker 和下载；原生文本/形状/表格/图片、只读模板、独占输出、Open XML 和模板哈希均验证 | Beautify、187 presets、图片重建、复杂模板、叙述/视频和页面渲染截图未实现 |
| flotherm2foam | ⬜ | 尚未接入 | 转换器重写及逐文件对拍 |
| SCNET（可选编译） | ❓ | 原版文档有远程作业/文件/容器/Notebook/桌面，公共版默认关闭，0.11.0 是否启用未知；Web 已有 B/S 签名客户端、dockview 工作台、五类资源生命周期、详情/搜索/复制和本地 mock 的 API+真实 Edge 验收 | 真实 SCNET 账户、调度器/镜像字段、流式大文件、Jupyter/noVNC 与付费资源生命周期未联调；不能将 mock 验收当作原版启用或生产对等证据 |
| 多用户/订阅 | 🔶 | JWT、org/membership/plan/subscription、四类配额检查点、归属安全回归；viz/job/LLM/storage 429 与还原均通过 | 设置页仍只有三条用量条；其他写入口与到期执法不全，支付/seat/OIDC/admin/离线许可、组织 ACL 和完整安全隔离未实现 |
| OpenFOAM 补全 | 🔶 | 原版 31,542 唯一 label 精确集合 + baseline_sha256；保留细分 kind，合入自有通用 kind/parents/dictionaryValue 文件 contexts/sourceExamples/confidence、生成器/确定性 index SHA-256、富分类和 13 类 runtime tag；Monaco 与 API 回归通过 | 完整物理类型/单位推断仍需深化；合并基准与源码扫描分类统计属于不同层次，不混为同一计数 |
| 工作区 FTS | 🔶 | 每算例 SQLite FTS5 与工作区内跨 case 搜索、重建/搜索/snippet、普通写删/上传/caseDict/Agent 即时增量、作业后重建、搜索范围 UI 与跨 case 打开；`files.changed` 快照事件联动 FTS 和文件树；building-UUID 锁、WAL/checkpoint、stale 恢复和用户级事件隔离均有验证 | 无事件连接时外部修改不保证即时索引；大索引性能与跨副本一致性缺失，当前为单进程轮询 |
| 求解日志解析 | 🔶 | 残差/Courant/时刻/耗时、initial/final、FATAL/WARN、双 TimeStep 和多种求解器格式；完整行增量游标+前缀 fingerprint、半行续接与重写/截断 409；作业终态自动写回 Job.parsed_summary | 特定商业求解器真实日志样本和超大日志性能仍需扩展，不以当前 golden 宣称覆盖所有求解器 |

## 二、当前验证证据

| 证据 | 覆盖 |
|---|---|
| 128 个后端/专项套件（完整 121/121 + A14/A17/C08/CB16/J12/J13 与其余前端/专项） | API、case 管理、网格/MPI、后处理、viz/scenes/cadscene、报告文档、LLM/reasoning、伪协议清洗、流上限/断开、profile、handoff、会话管理/导出、历史分页、工具状态、web 工具、Goal、提案/符号链接、runtime、安全、迁移、Range、UDF、Texo receipt；实库已备份并临时恢复校验后 stamp head，未在实库 downgrade |
| A22 case_events 专项 | case.created、A→B/第二次修订、非法写入零新增、最新优先分页和跨用户 404；原始 16 块与 caseDict 回归通过 |
| H06 日志结构化专项 | 作业终态自动写回 schema/时刻/Courant/Ux/警告/ExecutionTime/finished_cleanly；jobs_smoke 全链复验通过 |
| H16 统一事件专项 | jobs.updated、scene.available、quota.warning、runtime.degraded 和跨用户隔离；files.changed/jobs/viz/quota 回归通过，套餐已还原 |
| H12/H14 补全专项 | 原版 31,542 唯一 label 精确集合 + baseline_sha256；合入自有通用 kind、parents、文件 contexts/sourceExamples，parent+context 与未知 context 空结果均通过，重复合并 hash 相同 |
| H13 富分类专项 | C++14/preprocessor、4,803 宏、260 枚举、2,895 注册、13 类 runtime tag、compoundKeys、FoamFile、428 命令行 token、environment/wmake；清除乱码标签，真实连续构建 hash 相同，live kind 与全部补全回归通过 |
| H15 补全 provenance 专项 | item confidence 与确定性优先级、生成器 SHA-256、去时间戳 index SHA-256；真实 v2406 连续两次构建 hash 相同，语义变化会改变 hash，live API 与标准/父键回归通过 |
| B02 几何上下文专项 | 真实 CadQuery preview 验证 `__file__`、Path 类型 CASE_ROOT/A_GEO_DIR/case_root、VIBEFLOW_* 环境/路径、show_object/show/debug；完整 geometry_smoke 回归通过 |
| A23 回收站生命周期专项 | case/workspace 删除后统一 404，回收站恢复保持文件/revision/历史作业；跨用户 404、活动作业 409、取消后等待执行器收尾；真实 UI 删除/恢复通过，`shots/a23-*.png` 已核对 |
| E17 图片查看器专项 | Bearer Blob、缩放/滚轮/拖拽/复位之外，90° 旋转实际包围盒 120×320、Fullscreen 进入退出与降级均通过；`shots/e17-image-fullscreen.png` 已核对 |
| H17 上传与 Range 专项 | 16 MiB+1 普通上传 413、错误 offset/提前 complete/坏 hash 拒绝、断点续传 SHA-256 原子发布与取消；三段 multipart body/HEAD 长度、重叠/17 段 416，单段回归通过；不宣称 tus/S3 协议 |
| G04 终端输出专项 | 2 MiB pending/queue、64 KiB chunk、浅色 SGR 纯函数通过；真实 3 MiB PTY 输出保留最终 marker、底部跟随、用户滚动优先、稳定光标与浅色截图均通过 |
| G03 IME 与快速输入专项 | CJK 标点 beforeinput/compositionend 80ms 去重、普通中文不被拦截、多行快速输入两个结果均可滚轮查证；`shots/g03-ime.png` 已核对 |
| G08 全局主题专项 | 设置页 Light/Dark 同步 dockview/能力卡/Monaco/终端/CAD，浅色跨重载、恢复 Dark 均通过；本地 Monaco ESM/workers 不依赖 CDN，`shots/g08-light-theme.png` 已核对 |
| F59 环境注册表专项 | `VIBEFLOW_*` explicit/fallback/explicit_error 来源、显式错误 fail closed、A–E/V 最小阶段检查均通过；live capabilities 与设置页返回一致 |
| G13 存储连通专项 | 临时 fs 写读删及清理、非目录根错误返回、live storage ok/latency_ms；环境注册表回归通过 |
| G09 管理员 Setup 专项 | Python/pandoc/WSL/OpenFOAM 配置原子持久化、非法路径/发行版拒绝、空值恢复默认、admin 原值 PATCH 与 capabilities 重探测；浏览器 Setup 保存通过 |
| G05 codex-wsl 兼容档专项 | API 返回 WSL/UTF-8/TERM profile，真实 WSL 环境变量与前端命令生成通过；不执行 Codex、不读取 token |
| G14 进程监督专项 | 真实长作业、PTY、STL viz、延迟 Agent 同时登记，按 case 一次取消全部终态并清理；terminal/viz/cancel/jobs 回归通过 |
| G11 客户端错误专项 | Bearer 脱敏、stack 上限、append-only、admin 回读、空消息 400；浏览器 error/unhandledrejection 去重上报通过 |
| G12 语音输入专项 | 点击前零启动，fake 转写进入 Agent 输入框、再次点击停止、API 不可用提示；未启动真实麦克风 |
| F20 web_search 专项 | Wikipedia/Crossref provider 适配、HTML 清理、URL 与 web_fetch 前缀解包；Crossref 在线真实结果 LIVE_OK，Wikipedia LIVE_LIMITED；mock 预算/取消/Agent 回归通过 |
| F21 web_fetch 专项 | example.com 证书失败仍 LIVE_LIMITED，但 Crossref 公网 safe_web_fetch 返回真实 JSON 记 LIVE_OK；SSRF/取消/Agent 回归通过，未关闭 TLS 验证 |
| H10 索引事务专项 | building-UUID、活跃锁拒绝、过期锁恢复、WAL/checkpoint 和原索引可读；index/events 回归通过 |
| H08 作业索引专项 | 独立算例先建 FTS，真实 Windows 作业生成唯一 marker 文件；无 watcher 订阅时收尾后即可搜索，API 写删增量回归通过 |
| H19 历史作业时间专项 | 真实完成作业在无 JobProcess 的 FastAPI ASGI 进程中，详情与列表均返回 DB `ended_at`；重启后的 live `/jobs/2090` 也复验通过，jobs 全链回归通过 |
| H02B 孤儿作业重启专项 | 独立 uvicorn + 临时 SQLite/存储/端口，真实 running 作业在进程停止并重启后恢复为 interrupted；不操作开发 PostgreSQL，多 worker 不在此项声明范围 |
| H04 日志解析 golden | OpenFOAM/TimeStep 两种时刻、科学计数、Courant、smoothSolver/GAMG/PBiCGStab、耗时、CRLF End、FATAL/Warning 与未正常结束均有硬断言；第二格式 `=` 解析 bug 已修复，job_summary 回归通过 |
| H03 残差图浏览器专项 | 真实确定性作业驱动 Ux/p 双时刻、2 条 initial 虚线+2 条 final 粗线、对数→线性坐标变化；实际下载 PNG 校验签名、61,414 字节和 1280×520；`shots/h03-residual*.png` 已核对 |
| H05 增量日志专项 | 完整行游标只解析新增内容，SHA-256 绑定已消费前缀；半行续接、DILU、空增量、前缀重写/截断 409 均有真实 API 断言；全量 golden、job_summary 与残差浏览器回归通过 |
| H09A 即时索引专项 | caseDict、multipart 上传和 Agent 审批写入后无 WS/等待即可搜索；proposal、case_events、casedict_sync、index 四组回归通过，多副本仍不由此项声明 |
| H07 工作区搜索专项 | 两个已索引 case 同路径命中且 case_id/name 正确、未索引 case 状态透明、跨用户 404；浏览器“整个工作区”返回两个命中，点击后切换第二个 case 并打开正确内容；`shots/h07-workspace-*.png` 已核对 |
| H11 Texo receipt 专项 | 图片 data URL 的 SHA-256 绑定；localStorage/CacheStorage 双写与 20 条同步清理；真实 Edge 首次识别、第二次缓存命中且 provider 仅调用一次；`shots/h11-texo-cache.png` 已核对 |
| `golden_cavity.py` / `golden_cavity3d.py` | 真实 OpenFOAM 2D/3D blockMesh→checkMesh→icoFoam→结果与日志解析 |
| `tools/e2e-drive.mjs` | 登录、主壳、终端、Agent、文件、自动预览成功/失败、范围附件、ZIP 导入、导入后文件事件和算例复制；截图 `shots/wp04-autopreview-shell.png` 已打开检查。PDF 内容和专用阅读器仍以专项回归为准 |
| WP09 浏览器专项 | LaTeX 源跳转/防抖、PDF 页码/Range/目录/搜索/SyncTeX/翻译复制、安全 HTML/DOCX、Markdown KaTeX、CSV、图片缩放平移、PPT spec/产物/下载，以及明确 mock 的引用 UI/格式/refs.bib；对应 `shots/wp09-*.png` 已人工核对 |
| WP10 reasoning 专项 | 双协议参数/事件/持久化、流式卡、单份回答、折叠/展开和重载恢复；`shots/wp10-reasoning.png` 已人工核对 |
| WP10 导航专项 | 用户/助手复制、指定用户消息高亮、真实滚动区回底部、文件修改摘要定位；`shots/wp10-chat-navigation.png` 已人工核对 |
| WP10 停止与 Markdown 专项 | 取消/重载/下一回合、稳定 DOM 块、权威替换和单光标；`shots/wp10-agent-stop.png`、`shots/wp10-chat-markdown.png` 已人工核对 |
| WP10 性能专项 | 40KiB 代码纯文本降级、600KiB 截断渲染、600,018 字符全文复制；`shots/wp10-chat-performance.png` 已核对 |
| WP10 profile 专项 | 编辑/连接测试、动态模型、新会话 model-b、离线回退与恢复；`shots/wp10-profile-models.png` 已核对 |
| WP10 上下文专项 | 字符/4 估算随正文/草稿更新，reasoning 不计入；`shots/wp10-context-usage.png` 已核对 |
| WP10 交接专项 | 压缩摘要进入新会话和模型消息，keep_chars 有界交接且不复制完整历史；`shots/wp10-handoff.png` 已核对 |
| WP11 web_search 专项 | runId 隔离、共享预算、取消、Agent 续跑、globe 卡和重载持久化；默认 mock 非实时；`shots/wp10-web-search.png` 已核对 |
| WP11 web_fetch 专项 | DNS/直接 IP、固定连接+原 Host、重定向、8 MiB、超时、取消和 Agent 私网负例通过；公网证书校验失败为 LIVE_LIMITED，未绕过验证 |
| WP11 Goal 专项 | update_goal 校验、revision、current_goal、下一回合恢复、目标条和重载；`/goal:run` 两轮完成/预算 blocked/取消，浏览器 active→completed/revision 2；`shots/wp10-goal.png` 已核对 |
| WP11 Plan 专项 | 只读工具执行、危险提案零副作用、批准恢复审批写入和浏览器批准按钮 |
| WP12 有序 IPC 专项 | run_id/seq 严格递增、AB 合并、reasoning 顺序、C→tool.state；LLM/工具/取消/Goal 回归通过 |
| WP12 CLI 发现专项 | 显式路径、稳定/预发布排序、.cmd/.bat、WSL Node 假 runner 和 capabilities 详情；不启动 Codex 或读取 token |
| G06 终端输出专项 | 真实 PTY `echo TERMINAL_AGENT_OK` 后，最近输出一键进入 Agent 输入框；选区路径已实现，截图未实现 |
| WP11 技能存储专项 | 用户隔离、组织共享、用户覆盖、ZIP 打包/安装、默认不覆盖和恶意路径拒绝；多组织解析与 Windows 安装路径通过 |
| WP10 图片附件专项 | 双协议图像块、附件原始字节、Base64/数量/大小限制；浏览器粘贴预览及重载 Blob 显示，`shots/wp10-image-attachment.png` 已核对 |
| WP11 会话管理专项 | 重命名/跨用户、活动/待审批/fork 删除约束和记录清理；浏览器重命名/删除及目标清理通过 |
| WP11 回答分支专项 | 指定完成回答前历史零拷贝、原会话不变、分支续聊独立；用户/取消/中间消息拒绝，浏览器创建/删除分支通过 |
| WP11 会话导出专项 | 自有 session/event.v1 JSONL、310 完整事件（含生命周期）、无密钥和跨用户 404；浏览器真实下载并解析 |
| WP11 checkpoint 专项 | 16 KiB checkpoint、断开 interrupted、started 恢复；浏览器半截正文/reasoning 单一恢复且禁分支，`shots/wp10-checkpoint.png` 已核对 |
| WP11 提案专项 | 文件/文本 baseline_sha256、批准后外部修改保护、原子写和 Agent 竞态；冲突保留外部内容，未变化才批准 |
| WP11 符号链接专项 | 内部链接允许，跨 case/外部/相似根/broken/未创建目标拒绝；case 管理、Range、提案和 caseDict 回归通过 |
| WP11 伪协议专项 | 双协议逐字符/无结束 sentinel、reasoning、落库/重放和 Ask 结构化 tool_calls 拒绝；普通相似标签保留，无工具卡副作用 |
| WP11 流上限与断开专项 | 双协议 64/8/8/2 MiB、16 KiB 脱敏预览通过；客户端断开到 provider 取消约 0.110s，下一回合正常 |
| WP10 模板/工具/历史/runtime 专项 | 固定追问模板、read/list/search 重型排除、计划状态约束、Windows/WSL/PowerShell runtime；280/1200/6000 与 4/8/16MiB 分级、300 条加载更早已通过；48 轮次 fork 配额失败后已改独立用户复验 |
| WP10 内容专项 | 空占位过滤、真实修改摘要和短回答在重载后保留；`shots/wp10-chat-content.png` 已核对 |
| 其他 E2E/探针 | 历史覆盖 texo、viz 模式、Agent UI、残差图、快速输入等；是否本轮复验以 `progress.md` 为准 |
| `schema_diff.py` | 2026-10-24 实库与 12 表模型零差异；未执行 stamp/upgrade |

测试通过只证明对应断言，不自动推出“桌面版 100% 对等”。

## 三、当前架构与原方案的差异

| 项目 | 当前实际形态 |
|---|---|
| 执行环境 | Windows 宿主 + 固定 WSL 发行版；不是已完成的按用户 Docker 沙箱 |
| 实时与作业 | 单 FastAPI 进程内注册表；Redis/arq/多副本事件总线未接入 |
| 文件存储 | 当前 fs 为事实源；S3 仅覆盖部分文件 API，不是全系统可透明切换 |
| 可视化 | venv VTK + trame + `vtkOpenFOAMReader`，不是完整 pvpython/server-manager 驱动 UI |
| 数据 | PostgreSQL 12 表；JSON 多以 Text 保存，不是方案中的全 JSONB |
| 前端 | React 19 + Zustand + dockview；未采用 TanStack Query/Tailwind |
| 数据库变更 | 已有 Alembic 基线和只读差异工具；现有实库纳管需备份后明确 stamp |

## 四、后续优先级

1. **生产安全与隔离**：命令执行沙箱、组织资源授权、S3 路径一致性、会话/作业跨副本模型。
2. **部署验收**：目标机 Docker 构建与端到端验证、Linux PTY、备份/升级/回滚演练。
3. **核心 CAE 深度**：专用后处理与数值证据、MPI 取消/调度、更多网格/求解器、flotherm2foam 对拍。
4. **可视化与文档长尾**：server-manager 通用面板、完整 CAD studio/多副本、真实 PDF 反向定位，以及 PPT Beautify/presets/图片重建/叙述视频和渲染验证。
5. **团队功能与验收**：seat/admin/OIDC、完整配额、性能负载、渗透测试和第三方许可证核查。

有意缩减/暂缓项仍沿用原方案记录：Catalyst/Extractors、MinerU、多 harness 深度集成等；详见 `mechanism-audit.md` 和 `plan-cs-to-bs.md`。
