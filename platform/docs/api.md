# API 面清单（已实现）

在线接口契约由 `/openapi.json` 提供，`/docs` 用于交互查看。报告派生产物和 LaTeX 原件的 GET/HEAD 分别生成唯一操作标识，GET 409 中的 `detail.code` 和 `detail.message` 可用于识别对象版本及读取诊断。 [OpenAPI 验收](FF-J26-openapi.md)

新建Windows终端可带 `cli_harness=kimi|opencode|claude|codex&provider_profile_id=<id>`（harness选择其中一个），使用本人API配置启动相应CLI；已有会话不接受更换绑定。Claude Code接受Anthropic Messages配置，官方DeepSeek配置自动使用其Anthropic兼容地址；其他OpenAI协议配置返回400。Codex要求服务支持Responses API，拒绝Messages配置。终端列表/详情增加无密钥的 `credential` 元数据。见[Kimi API配置切换](kimi-provider-profiles.md)、[OpenCode 2配置与已知问题](opencode-provider-profiles.md)、[Claude Code配置](claude-provider-profiles.md)和[Codex终端配置](codex-terminal-provider-profiles.md)。

专业入口经 `start_job` 抛出并交由全局处理器返回的 `ExecutionConflict`，现在在已有 `detail.code/message` 中附上该次 `job_id`，仍为 409；无 Job 的冲突省略 ID。已验证 CalculiX、SU2、ParaView、PPT、PDF、FloTHERM 转换/library、UDF build/integrate 的错误登记响应。客户端据此读取失败详情，不自动重提；不改变独立改写响应的路由或其他异常。见[专业进程故障](professional-process-faults.md)。

新增会话session-requests创建/查询与单请求查询/取消；旧sessions入口兼容。协议、状态与幂等边界见[原生启动回执](native-cli-startup.md)。

原生turns/turns:stream接收model_options，models读取支持cursor/limit和服务档位能力；契约见[原生模型参数](native-cli-model-options.md)。

原生回合读取/列表/幂等回执增加usage，持久事件增加usage.updated；计数、实际尝试归属与未知状态契约见[原生用量快照](native-cli-usage.md)。

原生 turns / turns:stream 支持 images:[{name,mime,data}]、最多3张/4MiB每张与纯图片消息；文件引用、幂等摘要和私有输入副本契约见[原生图片API](native-cli-images.md)。

新增原生CLI分叉 POST codex/conversations/{cid}/fork、GET forks查询、POST forks/{id}:cancel，activity发现原生分支；持久幂等和未知结果禁止自动重放见[原生分叉API](native-cli-forks.md)。

原生聊天新增会话级 GET turns/activity 和 PATCH settings；历史分页、client_message_id查询与活动归属只读，设置更新受空闲/权限/未收尾检查保护。新线程文字交接及恢复历史连续性见[统一聊天API](native-chat-ui.md)。

CLI `POST /api/v1/codex/sessions`新增 `runtime=windows|wsl`（默认Windows）；发行版/程序/私有目录只由服务器配置。新增 `GET /sessions/{id}/sandbox`，分别标明legacy就绪与MXC未探测/不适用；配置变化保留409冲突，未知协议收尾后报错。见[原生WSL API契约](owned-cli-wsl.md)。

新增网站身份入口：`/api/v1/auth/chatgpt/{status,start,cancel,callback,connections}`、关联DELETE与`POST /api/v1/auth/logout`。普通API与手动WS/可视化入口支持第一方Cookie；Cookie写请求/WS校验Origin。旧JWT继续兼容，网站身份不作为推理凭据。正式client/公网回调待联调，详见[网站登录API和部署说明](website-sign-in.md)。

`POST /api/v1/codex/sessions` 新增 `provider_profile_id`、`provider_wire_api: "responses"`；显式绑定API配置，不修改Conversation选择，但后续会话配置变化使旧Session失效。未绑定会话不能推理；Session与account响应增加credential状态。没有新数据库表和新路由。详情见[托管API凭据](hosted-cli-credentials.md)。网站Sign in with ChatGPT尚待下一单元，不把CLI内置登录作为商业产品登录。

Codex新增正式 `/api/v1/codex/sessions` POST/GET、`/{id}` GET/DELETE、`/{id}/account`和`/{id}/models` GET、`/{id}/thread` POST。宿主会话持久归属、账户私有目录与停止核验见 [owned-cli-sessions.md](owned-cli-sessions.md)。新增 `/sessions/{id}/turns` 和 `turns:stream` POST、`/turns/{id}`及`/events` GET、`:cancel` POST、`/approvals` GET及`/approvals/{aid}:respond` POST，详情见 [owned-cli-turns.md](owned-cli-turns.md)。已支持持久SSE、复杂审批/计划问答与显式resume；账户登录/登出、WSL、sandbox设置和完整UI继续g2b3+。

Agent命令审批/拒绝使用原子pending领取，重复或竞争请求409；命令运行在所属NativeRequest中，原生期间不持有算例写锁。新增GET /api/v1/conversations/{cid}/tool-calls/{tid}/execution，返回所属执行/停止证据及tool_call_status/tool_result，不触发执行。消息停止、算例取消和真正断线等待原生退出；含未核验请求或待处理副本的会话不能删除，明确删除只随已核验完成的历史移除执行绑定。详见 [owned-agent-shell.md](owned-agent-shell.md)。

viz新增GET /api/v1/viz:sessions/{sid}和列表include_closed=true；保留整数ID，增加phase/ready/process/stop/cleanup_verified/owner_available/finalizing/exit_code。启动等待锁外执行，首帧及实际出生核验后才可代理；DELETE等待实际范围与S3收尾，失败502，已核验正常关闭可重复调用。未知owner返回409，不依历史PID控制或重建。创建者可停止自己的资源，暂存发布仍须复核算例权限/来源。详见 [owned-viz-sessions.md](owned-viz-sessions.md)。

PTY会话增加持久GET /api/v1/terminal/sessions/{id}及列表include_closed=true；正常历史只读回放，未知owner/过期活跃不重建进程，WS返回4409和恢复状态。列表增加phase/cleanup_verified/owner_available/finalizing/pending_input_bytes；退出码未知保留NULL，error帧被终端界面显示。输入队列2MiB，超限只返回terminal_input_limit；关闭等待实际范围退出及S3收尾，失败502，已核验关闭可重复DELETE。详见 [owned-pty-sessions.md](owned-pty-sessions.md)。

GET /api/v1/runtime/capabilities保持现有能力字段，Python/WSL各新增request_id、reachable/error等所属结果；Setup解释器实际导入固定依赖，配置消失不回退API环境。匿名不分配请求，断线等待本次进程退出，返回前用户禁用则401。请求不绑定工程、不分配Job；运行失败返回该部分降级诊断。详见 [owned-runtime-probes.md](owned-runtime-probes.md)。

synctex:query现支持只读共享viewer，先选择并冻结注册/历史或旧版PDF和sidecar，锁外执行所属WSL查询，锁内复核权限/来源。旧版来源改动返回409，缺映射422，超时504，执行/回执失败502；查询期间撤权返回404。返回字段与历史source_current_matches语义保持，详见 [owned-synctex-requests.md](owned-synctex-requests.md)。

正式report:build/latex:build和工程报告准备先幂等查询，再进行锁外所属工具调用，提交锁内再次查询幂等键、检查权限及来源；来源失效返回409，HTTP断线取消尚未提交的准备并等待实际退出，不提前创建Job。已提交请求仍能在当前源文件缺失时重放，详见 [document-admission.md](document-admission.md)。

