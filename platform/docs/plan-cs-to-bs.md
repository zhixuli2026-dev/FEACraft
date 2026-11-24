# VibeFlow Studio：C/S → B/S 架构迁移方案

## Context

**目标**：把 VibeFlow Studio 0.11.0（Qt 5.15 + ParaView 5.11 桌面应用，CAE 数值仿真工作台）重新实现为 B/S 架构 Web 应用，功能完全对齐现有版本。

**出发点**：我们已对安装包完成完整逆向（自研 Inno Setup 6.7 提取器，30,275 个文件 SHA-256 全部校验通过），掌握了桥接协议、工作流状态机、进程架构的全部事实依据。

**用户决策（已拍板）**：团队服务器部署（多用户+账号体系）/ 3D 可视化用 trame+ParaView Web / 后端 Python FastAPI / 范围一次性全功能。

**IP 边界**：全部文字资产（CLAUDE.md、37 个 SKILL.md、vibeflow_geo、flotherm2foam 胶水代码）**重写不照抄**；概念/协议/schema 可借鉴，代码与文字表达是厂商资产；补全索引从 OpenFOAM v2406 源码自行重建。

## 现状关键事实（逆向证据）

**架构**：Qt Widgets 主壳（VFMainWindow + ParaView dock 全家桶）+ 5 个 QtWebEngine 页面 + 命名管道连接的两个辅助进程：
- `VibeFlowAgentHost.exe`：通用 SSE 流式中继（任意 OpenAI 兼容端点）+ QProcess 孵化 CLI agent；IPC：`agent.request/command/tool/cancel`；内置工具集 read_text_file/run_wsl_command/web_search/propose_file_update/request_approval 等 12 个
- `VibeFlowWorker.exe`：VFIndexService（OpenFOAM v2406 补全索引构建）+ VFLogService（求解器日志解析）
- 执行模型：Windows 宿主 + WSL Linux 求解（OpenFOAM v2406、pvpython、TeX Live），工具发现用 `VIBEFLOW_<TOOL>` 环境变量

**桥接面（B/S 替换清单）**：
| Web 页面 | 桥接 | B/S 替换 |
|---|---|---|
| agent（聊天，2796 行单文件） | QWebChannel `agentBridge` 9 槽 + `vibeAgent.*` ~40 推送 + `vibeAgentBatch` | React + REST + SSE（推送方法收敛为类型化 SSE 事件流） |
| terminal（xterm.js+ConPTY） | `terminalBridge` 6 槽 10 信号 | PTY WebSocket（协议已完整枚举） |
| cadviewer（three-cad-viewer 4.3.7） | loopback WS `scene.replace/scene.clear`，schema `vibeflow.cadquery.scene.v1` | **几乎原样可用**，WS 协议保留 |
| texo（FormulaNet ONNX+WASM） | console.info 信令 | 静态托管+回调，现代浏览器可开 SIMD |
| pdfjs | `pdfBridge`（pdfDataBase64/translateSelection） | PDF.js+Range 请求+`POST /api/pdf/translate` |

**工作流（产品灵魂）**：caseDict 唯一参数源；状态机 A(几何 CadQuery)→B(网格 blockMesh/snappy/gmsh)→C(求解 多工况 C-run/case-*)→D(后处理 postprocess.py + scenes.py 文件监听自动执行进 GUI、VF_* 增量管线)→E(报告 pandoc/LaTeX)；V=UDF 开发 D1→D5（wmake 编译验证）。37 个求解器技能、多 harness（Claude/Codex）、`$skill` 调用、memory.md 经验日志。

## 目标架构

