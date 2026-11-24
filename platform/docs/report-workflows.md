# 注册报告构建、不可变 HTML/DOCX 与历史读取

后续正式整合：报告/LaTeX提交及工程报告原生准备已移出case写事务；HTTP断线、幂等和来源重验通过本批隔离验证，详见 [document-admission.md](document-admission.md)。原有正式作业、独立内容验证与发布协议继续适用。

N3-06第四批增量：普通HTML/DOCX与独立compose已统一注册发布，移除旧内存latest/直接写稿分支；保留旧证据门禁、覆盖和原字节备份，补齐固定生成时间、两代幂等键、历史和跨API验收。普通文档双库各23项、工程report各23/compose各25、core14/契约4/最终兼容3通过，见 [ordinary-document-workflows.md](ordinary-document-workflows.md)。主线仍9完成/14实施中/30待实施，295项功能范围不变；下一单元N4-01进程身份，N3-06完整取消/恢复前置尚未关闭。

N3-06第三批增量：全部LaTeX入口已接通注册编译、动态资源封存、按主文件持久版本、PDF/SyncTeX/BBL内容验证与历史源码定位，双库各32项通过，见 [latex-workflows.md](latex-workflows.md)。HTML/DOCX各23项、compose各25项复验；主线仍9完成/14实施中/30待实施，295项功能范围不变。这一历史增量后的普通文档统一已由第四批完成；完整取消/恢复前置仍在N4/N5推进。

N3-06第二批增量：独立报告草稿已接入注册双通道、原字节备份、覆盖保护、持久历史与HTML/DOCX生成来源，双库各25项通过，见 [report-compositions.md](report-compositions.md)。HTML/DOCX双库各23项复验；主线仍为9完成/14实施中/30待实施，295项功能范围不变。接下来继续LaTeX注册，N3-06整项未关闭。 本次复验和历史HTML里程碑证据分开记录，见tmp/mainline-N3-compose-evidence。

验收结果：SQLite/PostgreSQL各23项；后处理双库各21项、几何工作流各15项复验，core14/14、计划/调度/快照兼容3/3、最终资源快照10项、旧入口兼容3/3通过。开发库13表/旧head，临时PG为0。归档 `platform/backups/mainline-N3-report-html-20261102.zip`；源码匹配记录见tmp/mainline-N3-report-evidence/current-source-match.json。

日期：2026-11-02。N3-06 正在实施。本单元接通 `report.build@1`：工作流在显式 E 确认后生成报告，全部既有 Markdown 构建入口使用同一处理器。独立report:compose后续已接通版本化与覆盖保护，见 [report-compositions.md](report-compositions.md)；全部LaTeX入口后续也已注册，见 [latex-workflows.md](latex-workflows.md)。无postprocess head的普通入口已统一，完整取消/恢复前置仍未关闭。

## 两种正文来源

工作流创建的 `target: "report"` 默认使用 `parameters.report_mode: "compose"`；也可明确指定 `"markdown"`。其余参数、预算、确认和幂等语义沿用 [registered-workflows.md](registered-workflows.md)。已有阶段决定起点；从 E 创建报告运行直接进入报告步骤，不重算已经确认的上游。

| 模式 | 正文输入 | 发布行为 |
|---|---|---|
| compose | 封存 caseDict、当前后处理指标/图件及网格质量，调用既有九章生成器 | 生成 `E-report/report.source-{job_id}.md` 和 HTML/DOCX，保留用户 `E-report/report.md` |
| markdown | 用户 `E-report/report.md`、Pandoc AST 中实际引用的本地图片/资源 | 封存后编译，另存本次正文版本，不回写用户原稿 |

自动正文优先使用 caseDict.caseMeta.title，否则使用 case ID 名称；当前不读取未封存的数据库显示名称。生成器读取封存指标的时间、来源和 readiness，因此重算正文稳定。自动正文保留既有章节与图件链接语义；复杂模板、自动章节/插图和多序列出版排版仍在全功能任务中。