Job详情/列表/轮询与SSE使用持久逻辑exit_code和ended_at；成功发布兼容文件尚在收尾时显示completion_pending=true与finalizing，重复构建请求采用同一判断。done新增ended_at和cleanup_verified，远端流等待持久记录并释放请求数据库连接；lease/回执不可核验返回recovery_required而非done。取消请求时间与实际停止时间仍需区分，详见 [job-terminal-projection.md](job-terminal-projection.md)。

作业详情的attempt.child_processes现包含pandoc_parse/pandoc_compile/pandoc_version，绑定host出生身份、父attempt和停止回执；LaTeX仍绑定WSL身份。正式报告及API复编译的取消/断线均由所属执行器停止并等待；提交前解析与SyncTeX请求监督待续，见 [owned-pandoc-children.md](owned-pandoc-children.md)。

作业详情新增attempt.child_processes，返回严格绑定该attempt的原生子执行台账；损坏记录为null，旧空值不代表所有入口均已监督。能力凭据不公开，权限沿用算例检查。注册LaTeX取消会等待所属编译范围退出并记录结果；旧启动interrupted状态不能代替停止回执，见 [owned-native-children.md](owned-native-children.md)。

取消接口现以持久Job状态作决定，提交取消后再通知本机/Redis；另一API无本地执行对象、根进程exit 0但仍finalizing时也可提交。提交失败不先停止本机进程；已成功发布后的迟到取消不撤销结果。ok=true仍表示请求接受，实际子执行退出与完整竞态待N4-03/04，见 [publication-preflight.md](publication-preflight.md)。

MPI `POST /cases/{id}/mpi:run` 现以配置WSL发行版的nobody账户进入受监督私有namespace，登记时校验非零原生UID及发行版；登记冲突返回409及code/message/job_id，启动错误返回409且保留已失败Job。返回成功结构不变，无新迁移或公共预算参数。真实ranks停止证据与剩余入口见 [native-entry-supervision.md](native-entry-supervision.md)。

新停止记录可带`attempt.process_stop.scope_observation`，采用`vibeflow.windows-members.v1`记录累计成员、已观察成员、句柄退出/打开前已消失、待等待数及核验结论；数量不一致或观察失败时不能verified。旧v1停止记录仍可读取，没有该字段的历史不能视为通过本次累计成员核验。无新增迁移，详见 [process-tree-stop.md](process-tree-stop.md)。

Job详情新增`attempt.process_stop`：未提交/无效记录为null，已验证记录采用`vibeflow.process-stop.v1`并绑定本次启动nonce；包括停止原因、宽限、升级、范围退出与控制报告。清理失败不能登记成功，取消接口接受请求不等于退出完成。详见 [process-tree-stop.md](process-tree-stop.md)。

Job详情新增attempt.process_identity，保存经归属校验的vibeflow.process-identity.v1；旧/损坏/未登记记录为null。GET /jobs/{id}/process提供只读OS观察，身份和启动顺序见 [process-identities.md](process-identities.md)。

全部report:build和report:compose现已统一注册，不再以postprocess head是否存在分支；两者均支持Idempotency-Key、持久任务及历史。普通文档/旧证据门禁、原字节备份路径与manifest schema兼容见 [ordinary-document-workflows.md](ordinary-document-workflows.md)。下文历史增量中的“仅已有head”限制已取消。

全部POST /cases/{id}/latex:build现在使用latex.build@1；保留main/recipe，新增resources和Idempotency-Key。GET latex/results、latex/publications、latex/artifact、latex/source提供按主文件当前/历史/下载；synctex:query可指定publication_id并返回source_current_matches。详见 [latex-workflows.md](latex-workflows.md)。下文早期“LaTeX待注册”记录已由本增量替代。

独立报告草稿注册入口：已建立postprocess head的POST /cases/{id}/report:compose支持Idempotency-Key和?wait=false；默认同步超时返回504 composition_pending并保留Job。GET report/compose-result、report/compositions、report/compose-artifact提供当前/历史/下载；两种输出独立版本，current/stale/applied分开返回。详见 [report-compositions.md](report-compositions.md)。下文更早“独立compose尚未注册”已由此增量替代；LaTeX也已统一注册，见latex-workflows.md。

注册HTML/DOCX报告：工作流target=report，parameters.report_mode=compose（默认）或markdown；E确认后派发。已建立postprocess head的report:build使用注册处理器并支持Idempotency-Key。新增GET /cases/{id}/report/results?publication_id=...、report/publications、report/artifact?path=...&publication_id=...，详见 [report-workflows.md](report-workflows.md)。独立compose/LaTeX已由后续增量统一注册。

后处理：已有postprocess:run在case建立solve head后走注册处理器并支持Idempotency-Key；results新增publication_id选择和工具链/current/stale，新增GET /cases/{id}/postprocess/publications（最近50项）。工作流target=postprocess等待D确认后执行并绑定E确认，现有报告准入/收尾复核当前后处理head。详见 [postprocess-workflows.md](postprocess-workflows.md)。

求解新增 `GET /cases/{id}/solve/results?publication_id=...` 和 `GET /cases/{id}/solve/publications`；当前/历史证据、工具链及current/stale，坏文件409、越权/非求解版本404。工作流target=solve已注册，确认C后派发并等待D确认；详见 [solve-workflows.md](solve-workflows.md)。

网格版本读取新增 `GET /cases/{id}/mesh/results?publication_id=...`（省略ID读取当前）与 `GET /cases/{id}/mesh/publications`（最近50项）。返回quality/boundaries/toolchain及current/stale，坏发布409、越权/非网格版本404；原mesh:evaluate兼容保留。详情见 [mesh-workflows.md](mesh-workflows.md)。

前缀 `/api/v1`（除 WS 与 viz proxy）。认证：`Authorization: Bearer <jwt>`；注册/登录、provider 注册表和健康检查除外。终端与 viz 的浏览器通道使用 `?token=<jwt>`，且会话必须属于当前用户。

## 在线 Schema 与错误约定

- `GET /docs`：FastAPI 默认 Swagger UI。
- `GET /openapi.json`：由应用实际路由生成的 OpenAPI；比手工清单更适合核对请求/响应模型。
- WebSocket、SSE 帧负载和 proxy 通道不完整地表达在 OpenAPI 中，仍以本文为准。
- 这些默认文档端点当前未单独加鉴权；生产暴露前应纳入网关策略。

常见错误：400 参数/路径错误｜401 未认证或 token 无效｜403 已认证但禁止｜404 资源不存在或不可见｜409 状态/并发冲突｜422 字典或模型校验失败｜429 配额超限。

## auth
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /auth/register | 注册（邮箱+密码，自动建 Default 工作区） |
| POST | /auth/login | 登录 → token |
| GET | /auth/me | 当前用户 |

