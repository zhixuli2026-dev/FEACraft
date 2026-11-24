# 普通报告入口统一与证据兼容

后续正式整合：报告/LaTeX提交及工程报告原生准备已移出case写事务；HTTP断线、幂等和来源重验通过本批隔离验证，详见 [document-admission.md](document-admission.md)。原有正式作业、独立内容验证与发布协议继续适用。

N3-06 第四批增量，2026-11-02。普通 HTML/DOCX 和独立组稿已接入同一持久作业、封存输入、原生验证、发布代次与历史读取机制。普通文档SQLite/PostgreSQL各23项、工程HTML/DOCX各23项、工程compose各25项、core14/14、契约4/4和最终兼容3/3通过；证据见tmp/mainline-N3-ordinary-document-evidence。本文不改变主线 53 项和全功能 295 项的范围。

## 准入规则

`POST /api/v1/cases/{id}/report:build`、`report:compose` 现在始终通过静态注册处理器；不再根据 postprocess head 是否存在选择旧内存 latest 或直接写稿分支。LaTeX 已在上一增量统一，见 [latex-workflows.md](latex-workflows.md)。PPT、PDF 页码等其他专业任务仍按原计划推进。

| 输入情况 | 规则 |
|---|---|
| 没有 postprocess head，未配置需证据的后处理 | 允许普通文档，不要求虚构计算结果；组稿明确保留缺少指标、参考与不确定度的说明 |
| 没有 postprocess head，已配置后处理 | 继续校验旧证据的配置/工程指纹、输入/产物哈希、实际目录集合、参考文件及 readiness |
| 存在 postprocess head | 必须绑定当前有效、同 run 的后处理发布及网格质量，递归验证上游；失效 head 不能退回普通路径 |
| 工程工作流报告 | 始终要求注册工程来源；指定生产版本时必须仍为当前版本，不能退回普通文档兼容或换成另一生产者 |
| 普通文档执行期间首次建立 postprocess head | 当前发布资格失效，须重新提交以绑定新工程来源；不可变历史仍可读取 |

检查、输入选择和发布预留均在 case 控制事务内进行。普通路径在最终发布和当前版本查询时再次运行数据库门禁；因此只改变 head 资格、没有改文件字节，也不能绕过检查。

## 输入与正文稳定性

普通 Markdown 构建封存 caseDict、正文和 Pandoc AST 实际引用的本地图片。若有旧后处理指针，还封存指针、metrics 和 artifact-manifest。普通组稿另外封存存在的 `B-mesh/quality.json`；该文件的新增、删除或字节变化均影响当前资格。已注册工程输入继续从其不可变 publication 消费。

旧证据所关联的完整求解场无需全部复制到文档 attempt，但准入、当前资格和最新代次发布仍核对其实际输入/输出记录。文件、指针和上游验证不因普通文档兼容而放宽。固定 worker 在 attempt/run 内读取封存资料，最终正文和产物仍须独立重建一致。

每个新任务在准备阶段记录 UTC `generated_at`，通过已校验的固定 worker 参数传递，并进入描述符和工具链。组稿优先使用后处理证据时间；没有该时间时使用本次任务固定时间。独立复验不会再次取当前时间而产生不同正文。旧已注册工程描述符和 worker 参数格式继续可读。

## 幂等、覆盖和历史

HTML/DOCX 的请求摘要只包含显式固定操作，不再把当前可变 run_dir 当成用户请求参数。重复请求先查原任务，再校验新输入；原任务成功后正文缺失、caseDict 改变，也不阻止按相同键读取原任务。旧版本含 run_dir 的请求摘要按原任务封存描述符严格兼容；其他操作复用相同键仍返回 409。新提交和重放均返回 job_id、publication_id、generation 与状态。

组稿保留 `output/overwrite/expected_revision/expected_sha256`：非空白草稿需要明确覆盖；省略哈希表示不加该条件，显式 null 要求目标不存在。全部已有字节均备份，包括空文件、CRLF 与非 UTF-8 内容。备份位于 `E-report/compositions/{execution_id}/previous.md`；新草稿版本不再使用旧 `report.backups` 命名。

默认组稿等待完整收尾并返回原有章节/正文/备份字段；`wait=false` 返回持久任务标识。超过 20 秒仍未完成返回 504 和可查询任务标识，任务继续运行。是否当前、是否过期、是否已写回分别由 current/stale/applied 表达。首次请求未能写回不会报生成成功；重放可返回原历史及 applied=false，不覆盖人工改稿。

HTML/DOCX 清单统一为 `vibeflow.report-publication.v1`，inputs/artifacts 是路径映射；消费者应按 schema 读取。现有输出列表与文件下载路径保留。详细版本接口见 [report-workflows.md](report-workflows.md) 和 [report-compositions.md](report-compositions.md)。旧 `report_build.py` 快照辅助函数仍供兼容测试使用，生产路由已移除 `_REPORT_LATEST`、旧报告启动/收尾和同步写稿分支。

未编辑的自动稿进入 HTML/DOCX 编译时绑定其 composition publication。质量或工程证据变化使其过期，必须重新组稿；人工编辑的草稿仍作为明确的用户输入。有效但迟到的旧结果保存为 superseded 历史，不改新版本。失败、取消和输入过期均不能替换当前报告。

## 验收与后续任务

`server/ordinary_document_smoke.py --postgres` 使用独立 SQLite/临时 PostgreSQL、两套 API、真实 Pandoc，以及只存在于临时测试模块的文件屏障和产物篡改。覆盖普通内容/图片/公式、无后处理组稿固定时间、原字节/空文件备份、两个通道、生成稿来源、两代幂等键、人工稿、跨 API 提交、读取端重启、伪造产物、源/资源/质量变更、迟到/取消，以及执行中建立无效工程 head。所有临时 PostgreSQL 数据库按精确归属创建和删除。

另复验真实工程 report/compose 双库套件和 core，特别包括旧后处理证据组稿、HTML/DOCX 原生内容、LaTeX 生命周期；计划、发布输入、资源快照和调度契约定向复验。最终数量和证据目录以阶段台账为准。

本增量不修改数据库 schema，不迁移或重载开发 API。API 读取端重启不等于运行 worker 崩溃恢复；取消发布屏障不等于全部原生进程树已退出。下一单元是 N4-01：持久进程身份与归属验证，然后按 N4/N5 完成树终止、所有者恢复和 FS/DB 崩溃对账。完整版本 UI、模板与编辑器能力继续保留在 N6 和全功能计划中；N3-06 整项暂不关闭。
