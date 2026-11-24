# 输入封存与独立尝试目录（N2-02 / N2-03）

N3-06第四批增量：普通HTML/DOCX与独立compose已统一注册发布，移除旧内存latest/直接写稿分支；保留旧证据门禁、覆盖和原字节备份，补齐固定生成时间、两代幂等键、历史和跨API验收。普通文档双库各23项、工程report各23/compose各25、core14/契约4/最终兼容3通过，见 [ordinary-document-workflows.md](ordinary-document-workflows.md)。主线仍9完成/14实施中/30待实施，295项功能范围不变；下一单元N4-01进程身份，N3-06完整取消/恢复前置尚未关闭。

N3-06第三批增量：全部LaTeX入口已接通注册编译、动态资源封存、按主文件持久版本、PDF/SyncTeX/BBL内容验证与历史源码定位，双库各32项通过，见 [latex-workflows.md](latex-workflows.md)。HTML/DOCX各23项、compose各25项复验；主线仍9完成/14实施中/30待实施，295项功能范围不变。这一历史增量后的普通文档统一已由第四批完成；完整取消/恢复前置仍在N4/N5推进。

N3-06第二批增量：独立报告草稿已接入注册双通道、原字节备份、覆盖保护、持久历史与HTML/DOCX生成来源，双库各25项通过，见 [report-compositions.md](report-compositions.md)。HTML/DOCX双库各23项复验；主线仍为9完成/14实施中/30待实施，295项功能范围不变。接下来继续LaTeX注册，N3-06整项未关闭。

2026-11-02。当前接通显式输入清单的 Job API、内部工作流队列、实际执行器及受限 shell 重试。随后默认几何预览/导出接通隔离输入与持久产物发布，见 [geometry-publication.md](geometry-publication.md)。N2-03 仍在实施，其他专业步骤和恢复尚未全部接通；本页不关闭整项主线或功能任务。

## 使用和数据契约

`POST /api/v1/cases/{id}/jobs` 新增可选 `input_paths`，选择算例内 1–256 个文件或目录。例如：

```json
{
  "type": "shell",
  "command": "echo checking > result.txt",
  "runtime": "windows",
  "input_paths": ["0-caseDict", "C-run/case-1"]
}
```

命令在独立 `run/` 中执行，保持选中文件相对算例根目录的结构。输入选择会展开全部子文件和空目录，保留 `0`、`0.orig`、网格、选中目录内的重启时刻及其他文件；不会清理用户原件，也不会默认复制其他 runDir。调用方必须提供完整依赖选择；不推测任意 shell 的外部文件依赖。

后续增量新增 `input_publications`，可单独或与 `input_paths` 合用，从已发布版本复制所选文件并记录生产attempt/清单hash。默认要求当前有效版本，显式历史策略和重试均保留来源；同名输入拒绝覆盖，所有输入共用上限与配额。协议、启动/运行/收尾失效检查见 [publication-inputs.md](publication-inputs.md)。

没有 `input_paths` 的历史入口保持原行为和原请求哈希。加入输入清单后，幂等请求哈希包含该清单；同键相同请求回放原 Job 和原封存版本，改变清单返回409。旧 `run_dir` 求解证据读取活动算例，暂不允许与 `input_paths` 混用；专业版本化处理器在N3接入后再开放这一组合。

`GET /jobs/{id}` 在原 attempt 摘要中增加 `workspace` 与 `outputs`。只对有封存版本的尝试提供这些字段，沿用case归属授权。现有文件读取/下载可读取版本路径；尚无独立版本浏览器界面。工作流内部 `queue_step(..., input_paths=...)` 使用同一实现；当前公开工作流API和自动专业派发仍待完成。

目录布局：

```text
case/.vibeflow/attempts/{attempt UUID}/
  manifest.json
  inputs/       # 原始输入副本，受普通API写保护
  run/          # 执行专属的可写工作副本
```

数据库 `JobAttempt.input_manifest_json.workspace` 使用 `vibeflow.attempt-inputs.v1`，记录attempt ID、输入/运行相对目录、选择范围、文件字节数/SHA-256/可执行位、空目录、源case/revision/重试来源和宿主Python/平台/runtime。2026-11-05 起，新封存的工具链记录区分提交宿主与尚未观测的原生工具版本；正式处理器另存与执行规格、发布预留绑定的依赖声明。运行后的实际版本仍从经过验证的发布描述及工具链文件取得，不能把宿主Python版本当作OpenFOAM等专业工具版本。旧 `handler_not_registered` 清单保持原字节，详见[工具链来源记录](attempt-toolchain-provenance.md)。

## 提交、执行和失败行为