`POST /cases/{case_id}/report:build` 继续编译用户 Markdown。全部请求均使用注册处理器，可带Idempotency-Key；有postprocess head时严格绑定当前工程来源，无head时保留旧证据门禁或普通文档语义。失效head不能回退。工作流报告始终要求注册工程来源。

## 同运行生产版本与输入封存

工作流报告必须消费同一运行中已经确认的 postprocess publication。E 确认提交后若准备失败，保留确认与 pending 派发状态；显式重试不能换用另一运行的新后处理版本。由 E 新建的报告运行可以选择当前有效版本。

准备阶段复核后处理当前 head、工作副本门禁、readiness 和递归上游依赖；后处理的 run_dir 必须匹配。封存 caseDict、完整 postprocess publication、同一有效依赖链的网格质量 publication，以及手写正文及所需本地资源。报告构建读取这些封存产物，不需要重新复制全部求解场。

正文图片引用经实际 Pandoc AST 解析，Markdown 图片和受支持的 raw HTML/SVG 图片引用进入资源快照。链接、重解析点、硬链接、越界路径和内部执行元数据不能作为注册报告输入。远程图片应先保存为 case 文件；带未嵌入外部 href/CSS 依赖的独立 SVG 文件在准入时拒绝。任意 HTML/SVG/CSS 的全部动态依赖与完整模板资源闭包尚不宣称已经支持，后续须继续明确支持矩阵并补实现。

工程源指纹覆盖 caseDict 的报告/后处理配置、错误记录等内容及手写正文/资源哈希，仅忽略 workflowState、provenance 和 caseMeta.status 等记账变化。上游 publication 另由持久 ID、清单哈希与递归 current 检查约束；只改指针资格而不改字节，也能使运行中的报告失效。

最终发布和当前资格查询还重新运行工作副本的后处理门禁，检查求解场、网格、独立参考、后处理产物及实际文件集合。因此直接绕过文件 API 改动这些工作文件，也会阻止构建中的报告发布，已有不可变报告则保留并标记 stale。封存侧通过已验证的 publication 证明上游计算，未为执行这项工作副本检查而复制全部求解场。

## 原生执行与产物校验

固定 `registered_report_worker.py` 在独立 attempt/run 中调用配置的 Pandoc。客户端不能传任意命令、argv 或 worker 路径。工具链记录 execution_id、case/job/attempt、Python、Pandoc 版本/可执行文件 SHA-256，以及 worker、编译辅助代码和生成器代码的 SHA-256。

HTML 和 DOCX 编译在 attempt 私有临时目录进行；该目录使用独占创建，清理仅针对本次拥有的目录。HTML 使用 standalone、embed-resources 和 MathML；DOCX 由同一规范化 AST 和资源生成。每次原生编译有超时，最终发布仍由现有 Job 所有者、取消和完整收尾协议决定。

发布验证重新读取封存文件、重建正文及 AST，再独立调用相同 Pandoc 编译：

- HTML 必须与重新编译的字节完全一致。
- DOCX 必须有唯一的 document part；所有 ZIP 条目内容、正文 XML、关系、图片及其他元数据均须与重编译一致，只忽略 ZIP 容器时间戳和 core.xml 中的 created/modified 两个生成日期字段。条目数上限 10000，解压后的总内容上限 256 MiB。
- 发布正文必须等于本次自动生成或封存的用户正文。
- 清单必须精确对应输入、AST 哈希、生产版本、执行身份与实际输出文件；工具链身份及代码/可执行文件哈希须匹配准备记录。
- 所有封存文件保持原字节，捕获后的产物哈希保持一致。

因此“进程退出 0”、复制旧文件或修改文档后重算产物哈希均不足以发布。重编译增加构建成本；大型文档性能、离线工具链封装和完整进程树取消仍待相应主线任务验收。

## 持久发布和读取

