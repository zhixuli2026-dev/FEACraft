# 未封存输入的明确重新准备

2026-11-11补充：工作流作业现在可核对原流程、明确确认当前输入和A阶段重置，创建持久关联的新流程，并在响应丢失后沿用原确认回读。原流程及旧停止限制保持。注册重算、旧计划迁移和真实浏览器导出、导航、刷新及取消已有实际证据，见[工作流输入恢复](workflow-input-recovery.md)。下方“仍须交付”是11月10日该独立请求子阶段的历史范围，当前余项以新页及主线台账为准。

2026-11-10，N5-07输入恢复子阶段。主线仍38/53完成、剩15项；N5-07尚未整项完成，全部53项及295项全功能范围保持。本批源码封存名称为`mainline-N5-07-input-recovery-20261110.zip`，实际结果和摘要以`tmp/mainline-N5-07-input-recovery-evidence/validation.json`为准，不据此宣告整个N5-07完成。

## 用户操作

在作业的“恢复与重新执行”中，未封存输入会显示“核对输入并创建独立新请求”。用户明确填写新命令、运行环境及当前输入路径，或选择原作业类型对应的注册专业处理器与参数；填写新请求期限、日志上限和可选说明，先核对，再确认创建并执行。普通命令不填写路径时使用现有的当前算例完整封存规则，执行始终使用独立工作副本。原命令不会自动填入或执行。

原请求若尚未执行，会与新封存请求、投递意图、恢复关系及审计在同一事务中取消/创建；失败则不提交这组变更。新请求是独立根作业，使用自己的输入、尝试和资源预算。原Job、原attempt、原未封存清单、旧文件和旧尝试关系保留，不重建旧输入，不补造停止证明，也不把新结果当作旧请求的成功结果。工作流内请求继续由原流程的重算协议处理，此独立入口明确拒绝。

确认记录先写入绑定账户、算例和原Job的浏览器sessionStorage，再发送请求。响应丢失时保留同一标识和原参数；页面刷新后“继续核对此次新请求”仍使用原标识。后端先核对权限及已提交回执，再核对新变更的前置条件，因此原请求已经取消后，原确认仍可读取同一新作业。明确收到参数/版本拒绝后才能修改确认。旧、新作业均可读取同一关系和审计，并相互导航。

## 服务约束和API

已有可信封存、停止及写入结束证明，但未绑定注册处理器的旧专业作业，使用原`repeat`协议中的`reprepare_handler`。界面显示“用当前输入重新准备处理器”，必须选择同作业类型的正式处理器，并填写JSON对象参数。它按当前输入创建独立新根作业，保留原失败、旧argv和原停止证明；不作为重试旧命令或沿用旧检查点。该入口与未封存请求的`recover-inputs`入口分别保留原协议和固定提交标识。

`GET /api/v1/jobs/{id}/input-recovery`返回原请求快照摘要、原attempt、当前字典版本/事件序号、可选处理器及既有关系。`POST /api/v1/jobs/{id}:recover-inputs`需要固定`Idempotency-Key`，action为`reprepare_current_inputs`；提交GET中取得的source_attempt_id、source_snapshot_sha256、expected_revision、expected_event_sequence，以及明确的新命令或handler/parameters和预算。成功返回202、独立新Job及input_recovery凭证。`GET /api/v1/jobs/{id}`新增input_recoveries关系，原有repeat接口及幂等摘要不变。

只允许一个没有claim、worker/lease/heartbeat、started_at、原生/子进程、输出、发布或写入记录的单独初始尝试，且原状态为queued或cancelled、head仍指向该尝试、没有当前本地执行者、没有可验证封存。曾经领取、执行归属或写入记录损坏、未知归属、工作流、丢失尝试关系和有可信封存的请求均不能借此绕过原恢复控制器。只读查询和确认不会向未知进程发信号。

确认时复核原请求完整快照和当前输入/事件版本。专业准备沿用正式注册处理器；需要原生工具时，在SQL锁外走现有document_admission，再复核来源及准备结果。取消旧queued请求只是对未领取请求的持久取消决定，不能作为物理停止证明。新执行依然经过正式封存、处理器绑定、投递、claim、原生启动、预算和发布控制。

