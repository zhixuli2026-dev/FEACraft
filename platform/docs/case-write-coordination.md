# Case 写入协调与冲突协议

N3-06第四批增量：普通HTML/DOCX与独立compose已统一注册发布，移除旧内存latest/直接写稿分支；保留旧证据门禁、覆盖和原字节备份，补齐固定生成时间、两代幂等键、历史和跨API验收。普通文档双库各23项、工程report各23/compose各25、core14/契约4/最终兼容3通过，见 [ordinary-document-workflows.md](ordinary-document-workflows.md)。主线仍9完成/14实施中/30待实施，295项功能范围不变；下一单元N4-01进程身份，N3-06完整取消/恢复前置尚未关闭。

N3-06第三批增量：全部LaTeX入口已接通注册编译、动态资源封存、按主文件持久版本、PDF/SyncTeX/BBL内容验证与历史源码定位，双库各32项通过，见 [latex-workflows.md](latex-workflows.md)。HTML/DOCX各23项、compose各25项复验；主线仍9完成/14实施中/30待实施，295项功能范围不变。这一历史增量后的普通文档统一已由第四批完成；完整取消/恢复前置仍在N4/N5推进。

N3-06第二批增量：独立报告草稿已接入注册双通道、原字节备份、覆盖保护、持久历史与HTML/DOCX生成来源，双库各25项通过，见 [report-compositions.md](report-compositions.md)。HTML/DOCX双库各23项复验；主线仍为9完成/14实施中/30待实施，295项功能范围不变。接下来继续LaTeX注册，N3-06整项未关闭。

日期：2026-11-02。对应 N2-01，以及 N2-02 的首批入口接入。主线和全功能状态以 [执行台账](mainline-task-status.json) 为准。没有将本次并发写入验收等同于 N2 全部完成。

## 当前实现

`server/app/services/case_write.py` 使用与 Job/工作流控制相同的数据库 case 控制行锁。SQLite 和 PostgreSQL 都通过数据库协调多个 API 进程；没有用 Python 线程锁冒充跨进程锁。

顺序为：先完成归属授权 → 建立外层事务 → case 控制行锁 → 刷新 Case 镜像 → 校验 revision/文件 SHA-256 → 读取并计算修改 → 写入文件/镜像/事件/索引 → 调用方提交。协调器使用 savepoint，但不自行 commit；离开上下文后，锁持续到外层事务提交或回滚。SQLite 显式启动父事务，防止释放 savepoint 意外提交。

`expected_revision` 必须是非负整数。文件条件映射中的路径省略表示不检查该文件；显式 `None` 表示要求文件不存在；64 位小写 SHA-256 表示内容必须匹配。相对路径校验拒绝越界、盘符和冒号。冲突发生在文件副作用之前，不自动重试旧请求。

API 冲突返回 HTTP 409，`detail` 包含 `code`、`message`、`path`、`current_revision`。目前包括 `revision_conflict`、`file_conflict`、`case_unavailable`、`workspace_unavailable` 和 `workspace_conflict`。Agent 保留现有 SSE 工具结果协议，冲突表现为 `ok=false` 的 `CaseWriteConflict`，不将错误伪装为成功审批。

## 已接入的入口

| 入口 | 校验与行为 |
|---|---|
| GET `/caseDict` | 锁内读取文件、当前 revision 与 `content_sha256`，等待其他协作写入者提交 |
| PUT `/caseDict` | 可选 `expected_revision` 和 `expected_sha256`；原阶段保存门禁保留 |
| PUT `/caseDict/block` | 相同可选条件；取得锁后才读取和替换块，保留其他块及新注释 |
| 扩展 PUT/DELETE、错误 PATCH | 相同锁和冲突条件；DELETE 条件在 query；条件字段不会写入 errors 业务条目 |
| POST `/caseDict:advance` | 必需 revision 在锁内再次检查，再验证显式确认、顺序与专业证据；工作流确认仍检查 step 版本和快照 |
| Job 失败回退 | 锁内刷新 Case 后判断是否回退，保留当前文件内容；失败不能推进阶段 |
| Agent 全权限直接提案与批准提案 | 共用 `ToolExecutor.apply_proposal`；锁内复核持久提案哈希，然后写文件或替换文本；新建提案的空基线要求目标不存在 |
| GET `/file` 的文本响应 | 锁内返回文件内容、`content_sha256` 和 `dict_revision` |
| PUT `/file` | 可选 revision/hash；这里 hash 字段省略表示旧请求无条件写，显式 null 要求文件不存在；先提交再触发几何预览 |
| DELETE `/file`、普通上传 | 协调锁，可选 query revision/hash；caseDict 仍要求专用入口 |
| 分块上传创建/状态/chunk/完成/取消 | 同一 case 锁覆盖会话文件及发布；创建时记录目标 SHA-256/不存在基线，完成时重查，防止 `overwrite=true` 覆盖上传期间的新内容 |

分块上传的目标版本冲突会保留当前文件及已上传的分块，用户可取消旧会话后重新上传。缺少目标基线的旧上传会话拒绝完成，需重新创建。文件本身的 SHA-256 校验、offset 连续性、16 MiB 普通上传限制、Range 下载继续保留。

新 case 导入/复制中的初始字典写入发生在对象首次提交、其他 API 尚不可见之前，不是现存 case 的并发编辑入口。复制源/备份与成员协调、首批专业API写入已取得后续增量证据（见下文）；原生进程直接写文件和剩余产物入口仍待接入，不能认为所有文件均受锁保护。

## 已有证据

`server/case_write_smoke.py --postgres` 使用两个真实 API 进程共享同一个临时数据库和文件根。测试专用应用在文件替换后、数据库提交前设置屏障；生产 API 没有测试开关。SQLite/PostgreSQL 各 21 项通过，证据：`tmp/case-write-evidence-t6zu0qed/result.json`，临时 PostgreSQL 已删除。