1. 提交持case控制锁，在同一DB事务创建Job/attempt/请求/事件。先枚举和hash，再检查两份副本所需存储配额。当前输入上限10000文件、1GiB，其他资源策略仍按N4推进。
2. 在 `.preparing-{attempt UUID}` 私有目录流式复制输入，逐文件核对长度/hash，再复核来源清单。复制保留空目录与可执行标记；拒绝穿越、运行元数据选择、符号链接、硬链接、reparse point和特殊文件。两份文件独立存储，未采用硬链接。
3. 从输入副本生成run副本，写manifest并flush/fsync文件，经OS无覆盖目录移动发布。发布完成后才把manifest关联到DB attempt。准备失败或请求取消等待复制线程完成，再清理本次持有的目录并回滚DB；不会删除已存在的同名目录。
4. 原子claim提交后、spawn之前，从DB读取manifest，先核验attempt/case绑定，再核对磁盘清单、全部封存输入及run副本；外来manifest、新增文件、输入损坏或元数据变化均拒绝启动。校验期间取消会等待线程结束，并持久化cancelled，不遗留本次启动的进程。损坏输入记录 `input_stale`。
5. 实际进程使用run目录为cwd；Windows进程的 `VIBEFLOW_CASE_ROOT` 指向该目录，WSL脚本当前按cwd访问输入。历史日志仍位于 `.vibeflow/jobs/`。执行结束后复核inputs，枚举运行输出，提交 `vibeflow.attempt-outputs.v1` 及 `attempt.outputs_captured` 事件，再进入既有终态/wait/SSE完成边界。原生命令修改封存输入会导致 `evidence_invalid`，不能报告成功。
6. 受限内部shell重试从旧attempt的inputs生成新UUID目录和新Job/attempt；不读取当前工作副本作为重试输入。旧输入、运行文件与日志保留。重试仍不自动派发，没有新公共重试端点；专业retry/resume策略继续由N5交付。

普通storage文本/二进制写入和删除、Agent文本写入、普通上传及分块创建/完成拒绝 `.vibeflow/attempts`，返回结构化409/`input_sealed`；Windows大小写/尾点与文件系统符号链接别名也检查。用户编辑工作副本后另行提交即可创建新版本，封存副本不被普通旧客户端静默覆盖。其他直接文件写入的专业适配器仍需逐项审查，不能把这组接线解释为全部写入口已闭合。

case/workspace复制和备份v1不携带attempt目录，避免把带旧case/attempt ID的文件迁移成新对象。备份v1不含工作流控制表，恢复校验拒绝这种执行目录；完整工作流灾备继续由后续版本交付。已有用户原件、普通文件和历史Job兼容行为保留。

## 验证与边界

证据统一在 `tmp/mainline-N2-attempt-evidence/`；`attempt-workspace-result.json`双库各23项，`case-write-result.json`双库各93项，`writes/summary.json`26/26，`compatibility/summary.json`5/5；临时PG均已删除。只读开发库仍13表/旧head，无schema变更或开发API重载；源码145源/168路由/233边界，Python入口183/offline25/writes26。未重跑完整offline/core/CAE或前端。首次测试误把preview预期为STL，已按scene/validation契约修正并保留失败来源。源码归档`platform/backups/mainline-N2-attempt-20261102.zip`。`attempt_workspace_smoke.py` 使用临时SQLite/PostgreSQL、独立fs、真实Windows/WSL子进程、实际CadQuery和HTTP应用请求。测试包括并发隔离、初场/空目录保留、输出清单提交、权限、封存写拒绝、幂等、重试原输入、输入篡改、启动与准备重复取消、配额、源变化、同名目录所有权及复制/备份边界。

当前明确保留：

- 任意本机shell仍可访问cwd以外的文件。该实现是应用协议和副本隔离，完整原生沙箱/进程树归属在N4/N7；manifest只能检测观察到的变化，无法排除外部ABA或最后一次检查后的写入。
- 通用attempt输出记录引用本次run目录，不自动成为当前成果。默认几何入口随后接通不可变发布、代次/head、兼容文件和B阶段证据；只有该受控发布流程可提供当前几何证明，不能用任意未发布的CadQuery输出推进阶段。其他产物类别继续按N2-05接入。
- 外层DB commit结果未知时保留已发布的attempt目录；提交后、派发前崩溃也需要恢复处理。N2-06/N5尚无完整提交日志、孤立目录对账或自动恢复，不能凭目录存在就自动执行或删除。
- 当前限本地fs。S3封存、跨主机同步、输入总量/磁盘空间的全局预留、完整工具链指纹、WSL/沙箱内环境路径统一、专业处理器输入选择及版本读取、前端选择/历史/重试交互均未完整交付。

下一单元接通专业处理器的受控输入选择和版本引用，推进N2-04失效判定与N2-05产物发布；同步补齐剩余直接写入入口的封存保护。继续保留53项主线与295项全功能范围。
