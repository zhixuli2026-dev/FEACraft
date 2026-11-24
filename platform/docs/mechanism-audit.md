# 机制审计与缺口清单（2026-10-21 审计，2026-10-24 复核）

> 方法：5 个并行审计线（AgentHost / Worker 服务 / 工作流与技能 / Web 页面桥接 / 主壳与杂项）。
> A/B 保留当期审计结论；C 已按当前实现复核。当前功能总表见 `parity-checklist.md`。
> IP 红线：全部语义重述，无原文复制。

## A. 已修正的认知偏差（重要）

1. **VFIndexService 不是 OpenFOAM 补全索引**——它是**工作区文件索引**（SQLite FTS5 全文+目录列举+监听增量，喂项目树/全局搜索）。16.5MB OpenFOAM 补全索引是**静态资产** `share/vibeflow/completion/openfoam-v2406-keywords.json`，由 Studio 进程内消费，不经 Worker。
2. **原版内置工具是 11 个不是 12**——`request_approval` 是权限模式枚举（与 full_access 并列），不是工具。工具=read/list/search/web_search/web_fetch/update_plan/update_goal/run_wsl_command/run_windows_command/propose_file_update/propose_text_replacement。
3. **桌面版编辑器没有 Monaco**——原生 QPlainTextEdit + 自研高亮；补全浮层是原生 QListView。我们用 Monaco 是超出。
4. **`.macros/` 目录原版无自研机制**——仅 ParaView 原生宏菜单；我方布局里该目录定位为宏脚本存档约定即可。
5. **scenes.py 执行上下文**：原版注入 `__file__`/`VIBEFLOW_PARAVIEW_SCRIPT`，禁设 ViewSize；当前 watcher 与 VF_* 增量管线存在，但 `__file__`、`VIBEFLOW_PARAVIEW_SCRIPT` 未注入，也没有 ViewSize 检查，**不能称为上下文等价**。

## B. 本轮已落地（审计驱动修复清单）

| 机制 | 原版依据 | 本版实现 |
|---|---|---|
| scenes.py 监听执行 + VF_* 增量管线 | 原版 watcher 500ms 防抖+paraview_executor.log | viz_kernel watcher（0.8s 轮询+0.5s 防抖）+ 自有 facade 12 函数 + `viz_executor.log` ✅ |
| propose_text_replacement（聚焦编辑） | 11 工具之一，危险 | 工具+预检（唯一性/存在性）+approve 落盘 ✅ |
| 权限模式 full_access/request_approval | 会话级组合 | `conversations.permission_mode`（approve_writes/full_access/ask）+ ChatPanel 下拉 ✅ |
| 危险工具高危正则二次拦截 | 原版同款 | tools.py 已有 ✅ |
| 系统提示注入 memory.md/阶段门控/update_plan 引导 | CLAUDE.md 契约 | agent.py 系统提示升级版 ✅ |
| workflowState 宽容解析 | `{B}`/小写/dev/省分号 | casedict.py 正则对齐 ✅ |
| 状态推进确认+errors 门控提示 | 用户确认硬门控+errors.summary.open | CasePanel 二次确认按钮+open 警告 ✅ |
| 阶段快捷提示词 ×6 | 桌面一键提示词语义 | ChatPanel 阶段按钮行 ✅ |
| $skill 弹窗 | 桌面 $ 补全 | ChatPanel `$` 输入补全（技能注册表）✅ |
| diff 提案卡（+added/−removed+着色预览） | addFileProposalCard | ApprovalCard+utils/diff.ts（LCS）✅ |
| update_plan Todo 面板 | 固定 Todo 面板 | PlanStrip（最新计划投影）✅ |
| 残差图：initial/final 双序列+对数/线性切换+导出 PNG | OpenFoamLogPlot 语义 | JobsPanel ResidualChart v2 ✅ |
| 日志解析：TimeStep 第二格式+FOAM FATAL/Warning 计数 | worker 正则族+差异化增强 | logparse.py ✅ |
| search_text/list_files 排除重型目录 | 索引器排除表 | tools.py EXCLUDE 集 ✅ |
| PDF 划词翻译卡复制按钮 | copyTranslationText | ViewerPanel ✅ |
| texo 结果格式选择（$…$/$$…$$/equation） | 原版格式选择器 | TexoPanel ✅ |
| 终端 scrollback 50000+WebLinks | 桌面 xterm 配置 | TerminalPanel ✅ |
| Agent 回复提示音 | Message.mp3 | ChatPanel WebAudio 双音蜂鸣 ✅ |
| 核心技能文本（自有重写） | 37 技能生态 | vibeflow-openfoam/-postprocess/-report（+此前 4 个=7/37）✅ |