## workspaces & cases
| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | /workspaces | 列表 / 新建 |
| PATCH | /workspaces/{id} | 工作区重命名 |
| POST | /workspaces/{id}:copy | 工作区深拷贝（include_cases默认true）；按ID锁全部来源，任一子项失败撤销全部新目标；来源变化/活动作业409 |
| GET | /workspaces/{id}/backup | v1 ZIP 备份包含材料库版本历史；协调来源及 SHA-256 校验，排除回收站 case 和上传会话；活动或来源变化 409；不含密钥及工作流运行状态表。 |
| POST | /workspaces:restore | v1恢复到新workspace；解压/路径/记录引用/镜像/hash校验，新ID及每case日志映射；压缩包/原文件各100MiB、manifest160MiB，坏包400/上传超限413/配额429；详细边界见case-write-coordination.md |
| GET | /workspaces/{id}/cases | 算例列表 |
| POST | /workspaces/{id}/cases | 建算例（写 StandardCase 脚手架入存储） |
| POST | /workspaces/{id}/cases:import | multipart 导入 StandardCase ZIP；单根目录解包、路径/链接/大小/配额校验，失败不覆盖已有 case |
| GET | /workspaces/{id}/material-library | 材料目录与字段选项，支持 `after`、`limit`、`include_archived`。 |
| GET | /workspaces/{id}/material-library/{key} | 读取当前版本，或用 `revision` 读取固定版本和内容摘要。 |
| PUT | /workspaces/{id}/material-library/{key} | 保存 `{expected_revision, material}`，从 0 新建。按现有版本创建新修订，冲突返回 409。 |
| POST | /workspaces/{id}/material-library/{key}:archive | 用 `expected_revision` 追加归档版本，可用 PUT 恢复。 |
| GET | /cases/{id} | 算例详情，含 `workflow_state` 镜像及获准访问后用于复制逻辑路径的 `storage_prefix` |
| GET | /cases/{id}/provenance | caseDict 修订的 intent/假设/决策哈希链、chain_valid 和最新优先分页 |
| GET | /cases/{id}/events | case-events.jsonl 只读时间线，最新优先，offset/limit/has_more；跨用户 404 |
| PATCH | /cases/{id} | 算例重命名 |
| POST | /cases/{id}:move | 算例跨工作区移动（不改文件前缀） |
| POST | /cases/{id}:copy | 算例深拷贝（文件、caseDict、revision独立）；来源锁与hash复核，来源变化或活动作业409；协议见case-write-coordination.md |
| DELETE | /cases/{id}、/workspaces/{id} | 仅owner移入fs回收站；工作区/case/清单锁协调，保留DB/revision/历史；活动/未收尾attempt/目录占用409，普通API随后404，排队写入重新核验后409 |
| POST | /cases/{id}:restore、/workspaces/{id}:restore | owner恢复原路径；校验成员/前缀，目标已存在409且整区已移动项回滚，跨用户404；无崩溃日志/S3恢复 |
| GET | /trash | 当前用户回收站 case/workspace 名称、id 和 deleted_at；永久清除与 S3 回收站未实现 |

## workflows（几何、网格、串行求解与后处理）

完整请求与范围见 [registered-workflows.md](registered-workflows.md)。创建请求含target、expected_revision、parameters.geometry_mode、budget，可选run_dir和Idempotency-Key；未知字段拒绝，尚未注册的专业计划409。当前可执行几何预览/导出、blockMesh/checkMesh、串行OpenFOAM与后处理，分别等待显式B/C/D/E确认；确认后自动派发下一已注册节点，准备失败返回workflow_dispatch.pending而不撤销确认。resume、专用工作流事件查询和前端未实现。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /cases/{id}/workflow-runs | 已注册几何/网格工作流创建、幂等复用并派发，持久提交后才领取执行 |
| GET | /cases/{id}/workflow-runs | 最近50条运行摘要 |
| GET | /workflow-runs/{id} | 步骤/尝试/处理器策略/发布版本与状态，不暴露claim token或argv |
| POST | /workflow-runs/{id}:dispatch | {expected_version}；显式投递已提交queued任务，不重放已领取任务 |
| POST | /workflow-runs/{id}:cancel | {expected_version}；持久取消，本进程拥有任务时立即调用本地取消；完整树待续 |
| POST | /workflow-runs/{id}/steps/{step_id}:refresh-confirmation | {expected_version}取步骤版本；刷新修订，但不能替换生产publication |

确认继续使用caseDict:advance的workflow_step_id/expected_step_version，要求本run生产版本仍当前有效。

## files（存储=事实源）

受控写入通过 case 数据库锁协调。普通文本 GET 额外返回 `content_sha256`、`dict_revision`；PUT `/file` 可带 `expected_revision`/`expected_sha256`，hash 省略兼容旧无条件请求，显式 null 要求文件不存在。PUT `/file`、`/caseDict`、`/caseDict/block`、扩展PUT/DELETE及错误PATCH成功回执包含本次提交的 `content_sha256`，用于推进编辑器保存基线。DELETE 和普通上传可在 query 带 revision/hash；普通上传还可用`expected_absent=true`要求目标不存在（与hash互斥），并返回实际提交hash。冲突返回 409 的结构化 `detail`（code/message/path/current_revision），调用方需重新读取而不是盲重试。分块上传开始时保存目标内容基线，完成时必须仍匹配；冲突保留目标和分块。实现边界见 [case-write-coordination.md](case-write-coordination.md)。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /cases/{id}/tree | 文件树（平铺 path+size+modified） |
| GET | /cases/{id}/file?path= | 读（文本→content；二进制→size/download_url；查看器优先 /raw，fs 下载链接限制见文末） |
| PUT | /cases/{id}/file | 写文本（caseDict 被 400 拦截走专用端点；写 A-geo/geometry.py 自动触发预览作业，响应带 auto_preview_job） |
| GET | /cases/{id}/file:version?path= | 锁内读取文本/二进制版本：path、exists、size、content_sha256、dict_revision、protected；缺失返回exists=false，目录/越界422 |
| POST | /cases/{id}/upload?path= | 上传二进制（multipart），普通上传限 16 MiB；超限 413，大文件用 /uploads |
| POST | /cases/{id}/uploads | 创建 fs 分块上传会话 {path,size,sha256?,overwrite=false,expected_sha256?}（条件省略兼容旧请求，显式null要求目标不存在），最大 1 GiB；创建时检查存储配额 |
| GET/PUT/DELETE | /cases/{id}/uploads/{upload_id} | 查询断点 offset / 上传 chunk（offset 连续、单块≤8 MiB）/ 取消会话 |
| POST | /cases/{id}/uploads/{upload_id}:complete | 收齐后校验整文件 SHA-256 并原子发布；错误 offset/hash/提前完成 409，目标默认不覆盖 |
| DELETE | /cases/{id}/file?path= | 删除（caseDict 保护） |
| GET/HEAD | /cases/{id}/raw?path= | 鉴权原始字节流；无 Range 200，单段 206，多段返回 multipart/byteranges（最多 16 段、总长≤64 MiB），非法/越界/重叠 416；支持 suffix range，HEAD 返回一致长度无 body |

## caseDict（状态机）

GET 在协调锁内返回内容、`content_sha256` 与 revision。整文、块、扩展 PUT 和错误 PATCH 支持可选 `expected_revision`/`expected_sha256`；扩展 DELETE 条件使用 query。锁内重新加载镜像及文件再做检查和局部修改；阶段推进仍要求显式确认和必需 revision。当前兼容无条件旧请求，输入封存后的强制条件策略尚未完成。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /cases/{id}/caseDict | 读 + 解析树 + workflow_state + dict_revision |
| PUT | /cases/{id}/caseDict | 同阶段参数保存；可带 expected_revision 防覆盖，过期或改变 workflowState 返回 409；未闭合/重复状态/非法 schema 返回 422。workflow_state 不能用于推进 |
| POST | /cases/{id}/caseDict:advance | {state, confirmed:true, expected_revision}；B 当前几何验证、C Mesh OK+输入/日志证据、D 成功 solve-evidence、E 本算例成功 postprocess+报告门禁；拒绝跳级/旧 revision，V 须先回 A；可成对传workflow_step_id/expected_step_version确认工作流等待节点，校验归属/步骤版本/等待revision与原文哈希；同步已有caseMeta.status |
| PUT | /cases/{id}/caseDict/extensions/{namespace} | 注册/更新 extensions v1 条目（version/enabled/settings） |
| DELETE | /cases/{id}/caseDict/extensions/{namespace} | 删除扩展条目；缺失 404，统一 revision/provenance |
| PATCH | /cases/{id}/caseDict/errors/{errorId} | 错误生命周期 investigating/fixProposed/fixed/verified/open；按 schema 校验并自动重算 summary，非法状态/缺证据 422 且 revision 不变 |
| PUT | /cases/{id}/caseDict/block | 块级写入；保注释与其他块，统一更新 DB 镜像 |
| POST | /cases/{id}/caseDict/materials:apply | 应用 `{key, library_revision, name, expected_revision, expected_sha256?}`，将源材料及摘要复制进当前算例。 |
| POST | /cases/{id}/caseDict/units:convert | length/mass/time/temperature/angle/force/pressure/energy 确定性换算；非有限值/未知量纲 422 |
| POST | /cases/{id}/caseDict/validate | 仅校验 |