```
            浏览器 React SPA（dockview 停靠布局）
              HTTPS REST · SSE(agent) · WS(pty/viz/cadscene/events)
                            │ TLS（nginx/traefik 反代）
   ┌──────────────┬─────────┴──────────┬─────────────────┐
   api(FastAPI)    ws-gateway(长连接)    worker(arq 后台任务)
   REST+SSE        PTY/viz代理/事件总线   索引/转换/日志解析
   └──────┬────────┴─────────┬──────────┴───────┬─────────┘
      Postgres+MinIO(S3)   Redis(队列+pub/sub)   Docker host(s):
                                              · 每用户长驻沙箱容器
                                                (OpenFOAM v2406+wmake+gmsh
                                                +TeX Live+cadquery+python)
                                              · viz kernel: pvpython+trame/会话
                                              · job 兄弟容器: 求解器/MPI
```

- **内部认证**：短期 JWT（gateway 签发，scope 到 session/case），替代命名管道+静态令牌
- **caseDict 仍是文件唯一事实源**，DB 只做镜像/索引（这是契约，不能改成 DB 主存储）
- **会话持久化**：JSONL schema 保留为事件负载，存储改 Postgres `agent_events`（append-only JSONB），fork=分支头指针零拷贝
- **LLM 密钥**：服务端 vault（KMS 包裹 Fernet），BYOK+组织级 profile——B/S 后密钥必须服务端保管
- **授权**：废弃 Ed25519 单机 key，改正常订阅（org/seat/到期/配额）；签名仅保留给离线内网部署的 license 文件

### 前端主壳（最大工作量项：原生 Qt → Web 重建）

React 18 + TS + Vite + Zustand + TanStack Query + dockview + Tailwind：

| Qt 面板 | Web 组件 | 备注 |
|---|---|---|
| ProjectDock | react-arborist | watcher 事件增量刷新 |
| Pipeline Browser | 自研树 | 状态=wslink kernel proxy 快照 |
| QVTK 渲染窗 | vtk.js 远程图像流+交互回传 | 小数据可本地渲染 |
| Inspector 属性面板 | **server-manager XML 驱动的 schema 渲染器**（自动生成全部 filter 面板）+ Top-20 手工精修 | HIGH 工作量 |
| Color Map Editor | vtk.js PiecewiseGaussianWidget 移植 | HIGH，单独立项（3-4 人周） |
| Sources/Filters 菜单 | 命令面板（Ctrl-K），条目由 XML 类别自动生成 | 天然 100% 覆盖 |
| Animation View | v1 时间播放+相机预设+导出；关键帧轨道 v2 | 明确缩减点 |
| TerminalDock | xterm.js+PTY WS | |
| JobsDock | 作业表+日志 tail（WS）+残差图表 | |
| GeometryPreviewDock | three-cad-viewer（保留 OSS 库） | 场景协议不变 |
| OpenFOAM 字典编辑器 | Monaco+Monarch tokenizer+补全 provider | 补全走 index 服务 |
| LaTeX 套件 | Monaco+公式调色板+引用对话框+容器 TeX Live 预览 | |
| Markdown/PDF/图片/表格 | react-markdown+KaTeX / PDF.js / panzoom / AG Grid | MathJax→KaTeX |
| Catalyst 菜单 | **有意砍掉**（insitu，CAE 流程未用） | 记录在案 |

### trame 集成（HIGH 风险区）

- **VizSession 生命周期**：`POST /cases/{id}/viz:sessions` → 在用户沙箱容器内 `docker exec` 起 pvpython kernel（ParaView 5.13 EGL headless，桌面端锁 5.11 是 Qt 客户端约束，服务端无此约束，需回归验证 OpenFOAMReader）→ 短期 JWT 注册 → 浏览器 WSS 经 gateway 透传 wslink → 空闲 15min 回收，每用户并发 kernel ≤2（0.5-1GB 内存/个）
- **VF_ 增量管线等价物**：kernel 内 executor 线程对 `D-post/scenes.py` 做 inotify 监听（500ms 防抖），在同一 Python 命名空间 exec()——与"当前 GUI 会话"精确等价；日志路径契约 `D-post/logs/paraview_executor.log` 不变；VF_* 命名保护保持约定级+轻检查
- **属性面板引擎**：kernel 导出 server-manager proxy XML→JSON（Redis 缓存，每 ParaView 版本一份），React schema 渲染器自动生成面板；Top-20 filter（Slice/Clip/Contour/Glyph/StreamTracer/Calculator/Threshold/Warp…）手工定制
- **兜底**：M2 若通用面板落后，先 iframe trame-paraview 自带客户端，M6 前替换