新增`JobInputRecovery` / `job_input_recoveries`，用原/新Job与case的复合外键、原attempt与Job的复合外键约束归属；原Job、新Job各自唯一，防止不同确认产生多个替代请求。凭证记录操作者、原快照、当前输入版本、新Job/attempt、请求摘要、处理器及说明，并明确`old_execution_replayed=false`、`original_inputs_reconstructed=false`、`stop_evidence_created=false`、`new_execution_requested=true`。原、新执行记录不被当作同一尝试。

Alembic增量为`c20e87f143b9 -> d31f98a254ca`，只新增关系表和约束/索引。本轮没有迁移开发库或重启端口8000。隔离真实CLI/VTK历史副本已升级、回退、再升级，原三类native历史及会话关联保持。该回退会删除新增关系表；目前只演练空表回退，不宣称回退保留新增审计，也不代替N7完整混合数据/回滚演练。

## 实际证据

- `tmp/job-input-recovery-jcw2qb07/input-recovery-result.json`：普通命令新请求、当前v2输入、独立目录实际文件、原请求取消未领取、原输入清单保持、同键同Job、过期输入/快照与未知worker元数据拒绝、单原请求只允许一个替代请求通过。真实Windows进程停止和API创建句柄退出均核验。
- `tmp/job-input-recovery-tpa1zazi/input-recovery-result.json`：上述场景及注册`report.build`通过。真实`report.prepare`原生请求和Pandoc报告作业均停止已核验，实际HTML包含本次当前输入，重复确认保持同一Job；源真实CLI/VTK历史未改。这里没有执行模型推理，真实模型0、模拟LLM0。
- 实际浏览器`tmp/input-recovery-browser-gc4v2i02/browser-finished.json`：空命令、错误JSON对象、实际文件修改后的409、真实202提交后中断响应、刷新后同确认读取、旧新导航、专业处理器选择与实际报告提交均有证据。原6→新8、原7→新9，每个算例只有两个Job。自然UI重试时HTTP status/id/created=false/key/body断言通过，随后驱动在新折叠区域内等待标题超时；重新打开实际区域后读取到新8、同凭证和已清除的确认记录，没有再次提交。该定位超时保留。
- 上述浏览器runner最终`UI-result.json`为失败，原因是对实际Windows执行标记采用了LF专用断言。实际字节是`[111,110,99,101,13,10]`，即一行CRLF；没有重复执行。原失败未改为通过，原入口源码保存在`tmp/job-input-recovery-ui-entry-v1-20261110.py`。当前入口只修正换行归一化，未重跑原程序。
- `tmp/input-UI-readback-p1sletpj/readback-result.json`：复制上述真实结果，由新API使用原实际key/body读取同一新8/凭证，重复仍created=false；经HTTP读取实际CRLF单行、当前输入输出、专业HTML、原生准备及两作业停止证明。十张原业务/执行/关系表保持，原源库未改，未新增业务作业或重算。原UI的两个PTY、API和预览进程退出证明保留；本回读API也持有创建句柄确认退出。整体证据是原UI动作与独立回读的组合，不能把原失败runner标绿。
- `tmp/input-multi-API-_5e8jifq/multi-result.json`：独立临时PostgreSQL的七组场景通过。同键普通命令/注册报告并发各只创建一个新Job并读取同一凭证；异键只能产生一个替代请求；外账户、只读和撤销权限拒绝操作，成员同键读取保持。真实原生准备完成后分别取消、修改输入、退出实际API，均没有隐式创建新Job，重新确认才执行。增量升级/空表回退/再升级保留旧Job元数据，不冒称完整混合native迁移。三个API持有创建句柄确认退出，实际作业及准备停止核验；`tmp/N5-input-PG-drop-verified-20261110.json`独立确认自建库已删除，开发库未改。
- `tmp/input-multi-API-kvnl6mbl/multi-result.json`：SQLite同组场景及“无注册处理器的历史类型不可执行”共八组通过，三个实际API的创建句柄退出和所有本组实际作业/准备停止核验。上述两组均真实模型0、模拟LLM0；属于实际SQL/HTTP/进程/文件操作及明确故障条件，不作为模型推理证据。