- 同 revision 的两个编辑者只有一个成功；失败者不写文件、不增加 case 事件。
- 读请求在提交屏障前等待，提交后内容、hash 和 revision 一致。
- 外部编辑不增加 DB revision 时，哈希仍能拒绝覆盖；重新读取后可保存。
- 块编辑、扩展修改/删除、错误生命周期和阶段推进在等待锁后拒绝旧 revision；无条件局部编辑也会基于最新内容计算。
- Agent 全文件提案和文本替换批准等待锁后复查基线，保留编辑器新内容；新鲜提案只增加一次 revision。
- 普通文件创建、保存、删除校验 hash/不存在条件；分块上传冲突保留当前目标和分块，目标未变时成功发布。
- 原错误生命周期、扩展 schema、提案审批及高级上传四套完整兼容测试通过，见 `tmp/mainline-N2-write-evidence/write-compatibility/summary.json`。
- 原阶段/Job/报告核心 14 套通过，见 `tmp/mainline-N2-write-evidence/core/summary.json`；调度核心双库各 20 项复验通过，见 `tmp/workflow-scheduler-8jmtan9s/result.json`。

协调器里程碑的完整回归 `tmp/mainline-N2-write-evidence/` 中 offline20/20、core14/14、write-compatibility4/4、真实CAE3/3 均通过。该快照没有前端改动；后续文本编辑器验收如下。

运行命令（从 `platform/server`）：

```powershell
.\.venv\Scripts\python.exe -X utf8 case_write_smoke.py --postgres
.\.venv\Scripts\python.exe -X utf8 ..\tools\run_regression.py --profile writes
.\.venv\Scripts\python.exe -X utf8 ..\tools\run_regression.py --profile offline
.\.venv\Scripts\python.exe -X utf8 ..\tools\run_regression.py --profile core
.\.venv\Scripts\python.exe -X utf8 ..\tools\run_regression.py --profile cae
```

默认离线集已包括 `case_write_smoke` 的 SQLite 验证。新增 `writes` profile 自建临时 API/DB/fs，不能将这些旧兼容脚本直接指向开发服务。测试支持显式 PostgreSQL URL 时只接受 loopback 和严格命名的 `tmp_vf_*_<32hex>` 临时数据库。

## N2-02 文本编辑器接入

`web/src/store.ts` 的普通文本保存提交打开时的 SHA-256，caseDict 还提交对应 revision。两个 PUT 响应返回实际提交内容的 `content_sha256`，编辑器只依据该回执更新保存基线，不通过事后 GET 将其他人的新版本误认成自己已保存的版本。整文 caseDict 打开使用专用 GET，文件内容和 revision 来自同一次受控读取。

保存中的重复请求不再发送。等待响应期间继续输入的内容保持 dirty；保存完成只确认本次提交的文本。切换算例、关闭再打开同一路径后，旧响应不能更新新编辑器。保存后的文件树/镜像刷新失败也不会把已提交的内容误报为保存失败。

`ApiError` 保留 HTTP 状态及结构化错误，界面显示可读原因。revision/hash 冲突保留草稿并提供服务器版本查看；查看本身不改变基线。用户可以保留草稿、基于刚查看的版本继续编辑，或确认放弃草稿后使用服务器版本。下一次保存仍检查版本，查看后再次发生变化会再次冲突。普通保存试图更改工程阶段时显示原阶段门禁原因，不误报为外部修改。程序载入 Monaco 内容不会额外触发一次用户编辑。

- `tools/editor-write-smoke.mjs` 11 项通过，直接验证真实 Zustand store/API 模块的响应竞争、草稿保留、重复保存、跨 case/编辑器隔离、全部保存、刷新失败和门禁错误分类。结果：`tmp/mainline-N2-editor-evidence/editor-write-result.json`。
- `tools/playwright-editor-write.js` 通过 Playwright MCP 在独立 UI/API/SQLite/fs 上运行，4 项浏览器场景通过：真实 HTTP 409、CJK/emoji 草稿、查看后条件保存、实际提交后延迟响应时继续输入、caseDict revision 冲突与显式重载、阶段门禁。结果：`tmp/mainline-N2-editor-evidence/browser-result.json`；浏览器版本记录在结果中，输入采用真实 Monaco 模型操作，不宣称物理键盘/触屏验收。
- `npm run build` 通过，日志 `tmp/mainline-N2-editor-evidence/frontend-build.log`；现有大 chunk 提示仍在。1440×1000 截图 `editor-conflict.png`、`editor-saved.png` 位于同目录，已目视修正服务器内容面板被挤压的问题；impeccable 检查结果为空。移动端、屏幕阅读器和其他浏览器本轮未测。
- 加入 PUT 回执断言后，后端双数据库各 21 项复验通过：`tmp/case-write-evidence-r5yfef9v/result.json`。此前协调器完整回归继续作为基线，未因新增响应字段重复全部 CAE 计算。

新版编辑器替代原 mtime 后确认覆盖的旧交互；`e2e-wp02.mjs` 对应期望已同步，但这份依赖旧开发 fixture 的完整脚本没有在本轮执行。新功能使用上述独立浏览器脚本验收。

本次追加源码快照为 `platform/backups/mainline-N2-editor-20261102.zip`，完整协调器前置快照保持原样。N2-02 仍在实施，不能据整文编辑器通过关闭全部入口任务。

## N2-02 块/错误表单与 LaTeX 插入接入

`useCaseDraft.ts` 固定每份草稿对应的 revision/hash。面板因其他表单保存而刷新时，未提交的草稿和旧基线一起保留；下一次保存出现真实 409，不自动拿新基线覆盖服务器。块和错误表单支持查看服务器内容、显式保留草稿继续编辑或确认放弃草稿。服务器删除错误条目时，未保存草稿仍显示，直到用户明确放弃；不能把已不存在的条目当成可提交对象。

块 PUT、扩展 PUT/DELETE、错误 PATCH 的成功回执均增加实际提交内容的 `content_sha256`，并保留 `dict_revision`。表单只确认本次提交的草稿；等待响应时继续输入仍为未保存。提交期间禁用重复动作，输入保持可编辑。切换 case 后，旧读取或保存后刷新不会替换新 case 面板。可选错误字段清空会实际写入，不再被省略后误报已保存。草稿目前保留在组件内存中，不承诺页面重载或主动切换 case 后持久恢复。