### FastAPI 后端分解（模块化单体，三进程）

`api` 进程 routers：auth / workspaces+files(tus 分块上传、Range 下载、watcher 事件) / cases(caseDict 解析服务 Python 重写、状态机、StandardCase 脚手架) / agent(会话/fork/压缩/审批/提案+SSE) / llm(provider 注册表、vault、SSE 中继) / jobs / index / logs / convert(flotherm2foam 重写、MinerU、pandoc、TeX) / settings / runtime(supervisor 等价) / billing

`ws-gateway`：PTY WS、viz 透传、cadscene 推送、`/ws/events` 每用户事件总线（Redis pub/sub 扇出，多副本不丢事件）

`worker`（arq+Redis）：索引重建、flotherm2foam、MinerU/pandoc/TeX 编译、log parse——秒级到分钟级无交互任务

### Linux 沙箱（替代 WSL）

- 镜像 `vibeflow/sandbox:v2406` = Ubuntu 24.04 + OpenFOAM v2406 + wmake(gcc/openmpi) + gmsh + TeX Live + cadquery+ocp_tessellate + python3.11 + numpy/matplotlib + pandoc（~8-10GB）
- 每用户一个长驻容器（等价 WSL 会话），挂载 `/data/workspaces/{user_id}`→`/work`，注入工具注册表生成的 `VIBEFLOW_*` env
- 硬限制：cgroup cpus/mem/pids、uid≠0、rootfs 只读、**默认无公网**（web_search/web_fetch 在 gateway 服务端执行）
- 大作业：JobScheduler 经 docker socket 派生兄弟容器（同镜像、只挂该 case 目录、MPI 单容器多进程）；`executionProfile: remote|HPC` v2 接 SSH/Slurm（caseDict 字段原生预留）
- **CLI agent（codex/claude/opencode/kimi）只允许在沙箱容器内孵化**，独立低权用户+出口代理+全量审计——全系统最高危面

### LLM 网关

- SSE 规范化事件模型直接继承（text.delta/reasoning.delta/tool.delta/message.completed/stream.done|error/request.*）
- 工具集：`run_windows_command` 废弃，`run_wsl_command`→`run_shell_command`（沙箱内）；文件类工具 scope 到本 case workspace；web_* 服务端执行
- 审批模式：org 策略 `full_access|approve_writes|approve_all`；危险命令正则（rm -rf、curl|sh 等）任何模式都强制二次确认（继承危险操作确认协议）
- 限流：Redis token bucket，用户×provider×model 维度 + 组织日额度
- 会话表：`conversations`(forked_from_id/branch_head_event_id) + `agent_events`(append-only JSONB，`windowsCwd/wslCwd`→`containerCwd`)

### 数据模型（核心表）

users / oauth_accounts / organizations / memberships(admin|member|viewer) / **subscriptions**+plans（seat、并发 kernel、并发作业、LLM 日额度、存储配额）/ licenses(仅离线) / **provider_profiles**(vault) / workspaces / **cases**(workflow_state、dict_revision、case_meta 镜像) / case_events(状态机审计) / **conversations** / **agent_events** / tool_calls / proposals / cli_agent_runs / **jobs**(container_ref、cgroup_limits、parsed_summary) / viz_sessions / terminal_sessions / skills(重写文本，system|org|user 三级) / completion_indexes / conversion_jobs / audit_logs / settings

### 存储布局（文件系统为事实源）

```
/data/workspaces/{user_id}/{case_id}/
  0-caseDict/{caseDict, memory.md}   A-geo/{geometry.py, scene.json, exports/}
  Ref/   C-run/[case-*]   D-post/{postprocess.py, scenes.py, pvpython.py, figures/, logs/}
  E-report/{report.md, build/}   UDF/{README.md, src/}   .macros/  .vibeflow/
```

