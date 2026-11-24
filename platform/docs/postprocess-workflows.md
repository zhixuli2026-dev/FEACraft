# 注册后处理、版本化指标与 E 确认

2026-11-22，结果读取独立核对了文件系统与认证 SeaweedFS 的归档、版本和旧指针兼容。归档正文与来源校验现在在算例写事务外执行。[结果读取和并发实测](postprocess-result-read-capacity.md)

2026-11-06 N3-05后处理按原条件完成：SQLite/PostgreSQL各21项真实热传导提取与重算、参考对比、图件/指标防伪、来源变化、取消与迟到结果通过；进程退出0，临时数据库已清理。当前主线30/53完成，剩23（5进行中/18待开始）。N3-06报告各入口继续当前双库验收，N4-03机器入口清单已刷新但运行时验收尚未关闭；295项范围完整保留。

后续N3-06已接通自动组稿/用户Markdown的注册HTML/DOCX，绑定同运行已确认后处理版本，持久发布与下载见 [report-workflows.md](report-workflows.md)。下文“报告待注册”保留N3-05里程碑背景；独立compose/LaTeX仍在N3-06范围内。

验收结果：SQLite/PostgreSQL各21项；求解各25项、几何工作流各15项、输入封存各23项复验；core14/14、CAE3/3、最终准入兼容2/2与目录Windows7/Linux6通过。开发库仍13表/旧head，临时PG为0。源码归档 `platform/backups/mainline-N3-postprocess-20261102.zip` 及当前源码匹配证据位于本单元证据目录。

日期：2026-11-02。N3-05 将 `postprocess.run@1` 接入持久工作流：几何 → B 确认 → 网格 → C 确认 → 串行求解 → D 确认 → 后处理 → E 确认。报告的统一处理器与持久发布属于下一单元 N3-06；本单元先让现有报告入口遵守当前后处理版本门禁。

## 启动和输入

`POST /api/v1/cases/{case_id}/workflow-runs` 使用 `target: "postprocess"`。预算、revision、幂等键、归属与 [registered-workflows.md](registered-workflows.md) 相同。工作流到达 D 时等待显式确认；确认后准备后处理。已有阶段决定起点，到达 E 后再次提交相同目标只预检，不隐式重算整条流程。

同一运行的后处理必须消费该运行已确认的 solve publication。确认 D 已提交但准备失败时，响应保留 `workflow_dispatch.status=pending`，允许显式 dispatch；准备重试不能换成另一条运行产生的求解版本。由 D 阶段新建的运行可选择当前有效求解版本。

封存内容包括：

- 当前有效求解 publication 的全部场、时刻、日志、证据与工具链文件。
- 该求解记录的 mesh publication，继续递归验证其几何来源。
- caseDict、system、constant 中非网格输入、0 和存在的 0.orig。
- 配置引用的独立参考场文件。路径必须在 case 内，不能选择内部执行元数据；链接、特殊文件及不安全路径由封存清单检查拒绝。

求解 publication 的 run_dir、handler 和同运行生产者身份须匹配；caseDict 中显式 postProcessing.runDir 也须匹配。源指纹包含工程参数、规范化后处理配置和参考文件哈希。准备、启动、输出捕获及最终发布继续使用既有输入/版本复核协议。工作副本中的旧后处理结果不进入新尝试。

封存 manifest 的 source 新增 job_id；worker 从自己的 attempt manifest 取得 case/job/attempt 身份，生成独立配置文件，不接受 API 注入任意执行脚本。旧 manifest 的运行时读取仍兼容缺少此附加字段的历史记录。没有数据库 schema 变化。

## 固定执行与真实产物验证

`postprocess_handler.py` 使用固定 Python worker、run_dir 和本次 execution_id。`postprocess_worker.py` 在独立 attempt/run 中调用已有 VTK/NumPy 提取器；其工具链记录本次身份、worker/执行器代码 SHA-256，以及 Python、VTK、NumPy、Matplotlib 版本。

既有执行器拆出不发布文件的 `evaluate()`，保留探针、线采样、表面积分、体平均、频谱、POD、白名单公式、基础不确定度与参考比较语义。原 CLI 的 execute 仍负责生成版本目录及最近指针。图表使用 Agg、默认 rc 参数隔离和锁，固定 SVG ID salt 并去除 PDF/SVG 的时变日期，以便同一配置与数据可以复算相同字节。