LaTeX 表格插入先读取源文件及 hash，再用随机唯一名和 `expected_sha256:null` 创建备份，最后按读取时 hash 条件写回。读取后源文件变化时保留服务器新文件、已有备份和表格片段；用户重试会重新读取。备份和目标更新是两次写入，不是多文件原子事务。插入使用回调替换文本，避免表格里的 `$` 被字符串替换规则解释。

本轮真实浏览器发现并修复了序列化缺陷：空字符串写成空列表、无空白中文/特殊字符无法往返、布尔值大写导致阻断计数丢失。字符串采用适当引号/转义，布尔值写小写；旧 `True/TRUE` 仍参与 blocking 计数；空白/全空嵌套修复证据不能通过 `fixProposed`。保留旧调用方直接传单个已加引号 OpenFOAM token 的语义，避免后处理 metric 名称重复转义。该修复不是完整 OpenFOAM 语法解析器验收。

证据目录 `tmp/mainline-N2-forms-evidence/`：

- `browser-result.json`：`tools/playwright-form-write.js` 的 9 项真实 API 浏览器验收，包括面板刷新、查看后再次变化、保存中输入、错误条目删除、清空字段/空证据、CJK/引号、块切换确认、LaTeX备份/冲突/重试和跨case迟到响应。
- `editor-write-result.json` 11 项状态复验、`editor-browser-result.json` 4 项原文本编辑器浏览器复验通过。表单浏览器使用真实 input/textarea，编辑器仍用 Monaco 模型输入；桌面 Chromium 156、1440×1000，未测移动端或其他浏览器。
- `frontend-build.log` 通过，有原有大chunk提示；`impeccable-detect.json` 为空。`form-conflict.png`、`form-latex-saved.png` 已目视检查；既有宽面板/横向滚动与其他未重做界面不因此视为全量UI验收。
- `offline/summary.json` 20/20；`write-compatibility-final/summary.json` 4/4。`core/summary.json` 首轮12/14，发现已加引号token的兼容问题；修复后 `core-quoted-compatibility/summary.json` 后处理/报告2/2补验通过。`core-consolidated.json` 明确记录两轮合并，不能称为同一次无失败14/14。新增序列化/空证据离线断言有 `casedict-schema-unit-final.log`。
- 后端双API/SQLite/PostgreSQL各21项再次通过：`tmp/case-write-evidence-mo8xgmc6/result.json`，覆盖新增局部回执hash/revision；临时PG已删除。三条完整CAE仍使用前一协调器证据，本轮未重复整条计算。
- `developer-schema-readonly.json` 只读确认开发库仍13表、旧 `6fb6c18fb7ee`，无临时PG残留。没有schema变更，当前代码仍22业务表/head `4c02b1a7d9e0`；开发API未重载。

源码追加归档 `platform/backups/mainline-N2-forms-20261102.zip`，保留之前两个N2快照。N2-02继续实施，整项FF未关闭。

## N2-02 文件树条件写入与分块客户端

`CaseFileActions.tsx` 替换文件树内直接上传/删除的 prompt。先保留所选本地文件和目标路径，检查服务器版本，再显式上传/替换或删除。取消不会被解释成上传到根目录；检查失败不能发送无条件写。冲突保留选择，重新检查不自动提交。删除不会自动关闭已打开的编辑草稿，后续编辑器保存仍受原hash约束。

新增 GET `/file:version?path=`，在协调锁内返回规范路径、是否存在、字节数、SHA-256、dict revision及保护标记，文本/二进制均可读取，缺失返回exists=false/hash=null。目录/越界路径422、无权限404。普通上传增加 query `expected_absent=true`，与`expected_sha256`互斥；成功回执含提交hash。分块创建 body增加可选`expected_sha256`，省略兼容旧请求，显式null要求不存在；既检查用户查看时的基线，也保留完成时的原目标复核。

普通文件写/上传/删除、分块创建/完成及Agent字典写回统一识别本地caseDict别名，包括Windows大小写/尾点和现存同文件别名。普通入口拒绝修改，Agent仍走受阶段门禁约束的镜像回调。新保护不等于任意shell/外部编辑器受控；S3大小写不同的key仍按各自对象处理。

文件树对≤16MiB使用普通上传，更大文件使用现有fs分块API，每块4MiB，配置上限1GiB。支持进度、停止、响应丢失后查询服务端offset继续。停止或切换case时等待当前chunk结束，再取消旧会话；提交完成阶段不能伪称取消成功。若完成响应丢失而会话已被服务器清理，界面明确结果未确认，必须重新检查目标再决定，不自动重传覆盖。版本冲突保留完整分块；用户选择重新检查或取消时才清理旧会话。无效目录目标在分配会话目录前拒绝。

证据 `tmp/mainline-N2-tree-evidence/`：

- `browser-result.json`：`tools/playwright-tree-write.js` 10项真实API浏览器验收；包括普通二进制/CJK文件字节、create-only竞争、旧hash删除、检查失败、保护路径、分块及完成响应丢失、冲突保留/重试、停止、换case清理、长文件名和迟到元数据。大文件实际测试17825805字节，不宣称1GiB极限负载已验收。
- `upload-conflict.png`、`tree-long-filename.png`、`tree-uploads-complete.png`已目视检查，修正禁用按钮仍显得可点击的问题。桌面1440×1000/Chromium156，原有其他面板不在本轮UI验收范围；未做移动端、屏幕阅读器或物理键盘全流程测试。
- `frontend-build.log`通过，原有大chunk提示保留；`impeccable-detect.json`为空；`editor-write-result.json`11项状态复验通过。
- `write-compatibility/summary.json`4/4、`core-targeted/summary.json`3/3（core_api/stage_advance/casedict_sync）通过。双API/SQLite/PostgreSQL各29项：`tmp/case-write-evidence-kf276m3w/result.json`；新增等待锁的metadata、二进制hash/absence、分块创建基线、越界/归属、目录无孤儿会话、字典别名及Agent门禁。没有把此前offline20/core14/CAE3改写成此次全量重跑。
- `developer-schema-readonly.json`仍13表/旧head`6fb6c18fb7ee`，临时PG残留0；开发API未重载。本轮无schema变更，源码盘点137源/168路由/233边界。