## llm（BYOK 网关）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /llm/providers | provider 注册表（openai/anthropic/deepseek/kimi-coding/custom…） |
| GET/POST | /provider-profiles | profile 列表/新建；api_key 用 Fernet 加密存储且不回显；base_url 仅 HTTPS 或回环 HTTP，拒绝 URL 内嵌凭据 |
| PATCH | /provider-profiles/{id} | 编辑 provider/name/base_url/default_model；api_key 可留空保留或显式更换 |
| POST | /provider-profiles/{id}:test | 以存储密钥请求对应 `/models`，返回连接状态、模型数和耗时 |
| GET | /llm/models?provider_profile_id=N | 动态模型列表；失败返回 fallback:true、错误和当前 default_model，不返回密钥 |
| DELETE | /provider-profiles/{id} | 删除本人 profile |

## agent（SSE）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /conversations | 建会话（case_id+profile+permission_mode: approve_writes\|full_access\|ask\|plan） |
| GET | /conversations?caseId= | 会话列表 |
| GET | /conversations/{cid} | 详情（事件流[含祖先链合并]+tool_calls+pending_tools+current_goal）；`offset/limit` 从最新事件向前分页，返回 `event_window` |
| PATCH | /conversations/{cid} | 重命名（title 去空白、1–200 字符）；跨用户 404 |
| PATCH | /conversations/{cid}/permission | 修改权限模式；Plan 批准后切回 approve_writes，活动/待审批时 409，跨用户 404 |
| PATCH | /conversations/{cid}/settings | 一次保存可访问的 `provider_profile_id`、`model` 和 `permission_mode`。模型最多 200 字符，留空使用配置默认值。运行中或有待审批工具返回 409，跨账号会话或无权使用的配置返回 404。 |
| DELETE | /conversations/{cid} | 删除会话事件/工具记录；活动回合、待审批/执行工具或存在 fork 时 409，不删除算例文件 |
| GET | /conversations/{cid}/export | 导出完整可见历史为 session/event.v1 JSONL（祖先链、数值 id/eventId/time、按 Run 的 workingDirectory/mode/permission/model、ToolCall 快照，无密钥）；活动/待审批时 409；同节点归档忙 429。空会话可回导。[读快照、下载限额和回归](conversation-export-and-legacy-cache.md) |
| POST | /conversations:import?case_id={id} | UTF-8 JSONL 原子导入到当前用户新会话；支持自有回导和旧 message/tool/request，忽略 profile/工作目录/权限，活动状态只读化，摘要 covers_max_id 重映射；16 MiB/6000 事件上限 |
| POST | /conversations/{cid}/messages | 发消息 → SSE；可带 reasoning_effort: off\|low\|medium\|high（默认 off 不发送推理参数）及 images（最多 3 张 PNG/JPEG/GIF/WEBP、每张 4 MiB，Base64 输入，事件仅存附件元数据） |
| POST | /conversations/{cid}/messages:cancel | 设置当前会话取消 Event；无活动回合返回 ok:false，跨用户 404。部分 assistant 以 cancelled=true 保存 |
| POST | /conversations/{cid}/goal:run | 显式启动有界 Goal 自动续轮；逐轮检查配额/turn/tool 预算和取消，模型 completed 才完成，耗尽/失败/断开停止 |
| POST | /conversations/{cid}/fork | 分叉（零拷贝：forked_from_id+分支头指针，历史递归合并）；可选 head_event_id 仅从最终完成回答创建分支 |
| POST | /conversations/{cid}/compress | 上下文压缩（LLM 摘要落 system 事件 covers_max_id；keep_last 默认 6） |
| POST | /conversations/{cid}/handoff | 以有界文本开启新会话。可在已完成回答处传 `head_event_id`，或在原生回合提问处传 `before_answer_turn_id`。保留 case/profile/model/permission。 |
| POST | /conversations/{cid}/tool-calls/{tid}:approve | 批准危险工具 → 执行 → 续跑 SSE；body 可选 {reasoning_effort} |
| POST | /conversations/{cid}/tool-calls/{tid}:reject | 拒绝 |

### SSE 事件类型
`ready / text.delta / reasoning.delta / tool.delta / tool.state / tool.result / goal.updated / goal.progress / approval.requested / message.completed / turn.completed(内部不转发) / run.completed / run.cancelled / stream.done / stream.error / request.failed / request.timed_out`

每个 run 的 SSE 事件带 `run_id` 和严格递增 `seq`。相邻同类型 text/reasoning 按约 33ms 合并，工具、完成和错误事件先冲刷前序 delta，保持原始顺序；客户端断开与恢复语义见 run_state/run_checkpoint。

收到新的正文或推理后，API 最迟在下一个 750 ms 检查点期限开始保存，累计新增 UTF-8 字节达到 16 KiB 时提前保存。存储提交和调度会影响实际可读时间。回合结束时保存尚未落库的尾部。新断点载荷包含 `run_id`、`request_id`、累计正文和累计推理。若兼容 SSE 连接结束时既未收到 `[DONE]` 也未收到 `finish_reason`，客户端会收到 `stream.error`，其 `error_type` 为 `upstream_incomplete`。

读取会话详情时，`interrupted`、`failed`、`timed_out` 的 `run_state` 事件带有顶层 `recovered_checkpoint`。有待展示的断点时，该字段含 `content` 或 `reasoning`，即使原记录在上一页也可展示。该字段占用同一页的载荷预算，超出时使用已有的截断规则。没有未完成片段时返回 `null`。浏览器按终态所在位置展示一次，禁用该片段的回答分支操作。

JSONL 生命周期按会话、`run_id` 和 `request_id` 归属，`request_index` 从 0 起标记同一回合内连续的模型请求。导入旧档案时，即使两个回合复用了请求 ID，各自的终态和耗时也分别保留。未结束的请求会追加中断状态，在新会话中作为历史读取。[断点与生命周期验收](checkpoint-window-and-lifecycle.md)

示例帧（每帧以空行结束）：

```text
data: {"type":"text.delta","text":"部分回答"}

data: {"type":"run.completed"}

data: {"type":"stream.done","reason":"awaiting_approval"}

```

Agent 消息与工具批准均由前端 `fetch + ReadableStream` 解析，不要求使用 `EventSource`。assistant 消息 payload 分开保存 `content` 与真实 `reasoning`（仅 provider 实际返回时出现）；OpenAI 使用 `reasoning_effort`，Anthropic 使用 `thinking.budget_tokens`，low/medium/high 对应 1024/2048/4096。

会话详情默认返回最近 280 个事件，页内按时间顺序排列。翻页时传回上一页的 `event_window.next_offset` 和 `snapshot_id`。例如首页实际返回 100 条且 `has_more=true`，应请求 `?offset=100&limit=280&snapshot_id=<首页值>`。这样在他处追加消息后仍能接着读原来的历史。每次请求均核对用户的算例权限。省略快照参数会读取当时可见的最新历史。