发布验证独立复算，而不是相信 worker 的成功退出或自报哈希：

1. 封存文件在 run 中保持原字节，包括网格、初始场、求解输出与参考场。
2. 生成配置必须等于本次封存 caseDict、版本来源和 execution_id 推导出的配置。
3. 重新读取实际场，计算所有指标、比较、readiness 及 CSV/JSON/图件；数值结构与 PDF/SVG/PNG 等产物字节均须匹配。
4. 重建输入/输出清单、运行目录文件集合、来源和最近指针，检查 case/job/attempt 身份与代码哈希。
5. 受管理输出集合必须完整且没有未登记的后处理产物，捕获后的哈希必须仍匹配。

当前验证器在与 worker 相同的部署环境重算图件；工具链版本被记录，但尚未打包为离线可复现镜像。重算具有额外 CPU/内存成本；大型全场数据和长期性能验收仍在原计划内。

## 发布、诊断与确认

后处理 publication 包含 `D-post/configs/postprocess-{job_id}.json`、`D-post/data/postprocess-{job_id}/` 下的指标/CSV/图件/清单、最近指针和工具链。不可变目录、持久代次、当前 head、历史版本及工作副本修复沿用 [publication-inputs.md](publication-inputs.md) 的协议。失败、取消或源变化不替换当前有效结果；旧执行迟到只能保留为 superseded 历史，不能借新结果完成自己的 E 确认。

提取成功但真实参考比较未通过时，仍发布计算得到的诊断指标和 `report_ready=false`，供查看失败原因。此类成功表示“提取计算和证据有效”，不表示工程验收通过：E 确认与报告生成均被阻止。篡改指标、图件、比较或 readiness，即便同步重算文件哈希，也无法通过独立复算。

E 确认使用既有 caseDict:advance，携带 confirmed、revision、workflow_step_id 和 expected_step_version。门禁检查：

- 同一运行产生的当前 postprocess publication，不能通过 refresh-confirmation 换生产版本。
- 当前 head、不可变文件及递归上游的有效性。
- 全部工作副本产物、输入和独立参考的哈希，以及实际运行目录文件集合。
- readiness.report_ready 必须为 true。

原始文件编辑、额外输出时刻、被改网格或场、被改图件均会阻止确认。通过受管理文件 API 写入相同字节也会使 head 失效；单凭旧文件哈希完整不能冒充当前发布。

Job API 等待完整收尾和兼容副本修复后再暴露本地成功。工作流持久状态可能先于兼容副本修复提交；修复失败记录 `artifact.alias_repair_required`，不会谎报已提交发布被回滚。完整跨文件/DB 崩溃恢复仍由 N5 交付。

## 现有 API 与报告兼容

下列路径均使用 `/api/v1` 前缀：

| 方法与路径 | 行为 |
|---|---|
| POST `/cases/{case_id}/postprocess:run` | case 已建立 solve head 时使用注册处理器，可使用 Idempotency-Key；从未建立 solve head 的旧 case 保留原执行入口 |
| GET `/cases/{case_id}/postprocess/results` | 已建立 postprocess head 时读取当前不可变结果，附带工具链与 publication.current/stale |
| GET `/cases/{case_id}/postprocess/results?publication_id=...` | 明确读取历史不可变后处理版本 |
| GET `/cases/{case_id}/postprocess/results?job_id=...` | 注册作业返回对应不可变版本；旧作业保留既有结果读取兼容 |
| GET `/cases/{case_id}/postprocess/publications` | 最近 50 个发布的 ID、代次、状态、attempt 与当前标记 |

job_id 与 publication_id 不可同时指定。越权、跨 case 或类别错误返回 404；损坏发布返回 409。历史结果的 stale 表示已失去当前资格，不能据此推进阶段。创建和读取时，在 case 控制锁内决定是否使用注册版本，避免请求等待期间新发布出现而退回旧入口；后续发布仍在同一事务协调协议下校验。

现有 report:compose、HTML/DOCX build、LaTeX build 在受协调的准入阶段检查当前后处理门禁。HTML/DOCX 与 LaTeX 成功收尾时再次检查；处理中若当前后处理 head 被撤销，即使工作副本字节没变化，也不能发布新报告，上一份报告保留。既有正文、图片、LaTeX 源与工程输入快照检查继续生效。

