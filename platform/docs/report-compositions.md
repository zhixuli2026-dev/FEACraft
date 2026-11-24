# 独立报告草稿、版本与覆盖保护

N3-06第四批增量：普通HTML/DOCX与独立compose已统一注册发布，移除旧内存latest/直接写稿分支；保留旧证据门禁、覆盖和原字节备份，补齐固定生成时间、两代幂等键、历史和跨API验收。普通文档双库各23项、工程report各23/compose各25、core14/契约4/最终兼容3通过，见 [ordinary-document-workflows.md](ordinary-document-workflows.md)。主线仍9完成/14实施中/30待实施，295项功能范围不变；下一单元N4-01进程身份，N3-06完整取消/恢复前置尚未关闭。

N3-06第三批增量：全部LaTeX入口已接通注册编译、动态资源封存、按主文件持久版本、PDF/SyncTeX/BBL内容验证与历史源码定位，双库各32项通过，见 [latex-workflows.md](latex-workflows.md)。HTML/DOCX各23项、compose各25项复验；主线仍9完成/14实施中/30待实施，295项功能范围不变。这一历史增量后的普通文档统一已由第四批完成；完整取消/恢复前置仍在N4/N5推进。

最终验收：SQLite/PostgreSQL各25项；既有HTML/DOCX各23项、core14/14、契约/发布输入/快照4/4通过，见tmp/mainline-N3-compose-evidence。源码归档platform/backups/mainline-N3-compose-20261102.zip，匹配记录current-source-match.json。此归档做完整哈希核对，未另作恢复目录重跑。

日期：2026-11-02。这是 N3-06 中独立 `report:compose` 的实施记录。HTML/DOCX 构建见 [report-workflows.md](report-workflows.md)，后续LaTeX注册执行与发布见 [latex-workflows.md](latex-workflows.md)。无postprocess head的普通compose路径也已统一，N3-06完整取消/恢复前置仍在实施。本文不缩减主线53项或全功能295项范围。

## 准入与封存

`POST /api/v1/cases/{id}/report:compose`始终使用注册处理器。控制锁内区分注册工程来源、旧证据和无后处理的普通文档；失效head不能绕过门禁。默认仍等待收尾后返回完整正文，超时保留持久任务。

| 输出 | 处理器 | 持久发布类别 |
|---|---|---|
| `E-report/report.md` | `report.compose@1` | `report-source` |
| `E-report/report.generated.md` | `report.compose-generated@1` | `report-source-generated` |

两个输出独立保留当前指针和代次。准备阶段检查当前同 run 后处理、当前网格质量及其递归上游，冻结 caseDict、这些不可变成果、算例名称和原目标文件。已建立工程head时，普通未注册文件、旧结果或手工修改的后处理别名不足以通过准入。无head的普通文档/旧证据规则见ordinary-document-workflows.md。

原请求字段保持有效：`output`、`overwrite`、`expected_revision`、`expected_sha256`。目标含非空白内容时必须明确 `overwrite: true`；修订号/哈希不匹配均在创建 Job 前拒绝。省略哈希表示无该条件，显式 `null` 表示要求文件不存在。已有空白/空文件允许默认生成，但仍保存原始字节作为备份。

可传 `Idempotency-Key`。请求摘要包含处理器、规范化输出、覆盖选项、修订号、哈希的存在性和值。同 case/用户/键再次提交先找原 Job，因此成功后的旧文件前置条件不阻止读取原结果；不同请求复用键返回 409。它不重新生成或覆盖后来人工修改的正文。

## 执行与独立验证

固定 `registered_compose_worker.py` 在独立 attempt/run 内调用既有九章生成器，无需 Pandoc。用户不能提交任意命令或 worker 路径。每次使用独立 UUID 目录：

```text
E-report/compositions/{execution_id}/report.md
E-report/compositions/{execution_id}/result.json
E-report/compositions/{execution_id}/toolchain.json
E-report/compositions/{execution_id}/previous.md   # 原文件存在时
```

`result.json` 保留 `content/chapters/gate_reason/evidence/path/backup`。`path` 是原目标路径，`backup` 是本次独立备份。备份按字节保存，包含 CRLF、非 UTF-8 内容与空文件；不会通过解码再编码改变旧稿。toolchain 记录 case/job/attempt/execution、名称、输出、全部输入哈希、上游发布、原目标哈希、产物哈希和 worker/生成辅助代码/章节生成器的哈希。