边界：本地File和上传会话引用保存在当前组件内存，尚不提供页面关闭/重载后的持久续传、浏览器崩溃后的会话回收或完成结果的持久幂等查询。大文件分块仍仅fs，S3分块、整文件本地hash预校验及大规模负载未通过本轮验收。检查hash与外部绕锁写之间仍可能竞争，文件发布与DB提交也尚无共同崩溃事务。

追加源码快照 `platform/backups/mainline-N2-tree-20261102.zip`；前置forms/editor/write快照保留。N2-02继续实施，仍需封存输入写保护、Agent memory/复制源和其他直接写入口，不关闭任何整项FF。

## N2-02 Agent 记忆与复制源一致性

`ToolExecutor.execute_case_tool` 是自动工具回合与批准回合的统一入口。`update_memory` 在同一个case控制锁内读取当前记忆、追加/压缩并原子替换，随后由调用方将工具结果和事件提交。追加使用获得锁后的最新内容，仍保留每次16KiB/总量64KiB限制；模型之前读到的旧记忆不会被用来整文覆盖。Plan模式仍禁止记忆写入。该接入不把任意shell变为受控事务，也未解决同一工具重复审批/响应丢失重试的持久幂等。

单算例复制先锁来源并刷新DB镜像，再读取文件清单、大小和SHA-256；逐文件读取复核，复制后再次检查来源清单。caseDict字节与复制出来的revision来自同一次协调读取，不能把等待锁前的镜像配到新文件上。来源锁持有到目标事务提交，协作编辑者随后才继续。重命名与移动也使用该锁，移动保留存储前缀。

整区复制按case ID升序获得全部来源锁，之后才创建目标工作区/算例，避免目的记录先flush再等待来源造成锁顺序问题。该增量最初取请求开始时的成员列表；后续回收站单元已改为先锁工作区再枚举成员，详见下节。任一子项失败，撤销全部目标DB记录并清理已经复制的文件，不删除或还原来源文件。清理在外层rollback释放事务锁之前执行；清理函数仍为尽力清理，磁盘/对象存储故障可能需要后续恢复日志处理。

排队/运行中的Job、非终态JobAttempt（包括legacy Job已终态但attempt还在finalizing）、执行中的Agent shell会阻止复制；当前进程可见的活动终端/可视化也阻止复制。后两者不是跨worker注册表。绕过协调锁的原生/外部写入在哈希复核观察到变化时返回409；不能据此声称外部文件系统具备原子快照，最后检查后的写入与ABA变化仍需尝试目录隔离解决。

证据 `tmp/mainline-N2-memory-copy-evidence/`：

- `case-write-result.json`：SQLite/PostgreSQL各38项通过，两个实际API进程共享临时DB/fs。新增双记忆追加、旧编辑器冲突、压缩/非法输入、复制等待提交并刷新镜像、复制期间阻挡编辑、整区预先锁全部来源、外部改动409、单项/整区失败清理、queued/finalizing拒绝。`evidence-origin.json`保留原始报告位置；临时PG已删除。
- `write-compatibility/summary.json`：7/7，通过原4套及case_admin、memory、plan_mode。后3套新增可注入隔离API地址，两个模型mock使用随机端口；真实普通Agent回合写记忆并在下一请求注入，Plan违规工具不写文件。
- `developer-schema-readonly.json`：开发库仍13表/旧head `6fb6c18fb7ee`，临时PG残留0。代码仍22业务表/head `4c02b1a7d9e0`；无schema变更，开发API未重载。源码盘点仍137源/168路由/233边界。

本轮无前端改动，没有重跑浏览器/build、完整offline/core/CAE；前置证据及其限制继续保留。复制仍按原契约拷贝完整文件树（包含内部文件），不会因此创建对应Job/attempt记录；大目录性能、S3真实对象存储故障、进程崩溃后的补偿和持久发布指针未在本轮验收。源哈希复核增加读取开销，不宣称流式大文件复制交付。

源码追加归档 `platform/backups/mainline-N2-memory-copy-20261102.zip`，前置N2快照保留。N2-02和整项FF继续实施，下一步审查附件/备份恢复等写入口，并推进不可变输入与封存保护。

## N2-02 消息附件、备份读取与恢复包校验

图片先整批校验数量、MIME、Base64和单张4MiB限制（含解码前长度检查），生成完整UUID路径，再在case锁内检查消息/存储配额、写入附件、追加用户消息并提交。后一张图片无效、配额拒绝不会留下前面附件；提交前的存储/消息事件异常会清理本批路径并回滚DB。该锁也覆盖无图片的用户消息提交。配额检查尚不是跨case/用户维度的持久预留机制。

如果DB commit已尝试但返回异常，不能判定消息必然未提交，因此保留可能被已提交消息引用的文件，等待后续提交日志/对账。清理仍是尽力清理，删除失败与进程崩溃可能留下孤儿；本轮不声称文件与DB共同原子性。Agent其他事件写入、重复消息/工具幂等和原生进程仍有独立边界。

`case_source.py` 抽出复制/备份共用的来源清单、逐文件读取和最终复核。备份按case ID获取全部来源锁并刷新镜像，先拒绝已知活动，再读取文件和历史记录；排除单独进入回收站的case和未完成上传目录，锁后复核成员归属。来源外部改动或caseDict阶段/元数据与DB镜像不一致时返回409，不输出混合备份。该增量最初取开始时的成员列表，后续回收站单元已改为先锁工作区再枚举成员；原生绕锁写与其他worker的会话注册表还不是完整分布式快照。