成功的 API 模型回合在 SSE `run.completed.answer_event_id` 中给出已提交的回答事件 ID。可以把它作为 `head_event_id` 提交，正文相同的回答也能区分。详情中的 assistant 消息有 `can_fork` 标志。拥有回合终态记录时，`answer_state` 提供该回答的状态及已记录的计时字段。分支只包含选中回答及之前的可见事件。创建时与父会话共享算例文件，待审批工具和活动目标归原会话。无权访问或边界超出可见历史返回 404，可见事件不符合回答条件返回 409。[分支验收](conversation-answer-branches.md)

显示和模型输入分别使用 4 MiB 和 8 MiB 的事件载荷预算，模型最多取 1,200 个事件。历史浏览最多回看快照里最近的 6,000 个事件，`offset + limit` 超过 1,200 的请求使用 16 MiB 预算。`total` 为该分支与快照中全部可见事件数。单个事件超过预算时，响应的事件载荷带有 `truncated: true`，原文可通过完整 JSONL 导出读取。待审批工具和当前目标单独返回实时状态。[长会话验收](conversation-history-window.md)

工具卡以 call id 合并 `tool.state/tool.result`。`tool_calls` 只包含当前事件页引用的记录，不能绕过 280 条显示窗口；全局待审批记录仍由 `pending_tools` 单独返回。执行状态包括 queued/running/awaiting_approval/completed/error/cancelled/interrupted；服务启动只把孤儿 queued/running ToolCall 标记 interrupted，pending/executed 不变。审批续跑先返回真实工具结果，不返回“批准即成功”的占位成功。

assistant 的正文/reasoning 在双协议增量输出、落库和历史读取/重发前统一经过伪工具 XML/DSML/sentinel 清洗；闭合、逐字符分片和无结束协议块不会进入展示或模型上下文。Ask 模式不提供工具，provider 违规返回的结构化 tool_calls 也直接拒绝执行。

模型流限制为 64 MiB，正文+reasoning 合计 8 MiB，单 SSE event 8 MiB，单 tool call 2 MiB，权威 assistant 响应 10 MiB；超限以 stream.error 终止。错误正文只保留 16 KiB 脱敏预览，Bearer/API key/token/secret/sk-* 不落会话错误。客户端断开与生成器关闭会设置取消 Event 并关闭 provider iterator。

用户图片保存在 `.vibeflow/attachments/<id>/`，事件只带 name/mime/path/size。构造模型请求时读取原始字节，OpenAI 生成 image_url data URL，Anthropic 生成 base64 image block；非法 MIME、Base64、数量或大小在写文件前拒绝。

每个 Agent run 记录 run_state（started/completed/cancelled/failed/timed_out/awaiting_approval/interrupted）和 run_checkpoint（run_id+正文/reasoning）。每次模型调用另记录 request_state，包含独立 request_id、run_id、request_index、started_at/ended_at/duration_ms 和 started/completed/failed/timed_out/cancelled/interrupted；同一 Run 的多次 Request 不共用 ID，断开/启动/JSONL 导入会把遗留 started 恢复为 interrupted。run_state 的 started 包含 Unix 秒 `started_at`，后续状态包含同一 `started_at`、`ended_at` 和非负 `duration_ms`；前端按 run_id 显示固定起点的每轮耗时。checkpoint 按约 750ms/16KiB 及收尾 flush 写入；断开由独立 shield 任务写 interrupted，启动时把上次 started 恢复为 interrupted，恢复路径从持久化 started_at 计算耗时，前端只在没有最终 assistant 时恢复半截回答。

文件与文本提案在登记时把 `baseline_sha256` 加入 args（原文件不存在时为 null）。批准时再次读取并比较，外部修改或不存在→存在竞态返回 error 且不覆盖；普通文件在同一目录临时写入、flush+fsync 后 `os.replace`，caseDict 仍走统一专用写入。

`update_goal` 写入 append-only `goal` 事件，包含 session goal_id、递增 revision、验收条件、max_turns/max_tool_calls、状态和终态原因。`current_goal` 始终返回最新目标，不受显示分页影响；后续模型回合在系统提示中恢复目标。该工具只更新目标，不启动 F50 自动续轮。

### 内置工具
`read_text_file / list_files(默认排除 .vibeflow/) / search_text(排除重型目录) / web_search / web_fetch`（安全，免审批）；web_search 使用每 Agent run 独立 runId 和同 run 共享预算，provider 任务可由回合取消 Event 中止，默认 mock 明确返回非实时联网结果；web_fetch 校验所有 DNS/直接 IP、固定公网地址连接、保留原 Host/SNI/证书验证、逐次重定向复查并限制 8 MiB/超时，禁环境代理和 URL 凭据；
`run_shell_command / propose_file_update / propose_text_replacement`（危险，审批；full_access 模式直执，高危正则仍拦）；命令工具支持 `runtime: windows|wsl|powershell`，PowerShell 使用 `powershell.exe -NoProfile -Command`，三者都不是硬沙箱；
`update_plan / update_goal / get_skill`

## llm 内部补全
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /llm/translate | 划词翻译 {text,target:zh\|en,profile 可选缺省取最早} 非流式 |

## terminal（WS）
| 通道 | 协议 |
|---|---|
| WS /ws/terminal?token=&cols&rows[&shell=cmd\|wsl\|wsl-sandbox][&session=id][&case=id] | 有效 JWT 和活跃账户必需。指定算例须由本人所有且可用。新建、重连和输入均重新核验权限，权限失效后关闭 4403。就绪前断开取消启动，就绪后可挂接本人同算例的原会话并补发最近输出。输入行列为 1–1000，JSON 控制帧包含 input、resize、ready、exit，PTY 输出用二进制帧。S3 工作副本准备期限、过载与来源变化错误见[终端启动说明](terminal-preparation-capacity.md)。 |
| GET /api/v1/terminal/sessions | Bearer 认证，仅列当前用户会话 [{id,shell,alive}] |
| DELETE /api/v1/terminal/sessions/{id} | Bearer认证，仅所有者可关闭，等待父进程退出再注销；WS断开取消转发订阅但保留PTY供重连 |

## jobs
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /cases/{id}/jobs | 提交 {type: solve\|mesh\|pvpython\|geometry\|convert\|shell, command, runtime: windows\|wsl\|wsl-sandbox, timeout_seconds, max_log_bytes, run_dir?, input_paths?, input_publications?}；默认 86400s/16MiB。run_dir 仅 solve 可用，必须为 C-run 下相对目录且有 controlDict/网格；用于冻结输入并生成求解场/日志证据。不指定的通用作业可运行但不构成D阶段证明。input_paths选择1–256个相对文件/目录，在独立attempt副本执行；当前fs限定，不能与旧run_dir证据混用；见attempt-workspaces.md。input_publications选择1–16个本case成功发布版本及可选文件清单，默认require_current=true，可显式历史；与普通输入重名拒绝，运行前/收尾复核，详见publication-inputs.md |
| GET | /cases/{id}/jobs | 列表 |
| GET | /jobs/{id} | 详情（含log_backlog尾部、parsed_summary；新作业附attempt的id/number/root_job_id/phase/reason_code/retry_of_attempt_id，显式封存项附workspace/outputs清单，不暴露claim凭证；历史详情可读持久phase）。作业额度和原生用量通过 `attempt.windows_compute_usage`、`attempt.wsl_compute_usage` 或 `attempt.linux_compute_usage` 读取。 [Windows](FF-H18-windows-compute-budget.md) · [WSL 与 Linux](FF-H18-wsl-compute-budget.md) |
| GET | /jobs/{id}/process | 只读核验持久进程身份；match/exited/mismatch/unverified，先校验case权限；不发送信号、不透传无效/旧记录 |
| GET | /jobs/{id}/log?offset= | 增量日志（轮询） |
| GET | /jobs/{id}/log-stream | SSE 实时日志（backlog→增量→done；订阅竞态已修：收尾先于订阅也发 done） |
| POST | /jobs/{id}:cancel | 本地运行项取消；未领取queued项在锁内复查后持久取消，已claim返回ok=false及状态变化提示；跨worker完整取消待续 |
| POST | /cases/{id}/runs:derive | fs工况派生：case锁内枚举/hash/配额→私有目录流式复制并复核→无覆盖发布C-run/{name}；跳结果时刻/日志/foam/processorN，保留0初场；源变化或目标冲突409，失败/取消清理本次暂存 |