report 类别使用既有 ArtifactPublication/Head：当前代次决定活动版本，旧构建迟到可保留为 superseded 历史，失败、取消、源变化和上游失效保留上一份已发布文档。HTML、DOCX、正文副本、manifest 和 toolchain 都进入不可变 bundle；head 提交后才修复工作副本。

注册构建的 manifest.schema 为 `vibeflow.report-publication.v1`，inputs/artifacts 按相对路径映射记录；它不是旧兼容入口的 `vibeflow.report-build.v1` 快照数组。结果消费者应按 schema 读取。现有前端仅使用构建 job_id 和输出文件列表，不依赖旧清单内部结构；本单元未改前端，也未宣称版本选择 UI 已完成。

已有工作流状态 `succeeded` 与“当前报告”须分别判断：一个旧构建可以成功生成有效历史，但它的 publication.status 为 superseded，不能替换较新的报告。报告详情明确提供 current/stale。API重启后读取与版本选择使用数据库；全部HTML/DOCX、compose和LaTeX入口已替代进程内latest，普通文档规则见ordinary-document-workflows.md。

以下路径均有 `/api/v1` 前缀：

| 方法与路径 | 行为 |
|---|---|
| GET `/cases/{case_id}/report/results` | 当前不可变 manifest、工具链和 publication 元数据 |
| GET `/cases/{case_id}/report/results?publication_id=...` | 明确历史版本，保留 current/stale 说明 |
| GET `/cases/{case_id}/report/publications` | 最近 50 个版本的代次、状态、attempt 与当前标记 |
| GET `/cases/{case_id}/report/artifact?path=...&publication_id=...` | 下载该发布清单列出的 HTML、DOCX 或正文副本；省略版本则读取当前发布 |

未知、跨 case 或类别错误返回 404；不可变文件/清单损坏返回 409。通过受管理文件 API 编辑报告产物，即使写入相同字节，也会撤销当前 head；历史仍可显式查看，重新构建可修复当前版本。原 `/report/outputs` 和普通文件读取仍属于工作副本接口，工作流 UI 与版本化阅读入口还需在 N6/全功能任务中接通。

## 验收与后续范围

`server/report_handler_smoke.py --postgres` 使用隔离 API、SQLite/临时 PostgreSQL、真实 CadQuery、blockMesh/checkMesh、laplacianFoam、VTK 后处理和 Pandoc。它覆盖 E 确认、自动正文保留用户稿、带图片/公式的手写正文、跨 API 幂等、重启后的当前/历史读取、权限、坏不可变文件、空执行、伪造 HTML/DOCX/正文/工具链、源/图片变化、上游 head 失效、迟到/取消、准备失败后的生产者绑定与准入竞争。故障注入只在测试临时模块。

N3-06 已交付部分与剩余工作：

1. 独立report:compose的持久版本、覆盖前置条件、原字节备份与读取已由后续增量接通，双库各25项，见report-compositions.md；完整版本UI与崩溃对账仍按对应阶段推进。
2. LaTeX固定recipe、同运行来源、源/图片/参考文献封存及PDF/SyncTeX/BBL不可变发布已由后续增量验收，见latex-workflows.md；完整恢复与UI按原阶段继续。
3. 不同LaTeX主文件的持久通道、取消发布围栏与历史已实现；动态资源在声明的封存环境内求值，任意外部绝对文件读取拒绝发布。操作系统沙箱和完整进程树终止未宣称完成。
4. 普通HTML/DOCX和compose路径已统一；无工程成果时仍允许普通文档，保留门禁/覆盖/备份，双库各23项通过，详见ordinary-document-workflows.md。
5. 注册路径的本轮双库与旧阅读/诊断回归已通过；完整取消、发布崩溃对账和版本UI仍按前置与后续阶段推进，再评估N3-06整体关闭条件。

完整 Windows/WSL 进程树、所有者恢复、提交后崩溃对账、工作流 UI 和全部报告模板/编辑器能力继续保留在 N4/N5/N6 及 295 项全功能范围中。本单元没有数据库 schema 变化，不迁移开发库，不重载开发 API。