最初backend运行`tmp/job-input-recovery-6mvl4jh2`在已成功的新执行后，发现另一个确认标识错误返回快照冲突，未返回更准确的“已有替代请求”。已调整查询顺序，后续实际验收通过；原失败及v1源码保留。构建的首个PowerShell重定向因原生stderr警告报告非零，实际Vite已产出；随后通过Python子进程取得真实npm exit0，日志`tmp/N5-input-recovery-web-build-v2-20261110.log`。项目实际lint为oxlint，定向执行0错误/4警告；首次误用不存在的eslint入口仅是工具失败，没有安装依赖。设计检测器结果为空。

1440px/390px、浅暗主题、最小44px控件、文本对比度及可见2px键盘焦点已在原输入恢复界面实测。默认短Dock曾截断截图，通过实际分隔条拖动和面板滚动可操作全部表单。保留初始截断截图和实际展开后截图；不能据此称整个移动工作台已完成响应式验收。随后已实现完成态审计标题/说明、repeat拒绝原因中文和旧已封存专业处理器的显式重新准备选择/参数/固定确认。新一批构建及oxlint退出0、设计检测为空；该批新增交互仍待实际浏览器验收，不能沿用此前截图声称通过。

## 旧处理器真实界面验收

`tmp/legacy-reprepare-browser-kzdtoq6x/UI-result.json`通过：真实Windows旧固定argv执行一次、退出23并完成执行器后续清理后，浏览器选择`report.build:1`及明确参数。错误JSON没有产生新作业；实际202提交后中断响应，刷新并重新选择原算例/作业，再沿用同key/body读取同一新11和操作关系，created=false。真实准备/Pandoc停止核验，HTML包含当前随机输入；旧10的Job/attempt全部字段保持，外部执行标记仍只有一行。实际算例只有10、11两个作业，原三类native历史及输入审计保持。两个页面自动PTY均有持久停止证明，API与预览创建句柄确认退出。真实模型0、模拟LLM0。

桌面深色及390px浅色表单无横向溢出，控件至少44px、键盘焦点2px；实际截图已查看。随后仅修正旧处理器未注册时的上下文说明，避免误说成未核验停止，并在持久化前拒绝超过读取上限的确认记录；该小批编译/oxlint通过，未另跑推理或重复计算。`tmp/N5-input-finish-design-detect-20261110.json`为空，仅为源码正则扫描，不代替渲染证据。上文新增交互待验收的记录是早前状态，以本节的新证据为准。

前两次验收失败分别保存在`tmp/legacy-reprepare-browser-9vd4snhp/UI-result.json`和`tmp/legacy-reprepare-browser-hhdm6604/UI-result.json`。第一轮过早按失败终态修改输入，未等待原执行器完整后续清理，现场留下已有SQL提交证明的case-events文件事务；原报告没有该次409详细原因，不能事后精确归因。第二轮把该未完成事务复制到另一根目录，其清单仍绑定原根，明确返回file_recovery_required；属于验收复制方法错误，不能当作产品恢复失败。两原报告均保持false，API退出均核验。修正入口等待真实JobProcess.wait，并在复制前拒绝未完成的根目录绑定事务；随后完整UI运行通过。漏掉completed后缀、刷新后未先选算例的定位超时也保留为驱动问题；不能据此宣称N6-06的当前算例/尝试刷新恢复已实现。

## 仍须交付

当前两套SQL竞争检查在实际原生准备完成后保持响应，再制造取消/文件变化/API退出；不声称本轮覆盖准备工具仍在运行时的全部猝死窗口。历史无完整证明的记录保持人工核查限制，工作流原输入恢复仍需原流程用户操作衔接。人工版本选择/隔离、未完成上传和其余Jobs恢复继续N5-07原四条条件，N5-07不据本子阶段关闭；完整跨环境和故障矩阵仍由N7验收，不能因本轮同机双API通过而提前销项。