报告暂存清理复用了 `fs_directory.remove_owned`：每次尝试均重新检查目录归属和链接，仅对 Windows 共享/锁冲突 32/33 做有界重试，其余错误直接上报。这解决实际回归中临时文件占用掩盖原始过期证据错误的问题，不扩大可删除的目录范围。

此兼容门禁不等于 N3-06 已完成。报告仍待注册处理器、同运行后处理生产版本绑定、统一持久发布代次与相应历史/重启语义。

## 验收范围与剩余任务

`server/postprocess_handler_smoke.py --postgres` 在隔离 SQLite/临时 PostgreSQL、独立 API 和文件根执行真实 CadQuery、blockMesh/checkMesh、laplacianFoam、VTK、NumPy、Matplotlib 与 Pandoc。使用 20×4×4 导热网格、解析 T(x)=300+100x、均温 350 K 和独立参考场，L2 容限 1e-6 K；验证实际 PDF/SVG/PNG。

测试覆盖显式 D/E 确认、版本/归属/历史/幂等、坏不可变文件、空 worker、伪造指标/图件/readiness、真实比较失败诊断、报告执行中 head 失效、准备失败后的生产者绑定、并发准入、参考/配置变化、迟到结果、取消和迟到失败不回退新确认。故障开关仅存在于测试临时模块。

已有 core 后处理制造解回归继续验证六类操作、FFT/POD、公式、基础不确定度及 comparisonTargets 类型关系。不能把本单元的真实稳态导热测试扩大解释为全部瞬态/实验/异网格场景已经实算验收。当前取消验证覆盖持久终态和不发布取消结果，挂起 worker 由夹具释放；完整进程树取消与恢复仍属于 N4/N5。

主线 N3-05 保持实施中，整体前置 N3-04/N2-05 尚未关闭；下一单元为 N3-06。全功能清单继续保留 295 项，实验/前次运行自动采集、异网格映射、数组公式、高级不确定度、多序列复杂图、原 postprocess.py 兼容、报告完整章节/插图、工作流 UI、跨存储事务与完整恢复均没有缩减。任务与证据以 [mainline-task-status.json](mainline-task-status.json)、[full-feature-tasks.json](full-feature-tasks.json) 及 `tmp/mainline-N3-postprocess-evidence/` 为准。

## N3-06 承接顺序

下列工作是下一单元的实施清单，尚未作为本单元成果验收。既有工作流计划已预留 `report.build`；当前报告 API、报告正文生成、Pandoc JSON/图片快照与 LaTeX 源复制分别在 reports.py、report_builder.py、report_build.py 和 latex_build.py。

| 顺序 | 任务 | 具体完成证据 |
|---|---|---|
| 1 | 定义并注册报告契约，明确自动 compose 与已有正文构建的输入及覆盖语义 | 客户端不能注入命令；用户编辑正文受 revision/hash 约束；自动生成不静默覆盖用户稿 |
| 2 | 固定同运行已确认的后处理生产版本，封存所需网格/指标/图件和工程配置 | E 确认后准备失败可显式重试，但不能借另一条运行的新后处理版本；历史输入可追溯 |
| 3 | 将 compose、HTML/DOCX 编译接到独立尝试与持久 publication | 真实 Pandoc 产物有正文、图片和工具链证据；空成功、坏文档、源变化和迟到构建不能覆盖当前报告 |
| 4 | 接入 LaTeX 固定 recipe、源/图片/参考文献快照及 PDF/SyncTeX 产物 | 三种现有 recipe 和 biber 兼容；动态/绝对外部依赖边界明确，不能将未冻结文件算作已冻结 |
| 5 | 统一当前/历史报告读取与旧入口，逐步替代进程内 latest 标记 | 双 API 竞争及 API 重启后仍按持久代次选择当前结果；旧报告可按版本读取 |
| 6 | 补齐取消、过期、坏产物、失败清理和确认生产者故障矩阵 | SQLite/PostgreSQL、真实报告编译和旧阅读/下载路径回归；原稿和上一份有效报告保留 |

报告 worker 的受控取消/发布先沿用当前契约，完整 Windows/WSL 进程树终止、孤儿扫描、提交后崩溃对账和 resume 仍须在 N4/N5 完整实现，不能因报告链路接通而提前关闭。