恢复继续只创建新工作区。ZIP只允许manifest.json，压缩包上限100MiB、展开manifest上限160MiB、解码后的全部文件上限100MiB、文件数10000；各历史表清单最多100000条。恢复前检查schema/字段类型/日期/整数、唯一ID/路径、跨case和会话引用、事件序号、分支循环及祖先关系、终态作业/工具、Base64/hash、规范路径、文件与父目录冲突及Windows设备名/大小写/尾点别名。有有效解析镜像的case还必须与caseDict阶段/元数据一致。无效包400，HTTP上传压缩包超限413，配额拒绝429。

新目标创建前检查存储配额。恢复名称保留随机唯一后缀；日志按“来源case ID＋日志路径”映射，避免不同case使用相同相对日志名时串线。重映射后的路径冲突在文件写入前拒绝。写入失败时清理所有本次新case前缀（包括写成功后才抛错的文件）并回滚；commit结果未知时保留文件等待对账。

证据 `tmp/mainline-N2-attachments-backup-evidence/`：

- `case-write-result.json`：双API/SQLite/PostgreSQL各52项，新增14项覆盖附件批量校验、注入配额拒绝、落盘后异常、事件flush后异常、metadata/备份等待附件与消息提交、备份锁全部来源、外部改动、恢复坏包/路径/关联/ZIP尺寸、失败清理和跨case日志映射。`evidence-origin.json`记录原始报告，临时PG已删除。
- `write-compatibility-final/summary.json`9/9，writes加入image_attachment和backup；前者使用随机端口mock验证OpenAI兼容/Anthropic图片块和附件原字节，后者验证真实新ID/日志/分支/工具记录恢复和活动拒绝。原有7套也复验通过。
- `manifest-result.json`20项离线校验，涵盖资源上限、Windows路径、活任务拒绝、坏压缩流、caseDict镜像一致性和2002层分支链。资源阈值单测缩小常量验证分支，不宣称做过100MiB实际压力测试；实际API验证ZIP声明解压超限。
- `developer-schema-readonly.json`仍13表/旧head `6fb6c18fb7ee`，临时PG残留0；无schema变更，代码仍22业务表/head `4c02b1a7d9e0`。开发API未重载。源码盘点139源/168路由/233边界，Python套件入口179个。

备份仍为v1，包含case文件和旧Job/会话/事件/工具历史，不含新增WorkflowRun/Step、JobAttempt/claim、持久outbox和publication表，也不恢复密钥或活动执行。文件哈希是完整性校验，不是签名认证。完整DB/文件联合灾备、工作流历史迁移、S3真实故障、跨worker会话一致性和崩溃补偿仍在N5/N7及FF-J12范围，不能用此增量关闭J12。

本轮无前端改动，未重复浏览器/build、完整offline/core/CAE；新增manifest单测加入offline（目前21套），本轮只跑其中该套和上述双API专项。源码追加归档 `platform/backups/mainline-N2-attachments-backup-20261102.zip`，所有前置快照保留。N2-02继续实施，下一步是回收站删除/恢复与专业写入口，再推进封存输入和不可变尝试目录。

## N2-02 回收站、工作区成员与目录移动协调

工作区成员变更先按ID升序获取工作区DB行锁，再按case ID获取算例控制锁，最后才进入回收站进程间文件锁。创建/ZIP导入、重命名、移动、单项/整区复制、整区备份与删除/恢复遵循该顺序；协调上下文结束后仍由调用方commit释放数据库锁。移动只更新workspace_id，文件前缀仍按user ID/case ID保存。

整区删除、复制、备份在工作区锁内重新读取成员列表；先开始且已提交的新建会被包含，删除先获得锁时，排队的新建/导入/移入/复制会返回workspace_unavailable，不留下新成员或目录。普通写入/复制与Job提交、重试、领取在算例控制锁后重新检查case和workspace的回收站状态，排队前授权不能用来重新创建已移动的目录。正常请求删除后仍404；已经通过授权、等待锁的请求返回结构化409（case_unavailable/workspace_unavailable/workspace_conflict）。

算例删除和移动要求工作区直接所有者，shared member仍可按既有权限写文件；不能再把owner的算例移到member的回收站，形成双方均无法恢复的记录。恢复使用同一锁顺序并核验原文件前缀和工作区成员。已知排队/运行Job、未终态attempt以及Agent/Goal/终端/viz活动继续阻止目录移动；取消标记不代表执行器已收尾。

`_trash/index.lock`为稳定锁文件：Windows使用msvcrt字节锁，Linux使用flock；清单仍原子替换，进程死亡会释放OS锁，不靠删除锁文件解锁。不同API进程删除不同工作区的case也不会互相覆盖清单。读者读取完整旧/新清单；它不是对任意读取API提供目录与索引的整体快照。文件操作放在线程中，请求取消会等待线程结束再抛出，避免后台移动继续执行而数据库锁提前释放。

目录移动禁止覆盖目标。Windows使用非覆盖rename，遇到短时文件占用最多重试1秒，持续占用/权限不足409；Linux使用renameat2(RENAME_NOREPLACE)，运行时不支持则拒绝。恢复遇到目标已存在返回409并保留该目录；整区中途失败会逆序移回之前成功的目录，索引发布前异常也会尝试还原源目录。创建/导入/复制在提交前失败清理本次新前缀；commit结果未知时保留可能已提交的数据。

证据 `tmp/mainline-N2-trash-evidence/`：