## API/通道设计要点

REST `/api/v1`：bridge 槽位一一映射（webApproveTool→`POST /conversations/{cid}/tool-calls/{tid}:approve`，webForkConversation→`POST /conversations/{cid}/fork`，geometry 预览→`POST /cases/{id}/geometry:preview` 完成后推 scene.replace…）。实时通道：SSE `/conversations/{cid}/stream`；WS `/ws/terminal/{id}`（二进制帧=pty 字节+JSON 控制）；WS `/viz/{vsid}/ws`（wslink 透传）；WS `/ws/cadscene/{case_id}`（场景协议原样）；WS `/ws/events`（jobs.updated/files.changed/runtime.degraded/scene.available/quota.warning）。

## 风险登记（按严重度）

| # | 风险 | 等级 | 缓解 |
|---|---|---|---|
| R1 | ParaView UI 特性差距（数百 filter、color map、animation） | HIGH | proxy-XML 通用面板+Top-20 精修；color map 单独立项；animation v1 缩减；iframe 兜底 |
| R2 | 服务端孵化 CLI agent 安全 | HIGH | 仅沙箱容器、uid≠0、无 docker socket、出口代理、密钥只在 gateway、全审计、组织可禁用 |
| R3 | run_shell_command 多租户滥用 | HIGH | cgroup 硬限、目录隔离、审批策略、危险命令强确认、配额告警 |
| R4 | IP 合规 | HIGH(法务) | 全部干净重写；索引从 OpenFOAM 源码自建；第三方组件逐一核 license；FormulaNet 权重需核实 |
| R5 | 商业求解器共享服务器授权 | HIGH(商务) | 不捆绑；组织自配 license server 或走 remote/HPC profile |
| R6 | trame kernel 内存×多用户 | MED-HIGH | 并发上限+空闲回收+节点熔断+降级静态截图 |
| R7-R12 | 长作业 WS 规模/CadQuery 延迟/EGL 兼容/PTY 保真/大文件/会话亲和 | MED-LOW | 见各自缓解（pub/sub 扇出、scene 缓存、官方 EGL 基线、TERM 协商、tus/Range、sticky session） |

## 里程碑（3-4 人团队，约 26 周 +15% 缓冲 ≈ 7 个月）

> **工期更新（2026-10-25）**：以下是原始目标估算，保留作历史参考。当前 294 项证据台账对应的执行顺序、工作量和发布门槛以 [implementation-master-plan.md](implementation-master-plan.md) 为准，不再按 26 周承诺全量交付。

| 里程碑 | 内容 | 人周 | 关键路径/风险 |
|---|---|---|---|
| M0 地基（2w） | mono-repo/CI/DB/Redis/MinIO、auth+org+订阅骨架、沙箱镜像、工具注册表 | 6 | 镜像构建 1 天必须跑通 |
| M1 壳与文件（4w） | dockview 壳、项目树、files API、查看器组、Monaco OF 编辑器+补全、PTY 终端、settings | 12 | |
| M2 可视化（5w） | EGL spike→pvpython kernel、wslink 代理、Viewport、PipelineBrowser、通用属性面板 v1、scenes.py watcher、Information、python console、cadviewer、geo 服务重写 | 16 | **HIGH**：EGL spike 第 1 周出结论，失败即切 iframe |
| M3 Agent 核心（4w） | llm 网关、聊天 UI（流式/工具卡/diff 卡/fork/compress）、工具集、agent_events、CLI agent 孵化+审计 | 13 | **HIGH**：R2/R3 |
| M4 工作流引擎（3w） | caseDict 解析器+状态机、StandardCase 脚手架、skills 注册表（核心 10 个文本重写） | 10 | 文本重写可并行推进 |
| M5 作业与转换（4w） | JobScheduler、log parse+图表、多 case、checkMesh、flotherm2foam 重写、MinerU/pandoc/TeX、LaTeX 套件、texo、PDF 翻译、PPT | 13 | 商业求解器仅做接口 |
| M6 多用户收尾（3w） | seat/配额/限流、审计中心、admin console、OIDC、离线 license 验签、剩余 27 skills、color map editor、Top-20 面板精修 | 10 | |
| M7 加固验收（3w） | 全部验证、渗透测试（R2/R3 专项）、性能调优、部署运维文档 | 9 | |