状态机：`queued→running→succeeded\|failed\|cancelled`，DB 实时回写；服务重启时孤儿 queued/running → `interrupted`。`ended_at` 为 Unix 秒时间戳；内存进程已不存在时，详情和列表均回读 DB 持久化时间，未完成作业仍为 `null`。
求解器日志结构化：`GET /cases/{id}/logs/parse?path=` → chart-ready（残差/Courant/时刻/耗时）。追加读取使用 `incremental=true&offset=N&fingerprint=SHA256`：只返回上次完整行游标之后的新内容，半行保留到下次；`cursor` 提供新 offset/fingerprint/unchanged/pending_bytes，已消费前缀被重写或截断返回 409。

## mesh
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /cases/{id}/mesh:evaluate | 解析指定 checkMesh.log，写 `B-mesh/quality.json`（指标、violations、policy、mesh hash）；A/B→C 按 policy 门控 |
| POST | /cases/{id}/mpi:run | 接收 `run_dir`、`solver`、`subdomains` 和可选布尔 `force`。以封存输入在私有工作区执行分解、求解与重构。省略 `force` 沿用原行为，显式 `false` 遇到既有分区返回 `409 mpi_processor_exists`。 [分解选项](mpi-publication.md#2026-11-22-旧分区与状态读取) |
| GET | /cases/{id}/mpi:status?run_dir= | 返回 processor 目录、重构时刻和 complete 状态 |

## geometry（A 阶段）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /cases/{id}/geometry:preview | 封存 caseDict/A-geo，在独立attempt执行；提交不可变scene/validation/toolchain。可选Idempotency-Key，返回job_id/status，新请求另含publication_id/generation；见geometry-publication.md |
| POST | /cases/{id}/calculix:solve | CalculiX `.inp` 真实 ccx 作业（VIBEFLOW_CALCULIX 显式路径/fail closed），输出 DAT/FRD/STA |
| GET | /auth/oidc/providers | 返回已配置的默认 OIDC provider，未配置为空 |
| POST | /auth/oidc/default/start | 生成 authorization code+PKCE S256 授权 URL 和单次 state |
| POST | /auth/oidc/default/callback | code+state 换平台 JWT；token/userinfo 验证且 email_verified=true |
| POST | /cases/{id}/flotherm:inspect | I01/I02 ECXML、V2310 PDML、XTXML/XTXMLA 配对与补充 ECXML，带摘要、重复 bbox 和阻断项。单文件上限 32 MiB。 |
| POST | /cases/{id}/flotherm:import | 接收 `paths`、可选的逐文件 `expected_hashes` 与 `settings_path`，返回由活动三维盒体生成的 IR JSON 文本和覆盖摘要。摘要变化返回 409。独立 PDML 需要计算域及环境温度配置，生成的 IR 可用现有文件接口条件保存。 |
| POST | /cases/{id}/flotherm:convert | I01 异步 IR/settings→OpenFOAM case+Allrun+覆盖报告；保留日志和部分输出 |
| POST | /cases/{id}/flotherm:build-library | I09 复制辅助库源码并 source v2406 后 wmake libso，默认不覆盖，安装到 FOAM_USER_LIBBIN |
| POST | /scnet/{operation} | admin SCNET 操作；jobs/files/containers/notebooks/desktops，签名 token、错误脱敏，变更需 confirmed=true |
| POST | /scnet/files:download | 管理员下载普通文本或目录 ZIP。请求沿用 `base_url`、签名凭证及 `args.path`，目录路径以 `/` 结尾。解码上限 100 MiB，返回附件。`/scnet/files.download` 保留 1 MiB JSON 预览。[下载说明](scnet-download-admission.md) |
| POST | /remote:execute | C16 admin SSH executionProfile；host allowlist+client key+known_hosts+超时，返回 stdout/exit_status |
| GET | /remote/profiles | 当前组织管理员可用的部署配置，返回名称、调度器、分区或队列、墙钟上限 |
| GET / POST | /cases/{id}/remote-jobs | 查询本人作业或以 `Idempotency-Key` 提交脚本、输入、成果及可选历史检查点 |
| GET / POST | /cases/{id}/remote-jobs/{runId} / `:refresh` / `:cancel` | 持久详情、实际调度状态及取消收尾，详见[远程集群作业](FF-C16-remote-hpc.md) |
| GET | /cases/{id}/remote-jobs/{runId}/files?path=… | 校验原归档的长度与摘要后下载成果或调度日志 |
| POST | /cases/{id}/paraview:run | D15 真实 ParaView 5.11.2 pvpython/pvbatch `.py` 离线作业；VIBEFLOW_PVPYTHON/PVBATCH 可覆盖路径 |
| POST | /cases/{id}/su2:solve | SU2 `.cfg` 真实 SU2_CFD 作业（VIBEFLOW_SU2 显式路径/fail closed），复用 solve 作业监督并保留官方 restart/VTU/CSV 输出 |
| POST | /cases/{id}/elmer:solve | 提交 `{"input_path":"C-run/elmer/heat.sif"}`，封存 SIF 和网格后启动 Windows ElmerSolver，发布 `.result` 和 VTU。支持 `Idempotency-Key` 及 SIF 声明的 ASCII 或二进制状态续算。[输入格式](elmer-runtime.md#平台提交与结果) · [续算](elmer-runtime.md#从状态文件续算) |
| POST | /cases/{id}/moose:solve | 提交 `{"input_path":"C-run/moose/thermal.i"}`，封存单文件、调用 MOOSE 并发布 CSV 与 Exodus。瞬态支持检查点发布，续算通过 `restart` 中的 `publication_id` 与 `checkpoint_path` 选择封存的原状态。支持 `Idempotency-Key`，使用通用作业入口取消和重新封存。[提交](open-solver-native-runtime.md#moose-平台提交) · [续算](open-solver-native-runtime.md#moose-检查点续算) |
| POST | /cases/{id}/code-aster:solve | 提交 `{"input_path":"C-run/code-aster/axial.export"}`，封存 `.export` 及声明的输入，隔离执行并发布 `.mess`、MED、表格或计算库。通过 `restart.publication_id` 和 `restart.checkpoint_path` 选择原计算库。支持 `Idempotency-Key`、取消和按当前输入重新计算。[提交](open-solver-native-runtime.md#code_aster-平台提交) · [续算](open-solver-native-runtime.md#code_aster-计算库续算) |
| POST | /cases/{id}/execution-submission | 原请求回执查询。五种开源求解提交使用 `kind: "native"`、原 `key` 和含 `solver`、`input_path`、可选 `restart` 的 `body`。SU2 的直接提交仍使用 `config_path`。找到时返回 `case_id`、`kind`、`found: true` 和原作业 `id`，请求摘要不一致返回 409。 |
| GET / HEAD | /cases/{id}/provenance/artifacts/{publication_id}/file | 按 `path` 读取成功的开源求解发布。原件来自指定的当前或历史发布，读取时核对文件摘要。支持 `Range` 和 `X-Vibeflow-File-Version`。须具有算例读取权限。 |
| POST | /cases/{id}/jobs:batch | C12批量1–50项/并发1–4；Job、manifest/index在case锁内准备后commit再派发；坏索引拒绝、提交前写盘失败恢复原清单；等待用户级配额空位 |
| GET | /cases/{id}/job-batches | 最近 50 个批次的持久化状态、分类计数和关联作业 |
| GET | /cases/{id}/job-batches/{batch_id} | 单批次实时状态与作业明细 |
| POST | /cases/{id}/job-batches/{batch_id}:cancel | 立即取消运行项和全部排队项；按 case 统一取消同样生效 |
| POST | /cases/{id}/udf/scaffold | case锁内生成9个D1–D3文档/源码；所有目标须不存在，部分目录冲突也409 |
| POST | /cases/{id}/udf:build | D4 wmake libso + CDLL 动态加载作业 |
| POST | /cases/{id}/udf:integrate | 锁内保留原functions/include/代码并插入D5对象，提交后运行icoFoam；同名/直接同类型409，无效块400 |
| POST | /cases/{id}/geometry:export | 同预览的隔离/幂等契约，另发布STEP/STL/manifest及自定义导出产物；与preview共用geometry代次/head |
| POST | /cases/{id}/geometry/repair | sew/fix/mirror/periodic/scale 修复 STEP 并写 validation；默认不覆盖，显式 overwrite 才可替换 |
| POST | /cases/{id}/geometry/msh-preview | meshio→VTU 转换到 `.vibeflow/msh-preview/<hash>/preview.vtu`，供现有 viz reader；source hash、16 MiB/网格上限 |
| GET | /cases/{id}/geometry/import-scene?path=... | STEP/STP 只读导入为 CAD scene；16 MiB、50 万顶点/100 万面上限，source hash，不改写 scene.json |
| GET | /cases/{id}/geometry/validation | 按持久head读取不可变validation，附publication/current/stale；可用publication_id指定历史。手工修改后的未发布草稿返回409；无head的历史case保留旧契约 |
| GET | /cases/{id}/geometry/scene | 同head或publication_id读取scene；草稿场景可显示但标记stale；读取校验manifest及文件hash |
| GET | /cases/{id}/geometry/publications | 本case最新50条几何发布，含id/generation/status/attempt_id/current/path/manifest_sha256，按case归属授权 |

上传几何文件转换共用每进程两个名额。容量忙返回 `geometry_conversion_busy`（429），转换超时返回 `geometry_conversion_timeout`（504）。转换期间源文件变化返回 `input_stale`（409）。获准访问工作区的只读成员可调用 `import-scene`。[资源与一致性](geometry-conversion-capacity.md)

## postprocess（D 阶段）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /cases/{id}/postprocess:run | 从 caseDict.postProcessing 生成不可变快照并执行；body 可用 run_dir 覆盖 runDir；仅 fs |
| GET | /cases/{id}/postprocess/results | 读取最近一次完整成功的 metrics+manifest；?job_id=N 读取指定版本 |

配置、指标命名、产物和证据边界见 `postprocess.md`；失败作业不更新 latest 指针。

## report（E 阶段）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /cases/{id}/report:compose | 全部入口注册；封存caseDict/质量/证据后组织9章，固定生成时间；overwrite与revision/hash保护，原字节备份和双通道持久历史；Idempotency-Key，wait=false或20秒同步等待 |
| POST | /cases/{id}/report:build | 全部入口注册；封存Markdown/本地资源和适用工程证据，真实Pandoc编译及独立复验，持久代次/不可变HTML-DOCX/历史；Idempotency-Key，返回job_id/publication_id/generation；取消/过期不覆盖当前版本 |
| GET | /cases/{id}/report/outputs | 产物列表 |
| POST | /cases/{id}/latex:table | 生成安全 tabular；支持转义、booktabs、边框和 l/c/r align |
| POST | /cases/{id}/latex:build | 注册latex.build：main/recipe/resources、Idempotency-Key；封存声明资源及动态读取证据，独立PDF/SyncTeX/BBL复验，按主文件持久版本及历史源码；禁shell escape/norc、原生120秒timeout；外部未封存依赖拒绝发布 |
| GET | /cases/{id}/latex/errors?job_id=N | 读取属于当前 case 的 LaTeX 日志，返回错误/warning、文件/行号/上下文；跨 case 404、非 LaTeX 400 |
| POST | /cases/{id}/synctex:query | {pdf,page,x,y} → SyncTeX 反向映射 {file,line,column}；坐标为页面左上原点 72dpi，缺 sidecar 409、无映射 422、超时 504 |
| GET | /cases/{id}/latex/outputs | LaTeX 产物列表 |
| POST | /cases/{id}/pdf:number | {input, output?} → argv 页码 worker；默认 `<stem>.numbered.pdf`，不覆盖源/已有输出 |
| GET | /cases/{id}/ppt/presets | 当前算例可用的 187 项 DrawingML 原生图形、连接线和任务编号 |
| POST | /cases/{id}/ppt:build | {title, slides, template?, output} → 版本化可编辑 PPTX。页级 `beautify`、`native_shapes`、`template_layout`、`reconstruction` 和 `notes` 可选。详见 [PPT 交付与验收](ppt-delivery.md) |
| POST | /cases/{id}/ppt:video | {input, output?, language?} → 带备注的算例 PPTX 生成版本化 MP4。`language` 支持 `english` 和 `chinese` |

配置后处理提取/图表/参考比较时，两种报告构建都检查同指纹的 `report_ready` 证据；缺失、过期或失败在创建 Job 前返回 409，详见 `postprocess.md`。LaTeX诊断支持源码行跳转，真实PDF/SyncTeX反向映射已支持注册当前/历史版本与原版本源码读取；PDF/PPT worker 均以 argv 执行且独占创建新 artifact。

## index（工作区索引）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /cases/{id}/index:rebuild | FTS5 全量重建（线程执行不阻塞） |
| GET | /cases/{id}/index/status | 索引状态（条目数/时间） |
| GET | /cases/{id}/search?q=&content=&limit= | 名字 LIKE + FTS 内容 snippet；普通写删、multipart 上传、caseDict 与 Agent 写入即时增量，作业收尾重建，外部进程修改由 watcher 补充 |
| GET | /workspaces/{id}/search?q=&content=&limit= | 当前用户工作区内跨 case 搜索；轮转合并避免单 case 占满名额，每个 hit 带 case_id/case_name，返回 total_cases/indexed_cases，跨用户 404 |

## viz（可视化会话）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /cases/{id}/viz:sessions | 以 `{run_dir}` 启动 OpenFOAM 或以 `{file_path}` 打开 VTK/VTU/VTP/VTM/XDMF/Exodus/STL/series。`run_dir` 为 `.` 时使用算例根目录，要求有 `system/controlDict`。每算例最多 2 个活跃会话。S3 工作副本在算例锁外下载，登记时复核来源及配额。预算到期返回 `504 / viz_preparation_timeout`，准备中的 S3 请求占满本 API 的两个名额时返回 `429 / viz_preparation_busy`。[结果文件说明](result-readers.md) |
| GET | /cases/{id}/viz:sessions | 锁内读取本API会话列表，等待启动提交后含存活检测 |
| DELETE | /viz:sessions/{sid} | case锁内终止kernel父进程并等待退出、关闭日志后注销；完整后代树仍待续 |

kernel = 独立进程（venv python + trame + OpenFOAMReader/通用 VTK readers 离屏渲染），绑 127.0.0.1；
**前端 iframe 走网关鉴权反代**：`/viz-proxy/{sid}/{path}`（HTTP 静态）+ `/viz-proxy/{sid}/ws`（wslink 隧道）。首页 `?token=<jwt>` 成功后按 sid 路径设置 HttpOnly/SameSite=Strict cookie，子资源和 wslink 复用；每次仍要求 token 对应用户为 viz session 所有者。

## 技能、文献、组织与运行时

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /skills | 技能注册表列表（内置 < 组织 < 用户覆盖，支持多组织） |
| POST | /skills | 用户或组织 admin 创建/更新技能；默认不覆盖，overwrite=true 才覆盖 |
| GET | /skills/{name} | 读取指定技能（按当前用户/组织覆盖顺序） |
| POST | /skills/{name}/package | 将用户/组织技能打包为 ZIP |
| POST | /skills/install | multipart 安装技能 ZIP；限 100 文件/1 MiB，拒绝穿越/绝对路径/符号链接，默认不覆盖 |
| GET | /cite/search?q=&source=crossref\|arxiv\|semanticscholar | 在线文献检索并规范化 BibTeX；Semantic Scholar 有界 429 重试/Retry-After，持续限流 503、网络失败 502 |
| GET | /workspaces/{id}/sharing | owner 查看组织分享配置 |
| PUT | /workspaces/{id}/sharing | owner 设置组织 viewer/member 分享或停用；member 角色可写，viewer 只读 |
| PUT | /org/{org_id}/provider-profiles/{pid} | profile owner+org admin 启用组织共享；密钥不回显 |
| DELETE | /org/{org_id}/provider-profiles/{pid} | profile owner+org admin 撤销组织共享 |
| GET | /org/plans | free/pro/team 套餐目录、价格展示与配额 |
| GET | /org/{org_id}/billing | admin 查询当前订阅与历史，归档已到期订阅 |
| POST | /org/{org_id}/billing:mock-checkout | admin 以 confirmed=true 执行 mock 套餐切换；不真实扣款，检查目标 seats |
| POST | /org/{org_id}/billing:renew | admin mock 续费 monthly/yearly |
| POST | /org/{org_id}/billing:cancel | admin 立即取消当前生效订阅并保留历史 |
| GET | /org/mine | 当前用户组织、套餐、成员和四维配额用量 |
| GET | /org/invitations | 当前用户待接受组织邀请 |
| GET | /org/{org_id}/audit | admin 查询组织审计哈希链；action/actor 过滤、offset/limit 分页、chain_valid |
| GET | /org/{org_id}/members | admin 查看成员、角色、邀请状态和 seat 计数 |
| POST | /org/{org_id}/members | admin 邀请已注册用户；角色 admin/member/viewer，邀请预留 seat |
| PATCH | /org/{org_id}/members/{user_id} | admin 修改角色或 disabled；保护最后 active admin 和 seat 上限 |
| DELETE | /org/{org_id}/members/{user_id} | admin 撤销待接受邀请，不删除 active/disabled 成员 |
| POST | /org/{org_id}/invitation:accept | 当前被邀请人接受邀请并占用 active seat |
| POST | /org/{org_id}/invitation:decline | 当前被邀请人拒绝邀请并释放预留 seat |
| POST | /runtime/client-error | 客户端 error/unhandledrejection 脱敏上报；message≤2K、stack≤8K、url≤500、UA≤300，append-only JSONL，空 message 400 |
| GET | /runtime/client-errors?limit= | 组织 admin 读取最新客户端错误，不包含 prompt/case 内容或凭据 |
| GET | /runtime/processes | 当前用户活动 Job/PTY/viz/Agent/Goal 列表，可按 case_id 过滤，其他用户资源不可见 |
| POST | /runtime/processes:cancel | 按 case 取消并清理全部活动进程，返回各类型 cancelled 数量、remaining 和 errors；跨用户 404 |
| GET | /runtime/cli-profiles | CLI 兼容配置档；codex-wsl 返回 shell=wsl 与 PYTHONUTF8/PYTHONIOENCODING/TERM，不执行 CLI 或读取凭据 |
| GET/PATCH | /runtime/config | 读取/修改管理员 Setup：python、pandoc、wsl_distro、openfoam_bashrc；PATCH 仅组织 admin，原子持久化，非法路径/非授权发行版 400，空值恢复默认 |
| GET | /runtime/monitor | admin 读取真实 DB/存储延迟、作业/进程状态、限流 bucket 与当前告警 |
| GET | /runtime/metrics | admin 获取 Prometheus 文本指标 |
| GET | /runtime/capabilities | Python/DB/存储/pandoc/WSL/CLI agent 自检；存储返回真实写读删/bucket 探测 ok、latency_ms、error；WSL 返回 v2406 版本及 OpenFOAM/wmake 实际路径；cli_agent_details 提供显式路径/版本目录/PATH 来源，不执行 CLI 二进制；environment_registry 返回 `VIBEFLOW_*` explicit/fallback/explicit_error，stage_checks 返回 A–E/V 最小阶段检查 |
| GET | /runtime/opencode:status | opencode 与契约注入状态 |
| POST | /runtime/opencode:apply-prompts | 已退役，返回410；改用算例CLI契约，原全局文件和备份保留 |
| GET | /cases/{id}/cli-contract | 当前CLI契约、文件状态、版本摘要和最近32份备份目录，见[契约与恢复](cli-contracts.md) |
| POST | /cases/{id}/cli-contract | 携带expected_revision，备份后应用Claude/Codex/OpenCode/Kimi项目约定，保留用户原文 |
| POST | /cases/{id}/cli-contract/backups/{backup_id}:restore | 携带expected_revision，先备份当前内容，再恢复所选文件快照和工具选择 |

## 杂项
- `WS /ws/events?case_id=<id>&token=<JWT>`：用户级事件总线，事件类型 `files.changed / jobs.updated / scene.available / runtime.degraded / quota.warning`；按资源所有者过滤，队列有界，断开退订
- `GET /api/health` 存活探针
- `GET /api/v1/completion/openfoam?q=&kind=&parent=&context=&limit=` OpenFOAM 补全（自建索引懒加载；未构建时 ready=false）；v2 索引返回 source、generator_sha256、baseline_sha256、去时间戳确定性 index_sha256，item 带 confidence 并按 confidence/occurrences/label 排序；基准精确为原版 31,542 唯一 label，合入自有通用 kind/parents/contexts/sourceExamples；kind 覆盖 C++14/preprocessor、macro、runtime registrations/tag、compoundDictionaryKey、foamFileClass、commandLineToken、environment/wmake 等富分类

## 当前通道限制

- fs 二进制 `download_url` 指向 `/cases/{id}/raw?path=...`，调用时需携带 Bearer；S3 继续使用预签名 URL。
- `/raw` 支持单段与 multipart 多段 Range；无 Range 的 GET 仍一次读取全部字节，超大文件需使用 Range 并做性能验证。分块上传是自有 JSON/offset API，不宣称 tus 线协议或 S3 分块兼容。
- Job、PTY、viz 的运行状态主要在单后端进程内，不能按多 worker/多副本接口语义依赖。
- 通用 Jobs 与 Agent 共用高危命令底线，Jobs 在 DB/进程副作用前返回 400；这不是完整沙箱。
- S3 并未透明覆盖作业、索引、终端、几何和可视化的本地文件访问；prod 默认持久化 fs，显式 S3 在未设置 `VIBEFLOW_ALLOW_PARTIAL_S3=true` 时于启动阶段拒绝，旁路只用于受限文件 API 测试。