- `case-write-result.json`：两个实际API共享临时fs/DB，SQLite和PostgreSQL各60项。新增8项涵盖删除与保存/复制/Job竞争、两个API清单更新、保存先提交、整区成员变化、恢复遇阻回滚、索引失败恢复、shared member权限；前52项复验通过，临时PG已删除。
- `write-compatibility-final/summary.json`11/11；新增隔离case_import/trash。导入脚本自建真实cavity网格后打包并导入求解，不再依赖开发库case 1；回收站生命周期等待cancelled attempt和ended_at后再删除。首轮失败9/11及补验1/2保留在原目录，最终11/11为独立完整重跑。
- `execution-compatibility/summary.json`3/3；`job-claim-result.json`SQLite/PostgreSQL各13项，双worker领取/重试/收尾复验。首次claim测试曾缺少child barrier而超时，最终独立重跑通过；新增子进程日志保留，没有据此宣称已确定首轮超时根因。
- `trash-control-windows.json`7项、`trash-control-linux.json`6项，通过真实Windows/WSL Linux系统调用验证预检查后新目标出现仍不覆盖、目录往返、持锁进程直接退出后再获取锁、重复请求取消等待线程、畸形成员拒绝；Windows另测短时占用重试和持续占用409。配置使用临时fs桩，无真实DB。Linux该组不是完整Linux API部署验收。
- 开发库只读仍13表/旧head `6fb6c18fb7ee`，临时PG残留0。没有schema变更或开发API重载；源码盘点140源/168路由/232边界，Python入口180，offline现22套、writes11套。

仍未提供永久清除、S3回收站、分布式文件系统锁保证、移动与索引的崩溃日志或FS/DB共同事务。进程在目录移动后、清单替换前被杀仍可能需要恢复；进程死亡释放锁只表示可继续获取锁，不表示业务状态自动恢复。多worker终端/viz/Agent注册、任意shell原生写、完整进程树取消仍有独立缺口。已有本地JSON清单是受信内部元数据，不是可导入的用户备份格式。

本轮无前端改动，未重复浏览器/build、完整offline/core/CAE；真实导入求解不能替代三条CAE基准。源码追加归档 `platform/backups/mainline-N2-trash-20261102.zip`，之前快照保留。N2-02和FF-A23保持实施中，下一步继续专业生成/工程写入口和输入封存，再推进N2-03不可变尝试目录。

## N2-02 首批专业写入口与 CAD WebSocket

几何repair/msh-preview、mesh:evaluate、UDF scaffold/build/integrate、MPI准备、postprocess提交及report:compose现在先获取case控制锁，再读取输入/检查覆盖条件、写入本次文件或创建Job，最后commit。几何preview/export的Job入队也在锁内；Job启动在commit后执行，避免持有case锁再等待独立claim事务。已经入队的Job继续阻止回收站移动。这里协调的是API准备与提交，原生worker运行期间仍未持锁或使用全套不可变输入。

| 入口 | 本次实现与限制 |
|---|---|
| geometry/repair、msh-preview | 源读取、目标冲突检查、转换和STEP/validation或VTU发布在同一case锁内。后台转换收到请求取消时先等待线程完成再释放锁；并不强行终止CAD计算，也不保证多文件崩溃原子性。大模型会延长持锁时间。 |
| mesh:evaluate | 等待caseDict提交后读取最新质量政策，日志指标和log_sha256来自同一份字节；run输入清单仍不是外部原生进程的原子快照。 |
| UDF/scaffold | 9个生成路径全部要求不存在；仅有Make/options等残留时也拒绝，避免覆盖用户文件。存储中途故障仍可能留下部分新文件，需后续提交日志/恢复处理。 |
| UDF:integrate | 锁内读取最新controlDict，使用定点插入保留已有functionObject、注释、include与verbatim代码，不再追加第二个functions块。重复名字/直接同类型409，重复块/不配对大括号/间接functions块400。不会解析include文件来推导间接定义，原生OpenFOAM仍验证实际配置。shell路径加引号。 |
| MPI:run | 生成缺失decomposeParDict和Job入队在锁内；Linux暂存名加入随机UUID，避免不同数据库重用Job ID时读到旧暂存结果。MPI复制回传、输入冻结和rank取消仍待后续。 |
| postprocess:run | caseDict配置、工程hash和dict_revision在同一次锁内读取，再创建配置快照及Job。专用worker的发布指针未在本次改造。 |
| report:compose | 门禁、证据读取、旧稿备份和新稿写入在锁内；新增可选expected_revision和输出expected_sha256（省略无条件，null要求不存在）；冲突409。备份保留原始字节，生成失败不提前创建备份；报告构建/发布另行处理。 |
| /ws/cadscene | 每条replace/clear用新DB会话重新检查用户active和case权限，再协调写scene.json；连接时授权不再永久有效。可传expected_revision/expected_sha256，成功广播增加content_sha256/dict_revision；冲突返回error/code=file_conflict等。删除或权限失效返回case_unavailable并关闭4403。广播仍仅当前API进程，尚无持久序号、重连快照、跨实例扇出或客户端乱序丢弃。 |

同步线程操作共用`async_ops.finish_before_cancel`，回收站也复用同一等待逻辑。CAD使用独立IO线程池并在文件写入结束后提交，再广播成功；异常不会提前释放数据库锁。它不提供进程树取消，重复取消等待底层线程的行为已在Windows/Linux复验。

新并发检查暴露文本GET在锁前检查exists、导致首个多文件发布期间返回404；现在文本存在性检查在锁内。file:version的协调冲突不再误归类为422，恢复结构化409。二进制/raw/树和外部进程读取仍不是整体快照。

证据 `tmp/mainline-N2-professional-evidence/`：

- `case-write-result.json`：SQLite/PostgreSQL各69项（新增9项、前60项复验），两个真实API共享临时DB/fs。覆盖UDF九文件与复制/编辑竞争、部分目录保护、mesh政策刷新、真实STEP与validation共同提交、真实Gmsh→VTU、报告旧hash、11个专业HTTP写入口及metadata在删除排队后的复查，以及CAD与HTTP冲突/删除/用户禁用。临时PG已删除；首轮失败位置在evidence-origin.json。
- `write-compatibility-final/summary.json`15/15，原11套及geometry_repair、udf_workflow、cadscene、mpi全部重跑。后四套改用隔离API和自建数据，不读写开发case 1。UDF实际wmake、CDLL与icoFoam/P0_UDF_OK通过；MPI使用两份独立生成网格/初场，确认并行目标事先没有0.1/U后运行2 ranks和重构，U场相对差异约1.14e-08（阈值1e-5），不是与同一目录旧串行产物比较。
- `core-targeted/summary.json`3/3：mesh_quality、postprocess、report_compose实际流程通过；`control-dict-result.json`10项保留原文和拒绝重复/无效块。`async-control-windows.json`7项、`async-control-linux.json`6项复验取消/锁/目录移动；Linux这组仅底层操作，不是完整API部署验收。
- 开发库只读仍13表/旧head `6fb6c18fb7ee`，临时PG残留0；无schema变更或开发API重载。源码142源/168路由/232边界，Python入口181，offline23套、writes15套。未重跑完整offline/core/CAE、execution或前端；真实UDF/MPI不能替代全部专业数值验证。