## C. 缺口复核（2026-10-24；2026-10-25 更新 UDF 垂直切片）

### 已闭环，不再列为未做

- 终端快速输入、多标签/backlog、CSV 查看器、About、外部修改冲突检测。
- Agent `@path` 附件、diff 提案卡、Todo、阶段快捷词和 `$skill`。
- OpenFOAM v14 补全索引、caseDict 块级表单（已改 token 级局部替换，块内注释、其他键和 workflowState 保真）、工作区 FTS+搜索 UI。
- preview 禁止自动调用 `export_geometry`（钩子仅在 export 分支）；脚本内联 `cq.exporters.export` 在 preview 也被抑制，显式 export 行为保持。
- Monaco caseDict 保存改走专用 `/caseDict`；二进制下载改为 Bearer fetch→Blob→objectURL，含文件名和释放。
- 公式调色板、refs.bib、Crossref/arXiv、LaTeX PDF 编译链。
- 多工况派生（精确保留初始 `0/` 和其他设置，跳过结果时刻/日志/.foam；`0/U`、`0/p` 保留，icoFoam 实测可运行）。
- `errors.summary.open/blocking` 的 workflowState 推进硬门控（REST 409、文件/revision 不变）与 JobsPanel Windows/WSL runtime 选择。
- Agent/Jobs 共用高危命令底线；WSL 作业与 runtime probe 使用 --exec/v2406；无扩展名场文件按 OpenFOAM 着色；prod 默认持久化 fs，未受限声明的 S3 在 DB 初始化前拒绝启动；viz/job/LLM/storage 429 与还原均有实证。
- OpenFOAM v2406 开发环境、wmake/foamEtcFile，以及最小 functionObject 的 D1–D5/G5 垂直切片：`wmake libso`、动态加载、真实 `functions` 执行、`P0_UDF_OK` 日志与 `0.1/U` 产物均通过；不代表通用 UDF 工作流或其他 UDF 类型已完成。
- 41 个自有技能文本、轻量对象管线/Inspector/Information/Color Map、Animation v1。
- 组织/成员/套餐/订阅骨架和四维配额；终端/viz/profile 的归属安全加固。
- fs/S3 范围读取、真实 fs 下载链接、单段 Range/206、suffix range、HEAD 和 416 校验。

### 剩余小/中项

- [ ] Agent 图片粘贴/图片消息附件；会话导出。
- [ ] 多段/multipart Range 与超大文件性能验证。
- [ ] OpenFOAM v2406 索引重建、上下文值补全和域探测。
- [x] 多工况批量提交/洪泛控制（1–50 项/批、每批并发 1–4、用户配额等待、持久化状态与统一取消；200 作业负载验收仍归 J21）。
- [ ] 完整配额覆盖：几何/报告/文本写/Agent 写/翻译/压缩和订阅到期执法。
- [ ] LaTeX 编译错误结构化解析、PDF 回跳；表格生成器。
- [ ] LLM profile 独立设置页、外观设置；索引 watcher 与大索引性能验证。

### 剩余大项

- [ ] server-manager 通用属性面板、完整 Pipeline Browser、自由点 Color Map、关键帧动画。
- [ ] 容器内隔离 CLI agent；当前按用户决策仅保留 opencode 通道。
- [ ] flotherm2foam 重写与逐文件对拍。
- [ ] seat/admin/OIDC、支付、离线许可、审计中心和组织级资源授权。
- [ ] 按用户硬沙箱、S3 全链路一致性、Redis/worker、多副本和生产部署验收。
- [ ] MPI、更多求解器实测、性能负载与安全渗透测试。

**有意砍掉（记录在案）**：Catalyst/Extractors 菜单、Comparative/Memory/Material Inspector、协作面板、客户端自动更新器、VFProcessSupervisor 看门狗（容器编排替代）、VIBEFLOW_<TOOL> 客户端发现（沙箱镜像预装替代，保留能力自检）、qtermwidget 双终端、28 插件长尾。

**明确决策**：MinerU 不接；CLI agent 仅 opencode；商业求解器只做组织自配 license/remote/HPC 接口预留。

**两端均无/新需求**：崩溃上报（建议 Sentry 类）、语音输入。