收尾重新检查全部封存文件，独立生成正文、结果元数据、原稿备份和工具链，并要求实际字节及捕获哈希一致。因此空执行、伪造正文/备份或修改后重算清单不能发布。输出写入不可变 bundle 后，最新代次才能竞争当前指针；迟到的有效旧结果只保留为 superseded 历史。

## 写回保护与成功含义

最新代次提交前再次检查原目标的存在性、大小和 SHA-256。提交后写回时，原目标必须仍等于原文件，或已等于本次生成字节；所有独立历史文件必须不存在或已经字节相同。路径逐级拒绝链接、硬链接、重解析点和越界。先镜像历史/备份，最后写正文；重复修复保持幂等。

受管理 API 在生成期间编辑目标会改变发布代次；直接文件编辑会使目标检查失败。提交与写回之间出现正文/备份修改时，保留该修改和已提交的不可变版本，作业记录 `artifact.alias_repair_required`。这不是完整 FS/DB 崩溃事务：外部程序不服从控制锁时仍存在检查与写入间的竞争窗口，提交后断电/进程崩溃的自动对账属于 N5，本文不宣称已完成。

结果中的三个标记应分别读取：

| 字段 | 含义 |
|---|---|
| `publication.current` | 数据库当前指针选中此版本 |
| `publication.applied` | 此版本仍是当前有效版本，正文及全部镜像文件与发布字节一致 |
| `publication.stale` | 已非当前、工程/上游变化或未完整写回 |

Job succeeded 表示不可变版本已验证并保存，并不保证此时仍可覆盖工作副本。首次同步请求只有 `applied=true` 才按生成成功返回；有效历史但未应用返回 409 `composition_not_applied`，包含 Job/版本标识。相同幂等键回读旧成功结果可返回 `applied=false`，不谎称重新写回。

## 查询与编译衔接

默认同步请求等待作业完整收尾；`?wait=false` 立即返回 `job_id/status/publication_id/generation/path`，可使用既有 Job 查询和取消入口。默认等待 20 秒后仍在执行则返回 504 `composition_pending`，附上述标识；不返回缺少章节的假成功，不取消持久 Job。调用方应查询 Job 和版本结果，或用原幂等键回读。

| 接口（前缀 `/api/v1/cases/{id}`） | 行为 |
|---|---|
| GET `/report/compose-result` | 当前主草稿；`output=E-report/report.generated.md` 选择另一个通道 |
| GET `/report/compose-result?publication_id=...` | 明确历史版本，包含正文、备份、工具链、Job 与 current/stale/applied |
| GET `/report/compositions` | 两个通道最近 50 个发布记录与当前标记 |
| GET `/report/compose-artifact?path=...&publication_id=...` | 下载该版本正文或旧稿备份；不接受任意文件路径 |

未知/跨算例/错误发布类别返回 404；不可变证据损坏返回 409。工作副本编辑撤销当前 head，但不删除历史版本；即使是同字节的受管理写入，也明确转为用户稿。

当 `report:build` 或工作流 markdown 模式消费尚未人工编辑、已完整应用且仍有效的自动草稿时，会额外绑定该 composition publication，冻结其正文来源。用户编辑后的草稿作为显式用户输入编译。仍匹配自动正文但其上游已过期时，须先重新组稿，不能把旧自动结论冒充新结果。

## 验收边界

`server/compose_handler_smoke.py --postgres` 使用独立 SQLite 和临时 PostgreSQL、真实 CadQuery/OpenFOAM/后处理以及原生 Pandoc，测试覆盖前置条件、原字节备份、两个通道、幂等、历史权限与损坏、报告编译来源、双 API 竞争/读取端重启、伪造产物、源变化、迟到、取消和提交后镜像冲突。故障注入仅在临时测试模块，不在产品接口提供测试开关。

本次不增加 schema，不迁移或重载开发环境。完整进程树终止、运行 worker 崩溃恢复、发布事务日志、版本 UI 和所有报告模板/复杂资源仍按 N4/N5/N6 及全功能计划推进。具体通过项数与证据路径写入主线状态和进展记录后，以记录的最终版本为准。