源码归档 `platform/backups/mainline-N2-professional-20261102.zip`。N2-02和全部FF继续实施。下一批处理report/latex/ppt/pdf与其他专业Job提交、批次manifest、viz首写，随后输入封存与不可变尝试目录。当前仍存在FS/DB提交失败或崩溃留下文件、原生worker绕锁写/发布与多worker活动注册缺口，不据此宣称全部写入口、幂等或恢复完成。

## N2-02 报告构建、专业作业与批次提交协调

报告/LaTeX/PPT/PDF、CalculiX、SU2、ParaView和Flotherm的提交入口先取得case锁，重新确认算例可用，再检查输入、准备文件并创建Job。commit之后才调用start_job，避免与独立claim事务互相等待。并发请求若已通过授权但等待期间算例被删除，返回case_unavailable/409，不能重新建目录或入队。

| 入口 | 本次行为 |
|---|---|
| report:build / latex:build | 快照准备在锁内、Job提交之前执行；准备失败回滚Job并清理本次新目录。已有job目录409保留，防止回滚后ID复用清掉历史证据。准备使用后台线程，重复请求取消也先等待线程结束并清理，再释放锁。取消HTTP任务的测试500来自测试应用主动取消，不作为业务成功响应。 |
| 报告/LaTeX完成回调 | 独立DB事务重新取case锁，复核证据/源文件并发布和清理。最新Job选择仍为本API进程内字典；这次没有实现跨worker持久发布代次。外部原生写/原始文件读取仍可绕过协调。 |
| 构建启动失败 | 不再盲目把Job改成failed或重新写日志。只有持久终态且无活动attempt才清理空闲快照；已删除算例留待恢复。claim/状态冲突409；已取消后删除的请求不能复建原目录或把cancelled覆盖为failed。 |
| jobs:batch / job-batches | Job、manifest和index在同一case锁内准备，之后commit；并发批次保留双方索引。读取接口持锁等提交；取消先锁内读取再释放事务，避免调用独立状态事务时死锁。索引损坏保留原文件并拒绝入队；提交前写盘/flush失败恢复原index并移除本批manifest。commit结果未知保留文件，仍需N2-06对账。 |
| jobs/{id}:cancel | 补上本地queued且无本进程句柄的取消：锁内重新检查Job仍queued、最近attempt未claim，写终态和事件。已经claimed则返回ok=false与状态变化提示，不能把已领取项当作未启动项取消。这不是跨worker完整进程取消。 |
| flotherm:build-library | 先检查配额和全部待复制文件（最多20个、每个1MiB），拒绝源/目标相互嵌套和源符号链接。新目录复制/flush失败时清理本次创建的目标；原地构建保持既有语义。提交失败结果未知及原生编译输出仍不提供崩溃原子性。 |

证据 `tmp/mainline-N2-admission-evidence/`：

- `case-write-result.json`：SQLite/PostgreSQL各81项，前69项复验+12项新增；两个真实API共享临时DB/fs。涵盖批次并发/索引写后故障、报告准备与编辑竞争、重复请求取消、已有快照保护、取消后删除再claim、已claimed不可误取消、辅助库嵌套/超限以及10个提交入口等待删除后的复查。临时PG已删除。
- `solver-compatibility-consolidated.json`：7套通过，合并首轮6/7和PPT修正后的1/1，保留原始失败。含PPT、PDF页码、CalculiX、SU2、pvpython/pvbatch、Flotherm转换及辅助库。本轮辅助库在临时算例内生成并CDLL调用，不覆盖全局用户库。
- `report-compatibility-consolidated.json`：3套通过，合并生命周期/多配方与LaTeX自建夹具补验；原latex_smoke依赖cavity-demo导致失败，现独立创建算例。PPT测试显式绕过本机HTTP代理；这不是产品代理设置变更。
- `execution-final/summary.json`3/3，`offline-targeted/summary.json`4/4（claim、finalization、durable outbox API、report snapshot）。没有重跑全部writes22/offline23/core/CAE或前端；本次无UI改动。
- 开发库只读仍13表/旧head `6fb6c18fb7ee`，临时PG残留0，无schema变更或开发API重载。源码142源/168路由/234边界，Python入口181，writes可隔离运行22套。失败来源与修复说明见`evidence-origin.json`。

源码归档 `platform/backups/mainline-N2-admission-20261102.zip`。下一单元处理runs:derive、viz/terminal启动和封存输入；PPT/PDF/专业worker仍直接读写活动算例，所有者恢复、跨进程活动登记、发布代次、FS/DB共同事务和崩溃日志仍待续。本次不关闭N2-02或任何整项FF。

## N2-02 工况派生与 PTY/viz 会话启动

`runs:derive`、终端启动/重连/输入和可视化创建/列表/显式关闭进入case控制锁。已经通过授权的请求等待期间若算例被删除，HTTP返回409，终端返回error/terminal_unavailable并关闭4403；不会重新创建旧case目录。终端仍保持owner-only访问，明确不带case的全局终端保留兼容语义；指定不存在或其他用户case不再退到服务端cwd。active用户状态在重连及每次输入/resize重新查询，失效输入被拒绝，已连接会话关闭自身父进程。