## 验证方案

1. **黄金用例 E2E**：G1 cavity(icoFoam) 全流程走通并对拍桌面版参考值（U/p 场 L2 误差<1%）；G2 pitzDaily(forces)；G3 damBreak(多相+动画)；G4 CadQuery 装配→场景→STEP/STL 导出 hash 对拍；G5 UDF D1-D5 wmake 编译验证；G6 flotherm2foam 输出逐文件 diff。**agent replay 模式**（录制 JSONL 回放驱动，确定性 CI 门禁）+ 每日真实 LLM smoke
2. **模块对等清单**：§映射表每行 checkbox+截图对拍；Sources/Filters 用 server-manager XML 自动比对（100% 覆盖为门禁）；终端跑 vi/htop/codex TUI 人工验收
3. **协议一致性**：SSE schema 契约测试+schemathesis fuzz；场景 golden JSON 回放；JSONL 导出 schema diff
4. **负载（k6）**：50 并发用户×(1 PTY+1 viz+1 agent SSE)，记录 REST 成功率和 p95，核对 SSE 首 token p95<2s、viz 交互 p95<200ms、200 作业洪泛配额公平。REST 原 300 ms 阈值按 2026-11-25 决定移出验收。 [当前口径](FF-J21-rest-load.md)
5. **安全**：容器逃逸套件、路径穿越 fuzz、越权访问、vault 回显扫描、限流生效、CLI agent egress 阻断

## 实施时最关键的对照基准文件

- `extracted/share/vibeflow/CLAUDE.md` — 工作流契约重写基准
- `extracted/share/vibeflow/StandardCase/0-caseDict/caseDict` — caseDict schema 验收基准
- `extracted/resources/agent/agent-conversation.html` — agentBridge 全桥接面（SSE 事件设计来源）
- `extracted/share/vibeflow/skills/vibeflow-postprocess/SKILL.md` — VF_ 管线语义规格书
- `extracted/share/vibeflow/python/vibeflow_geo/runner.py` — 几何服务重写 API 规格

## 附录：当前实现偏差（2026-10-24）

本方案继续作为批准的目标架构；以下是实施后的实际状态，不应把前文规划直接当成现状。

| 方案项 | 当前实现 |
|---|---|
| React 18 + TanStack Query + Tailwind | React 19 + Zustand + dockview；未引入 TanStack Query/Tailwind |
| api/ws-gateway/worker 三进程与 Redis/arq | 单 FastAPI 进程；Job/PTY/viz 主要为进程内状态，Redis/arq 未接 |
| Postgres JSONB 事件负载 | 当前 12 表，JSON 多以 Text 字符串保存；已接入 Alembic 基线 |
| MinIO/SeaweedFS 全系统对象存储 | 本机 fs 为事实源；S3 仅覆盖部分文件 API，执行链仍直接依赖本地路径 |
| 按用户 Docker 沙箱 | Windows 宿主 + 固定 WSL 发行版；Dockerfile.sandbox 尚未接入执行链 |
| pvpython/ParaView server-manager 驱动 | pip VTK + trame + vtkOpenFOAMReader；轻量自研 Inspector/Color Map |
| 完整 Color Map/Animation/通用属性引擎 | 预设色图与 Animation v1 已有；自由点编辑、关键帧和 server-manager 通用面板待做 |
| 多 harness CLI agent | 用户已收窄为 opencode；容器内隔离孵化仍未完成 |
| 完整多用户/团队服务器 | 个人组织与配额骨架已通；组织资源授权、seat/admin/OIDC、硬隔离和生产验收待做 |

当前功能证据和剩余优先级以 `parity-checklist.md`、`progress.md` 为准。