| 入口 | 当前实现 |
|---|---|
| runs:derive | fs内先枚举选定输入、计算长度/哈希并核验存储配额；在`.vibeflow/derive/{uuid}`流式复制，再比较全部来源清单。初始`0/`保留；普通/指数/负数结果时刻、processorN、日志和foam跳过；processorSettings等普通名字保留。目标与源不能相互嵌套，拒绝保留文件名/尾点和源符号链接。 |
| 派生发布/清理 | `fs_directory.move_new`以Windows rename或Linux renameat2(NOREPLACE)发布完整目录；即使检查后外部创建空目录也不覆盖。准备取消先等线程结束，再清理本次暂存。提交前flush失败清理本次新目标；commit结果未知保留完整派生，尚无崩溃对账日志。清理/移动校验解析路径在指定root内，不能删root或外部路径。 |
| PTY启动 | 持case锁等待线程完成native spawn并注册，提交后发送ready。取消/失败保留本次取得的句柄直到父进程结束。输入维度1–1000、shell限cmd/wsl/wsl-sandbox。普通cmd/WSL仍能访问cwd之外，不是硬沙箱。 |
| PTY连接/关闭 | WS任一转发任务结束都会取消另一个任务并unsubscribe，避免断线后订阅永远残留。断线保留PTY供重连；重连核验owner、active用户及case一致。显式关闭等待terminate(force=True)和实际isalive检查；终止失败保留活动记录。读取中断但进程仍活着不会误报为已退出。 |
| viz启动/读取 | 输入读取使用等待取消完成的线程，HTTP就绪探测异步执行并绕过本机代理；Popen后立即登记，事务提交前列表等待case锁。启动失败/取消等待父进程退出并关闭日志再注销；同API启动中的端口也参与端口排除。显式关闭持case锁并等待进程退出。 |

证据 `tmp/mainline-N2-session-evidence/`：

- `case-write-result.json`：SQLite/PostgreSQL各93项，前81项复验+12项新增；双真实API共享临时DB/fs。覆盖派生与编辑/重复提交/metadata竞争、外部源变化/目标冲突、写盘故障与重复取消；终端无权限/缺失case拒绝、三入口等待删除、真实PTY工作目录/断开订阅/重连/禁用用户/启动取消；真实STL kernel、异步健康探测、启动中的列表/同API删除等待及取消清理。临时PG已删除。
- `session-compatibility-final/summary.json`4/4：终端多会话、32个PTY占用下的实际LaTeX编译、独立生成blockMesh后OpenFOAM/trame/proxy/并发配额/端口关闭、viz空闲回收。原viz测试的`or True`无效断言已移除；不再依赖开发cavity-demo。早期未适配脚本在localhost:8000登录连接被拒、未执行case变更，失败保留；最终四套全部在临时API完成。
- `directory-windows.json`5项、`directory-linux.json`4项：真实OS无覆盖发布（含过期exists判断后的空/非空目标）、目录边界清理、Windows短时占用重试。Linux仅stdlib底层操作，不是完整API或PTY部署验收。
- 开发库只读仍13表/旧head `6fb6c18fb7ee`、临时PG残留0，无schema变更或开发API重载。源码144源/168路由/232边界，Python入口182，offline24/writes26；本轮只跑相关专项，没有重复全部offline/writes/core/CAE或前端。

局限必须保留：PTY/viz活动登记、端口分配、配额和监督器仍为本API进程内；本轮证明启动取消与等待删除，不证明另一个API能识别已经完成启动的所有会话。原生计算/终端持续写入、完整进程树取消、S3回写与会话恢复也未统一。文件manifest检测可观察到的变化，不阻止外部编辑在最终检查后写入或ABA。N2-02仍未关闭，下一单元推进封存输入的拒绝/显式改版规则与N2-03不可变尝试目录，再与N2-05/06发布/恢复和N4/N5运行所有权衔接。

源码归档 `platform/backups/mainline-N2-session-20261102.zip`。

## 显式输入封存与独立尝试增量

显式input_paths接通Job API/内部工作流队列/实际执行器/受限shell重试，普通storage、Agent文本及上传删除拒绝覆盖封存命名空间。SQLite/PostgreSQL各23项、原双API协调各93项、writes26/26和offline定向5/5通过。N2-03转实施中，旧默认入口、直接写盘专业适配器、版本发布及恢复仍待续；详见 [attempt-workspaces.md](attempt-workspaces.md)。

## 剩余工作与边界

N2-01 的同 revision 并发验收已获得证据。N2-02 的普通文本/整文 caseDict、块/错误表单、LaTeX 插入及文件树普通/分块上传与删除已接入并有验收；封存命名空间的普通写拒绝已接通，仍需其余直接写盘入口/工作副本显式改版、原生发布与跨API活动登记等剩余边界（派生与会话启动增量见上文）。Agent memory、复制源、消息附件、备份读取、回收站/工作区成员及首批专业API/CAD WS协调已取得增量证据，备份v1的完整工作流恢复仍待续；任意shell/外部编辑和专业产物生成仍不能视为统一。

N2-03 负责不可变输入和独立尝试目录；N2-04 负责依赖失效；N2-05 负责持久发布代次和活动版本指针；N2-06 负责提交日志及崩溃恢复。当前文件替换成功、数据库提交失败仍可能留下不一致，数据库 rollback 不会还原已写文件，不能宣称跨文件系统和数据库原子性。外部编辑器不持有控制锁，哈希只能检测检查时已发生的变化，不能阻止检查后直接绕过 API 的写入。

本轮无 schema 变更，仍为 22 个业务表、Alembic head `4c02b1a7d9e0`。只读核对开发库仍为 13 表（12 业务表加版本表）、旧版本 `6fb6c18fb7ee`，临时测试 PostgreSQL 库残留为 0，见 `tmp/mainline-N2-write-evidence/developer-schema-readonly.json`。开发库与原开发 API 不因临时库验证自动升级。回退使用独立源码快照 `platform/backups/mainline-N2-write-20261102.zip` 恢复到新目录，保留用户数据及失败证据；校验值见同名 `.sha256` 文件。
