# 独立请求和会话的启动登记恢复

## 后续会话关联与持久收尾（2026-11-10）

已有历史支持明确关联同账户、同算例中已存在的后续会话，保留原/新双向导航、绑定源快照的说明与独立关系凭证。关系不继承上下文、不授权新执行，也不生成停止证明。新增迁移`c20e87f143b9`仅增加`native_session_links`及约束/索引，父版本`b19d76e032a8`；开发库未迁移。双实际API的同键并发、跨账户/权限撤销、冲突和隔离PG升级/回退/再升级，以及真实浏览器提交后响应丢失回读、刷新与双向导航已有证据。PG迁移前只有旧schema/审计元数据，不宣称已覆盖N7混合原生历史全矩阵。

初版SQLite/browser独立清理复核失败：真实终端`b923dc30-16a9-4b86-9d37-30e7385c2eb9`仍是active且没有持久停止证明。初版脚本只按alive清理造成漏检，已纠正为检查当前API出生身份下每个持久记录的证明；修正后的独立运行`tmp/native-session-lineage-6grgh8hi/lineage-result.json`已通过，4个后端及2个页面生命周期PTY均有持久停止证明，两个API和预览进程均持有创建句柄确认退出。该次浏览器只验证创建/刷新生命周期及清理，没有验收刷新后的终端WebSocket重连；实际出现的“连接已断开”提示继续N6-06核验。原API、宿主和gate的退出观察不等于整个范围的停止证明；原行和原库保留，真实副本人工保留及未知控制DELETE409已验证。规范失败记录：`tmp/N5-native-lineage-post-runtime-cleanup-failure-20261110.json`；人工保留：`tmp/native-history-retained-vw8ukt4t/retained-result.json`。不得用后续通过覆盖该失败，精确持锁者及原缺证根因仍未确定。

另外确认独立产品缺口：`TerminalSession.close`原先永久缓存首次失败任务，数据库暂时写失败后无法再次持久收尾。现在只有原owner仍在内存且保留实际核验停止证明时，才允许重试冻结的原completion；重试只写原记录，不再控制进程、执行导出或同步文件，也不接受新一次close参数覆盖原证据。所有权token/nonce和原root/stop校验仍由正式持久服务执行，未知历史不会因该功能获得控制权限。

显式入口`native_terminal_close_retry_acceptance.py`经过入口、全部app源码和导入testsupport的版本哈希审查。实际ConPTY停止后，在真实SQLite BEGIN IMMEDIATE写锁下调用原complete得到SQLITE_BUSY；修复前再次DELETE502且持久记录仍无证明，修复后两个并发DELETE成功保存原root/stop，物理stop和finalize均仍仅一次，再次DELETE幂等。2个实际API均保留创建句柄确认退出。两个新PTY都有真实内存停止证明，修复后1个额外核对SQL持久证明；不声称修复前持久记录已补齐。新增真实模型0、模拟LLM0，此专项不执行模型推理。证据分别为`tmp/native-terminal-close-retry-w4k2soyb/close-retry-result.json`和`tmp/native-terminal-close-retry-y42ymw1q/close-retry-result.json`。它验证独立重试缺口，未证明历史b923由同一故障导致。

真实CLI/VTK界面验收：`tmp/native-history-actual-UI-u87051io/history-actual-result.json`。原API实际退出前分别保留活跃WSL CLI和已登记preflight、尚未登记root的真实初始化；新API只读取历史。通过编译后的界面，初始化按钮取得`initializer_not_released`凭证并结清占用，但原CLI完整停止仍显示未核验；另一CLI的范围收尾按钮取得实际管理范围退出/retirement凭证，原记录保持failed/OwnerExited，不生成成功模型结果。初始化会话的范围随后由正式控制器独立收尾。

后续会话由正式路由创建，在已安装WSL CLI内实际调用用户授权的`api.deepseek.com / deepseek-flash`，随机字符串严格匹配、completed终态和原助手消息落库均已核验；CLI原生usage报告10,556 tokens。只计1个真实CLI模型回合，不将它推断为1次HTTP请求；内部HTTP次数未逐次观察。浏览器将原CLI与该已存在后续会话关联，双向导航及刷新后同凭证保留，前后CLI ID集合仍仅3个。关系凭证`execution_authorized`、`stop_evidence_created`和`context_inherited`均为false。

可视化使用正式路由读取实际STL，启动真实VTK/trame渲染器。历史里的“打开已有可视化”经认证代理GET200，前后仍同一viz ID，实际浏览器图像显示3点/1单元三角形；截图`tmp/native-history-actual-UI-u87051io/actual-VTK-history.png`已查看。3条CLI会话记录最终停止核验、1个VTK及2个页面PTY持久停止证明均已确认；两个实际API和预览进程持有创建句柄确认退出。本组模拟LLM0。一次新夹具在登录前拼接空token失败，没有创建原生根或调用模型；原失败及另行确认原API确切退出记录保留在`tmp/native-history-actual-UI-olq_yu6_`，未改为通过。浏览器驱动曾误把会话下拉视为按钮、在折叠的关系组里等导航按钮；修正定位后只读取已提交关系，没有重发关联POST。

当前仍38/53、剩15，N5-07原四条范围保持。旧段的“待关联/待PG/待真实CLI-viz入口”是该段封存时状态；后续进展以本节的实际证据和限制为准。N7完整混合历史/部署/跨主机矩阵、N6刷新恢复和CLI所属对话完整导航继续原任务；输入恢复、人工版本选择/隔离及其余Jobs恢复仍待完整交付。

## 前一阶段已封存入口与审计（N5-07，2026-11-10）

当前主线38/53完成、剩15项（4进行中、11待开始），N5-07仍在进行中。以下早期启动矩阵保留为历史记录；其中受控CLI协议夹具不等同真实模型验收，禁止据此运行旧模拟LLM回归。全部53项与295项交付目标保持。

终端、原生CLI对话和可视化面板均有“会话历史与人工核查”折叠入口。可选择终端/CLI/可视化类型、当前算例或“我的全部会话”，查询关闭、失败、失联和无法核验的记录；分页沿实际创建时间与ID读取。未绑定算例的普通终端在全部范围中可见。显示的“当前服务未连接原执行者”不代表原执行主机已退出，“已关闭”也不自动等于停止已核验。读取历史不启动CLI/线程、发送模型消息或重开终端；已核验的终端可独立读取原有日志。

失联或损坏且无法由当前实例管理的原记录，可填写可选说明并“登记人工核查”。服务端重新核对账户、原创建者和算例读写权限，在账户/会话行锁内校验完整原记录快照。独立的`native_session_reviews`表保存原类型、ID、版本、快照摘要、操作者、说明和明确的`execution_authorized=false`、`stop_evidence_created=false`；原会话、租约、日志、停止和进程日志均不修改。表支持case为空，不以虚构算例补齐全局终端。相同快照和说明复用同一凭证，不同说明或过期快照返回冲突。此前快照的凭证继续保留；读取会严格检查字段形状，损坏的审计不能冒充有效凭证。

对于具备既有托管证据的CLI，会提供“核验并接续范围收尾”或“核验原初始化占用”。实际执行仍由原有控制器重查原owner退出、准确scope/guardian及已登记身份；本次查询本身不做物理控制。“范围收尾已返回”不代表会话执行锁或暂存写入已恢复，初始化占用回收也不是完整停止证明。未知、远端或损坏进程不能因登记人工核查获得控制权限。可视化历史仅在当前实例持有存活执行者时提供打开入口。

正式API：`GET /api/v1/native-sessions/{terminal|cli|viz}`，支持case_id、limit及绑定类型的游标；`GET /api/v1/native-sessions/{kind}/{id}`读取快照和凭证；`POST /api/v1/native-sessions/{kind}/{id}:retain-for-review`提交固定action、snapshot_sha256及可选note。凭证与原记录的物理停止、发布和新执行许可相互独立。

新增Alembic revision `b19d76e032a8`，父版本`a08c65df2197`，只增加审计表及索引。已有数据库需在部署时执行经审查的升级；本轮未对开发数据库运行迁移。隔离旧历史库已实际stamp到其原结构版本，执行升级→回退→再升级，原三类会话摘要保持。回退测试时新表为空；没有据此宣称带审计数据回退可以保留新表内容。PostgreSQL迁移和并发验证仍待完成。

实际后端验收：`tmp/native-session-review-tepu2w8d/review-result.json`。创建真实ConPTY、核验关闭，并读取原真实关闭历史。三类损坏JSON元数据故障分别返回“无法核验/停止未知”，独立登记、重复登记同凭证、说明冲突、旧快照拒绝、全局终端、分页及跨账户404通过，原会话行摘要不变。CLI和可视化故障行没有启动任何CLI/渲染器，也没有模拟其推理结果，不能计为原生CLI或可视化执行验收。

实际浏览器：`tmp/job-recovery-history-UI-xdk3vv4d/UI-result.json`。从上述终态库复制到独立UI库，保留3条旧凭证并新增3条明确的损坏元数据故障。通过编译后的正式界面，三类故障各登记一条HTTP200凭证（包含全局终端）；刷新后同一凭证可见，登记按钮不再出现。历史/日志操作前后所有类型会话ID集合一致，真实保留日志GET200并展示373字符。1440px/390px、浅暗主题、至少44px目标、可见键盘焦点、最低约6:1对比度及实际DockView拖动后的日志操作通过。首次终端标签定位误选下拉option、随后匹配到两个面板的历史区域，均属于浏览器驱动定位错误；修正为实际可见标签及打开的区域后取证，没有因定位错误重发业务操作。整个工作台移动布局仍属于N6范围，不以本段窄屏组件检查替代。

两组共2个实际API与1个预览进程有持有创建句柄的正向退出证据；新建真实PTY共3个，其中后端验收1个、浏览器既有面板生命周期2个，均核验关闭。上述3个不包含源库旧PTY，也不包含3类损坏元数据故障。新增真实模型0、模拟LLM0。退出后读取6条实际持久凭证，再对6种损坏凭证结构做纯协议拒绝检查：`tmp/N5-native-session-receipt-readonly-audit-20261110.json`。最终收紧凭证形状的源码与运行时原版分别保存，不将只读检查冒称新原生执行。

规范索引：`tmp/mainline-N5-07-native-session-history-evidence/validation.json`。本段仍未完成新旧会话关联、真实CLI范围/初始化和真实可视化在新界面的操作、PostgreSQL/跨API竞争及更完整的权限矩阵。N5-07原四条条件、其余Jobs恢复、输入恢复与人工版本选择/隔离/未完成上传保持，不将历史列表和人工登记计作整项完成。

## 早期启动登记证据

状态：本批8种配置的隔离验收完成。N5-03 仍进行中；主线 11 完成、19 进行中、23 待开始，剩余 42 项，全功能 295 项仍按完整交付条件逐项验收。

本批核对“进程已经创建，但登记尚未完成”时的持久状态和重启行为。已有实现先保存 owner、启动 nonce 和选择配置，再创建进程。新 API 无法接管原实例的内存控制对象；租约过期后，保留原身份和未知清理状态，显示 `recovery_required`。登记已提交但调用方收到异常时，原持有者应保存实际停止回执。

| 类型 | 持久记录与实现 | 本批运行环境 | 重启后的读取和控制核对 |
| --- | --- | --- | --- |
| 独立准备请求 | `native_request_records` / `native_request_execution` | Windows Pandoc、WSL LaTeX | 通过隔离路由读取实际 `public()` 投影并调用真实 ownership fence；不是新增正式通用历史 API |
| 交互终端 | `terminal_records` / `terminal_sessions` | cmd、WSL、WSL sandbox | 正式详情 API、原会话 WebSocket 重连，验证失去 owner 时拒绝控制 |
| 可视化 | `viz_records` / `viz_sessions` | Windows | 正式详情和关闭 API，验证不可控历史不会触发第二个渲染进程 |
| CLI | `codex_records` / `codex_sessions` | Windows、非 root WSL | 正式详情和 account API，验证历史会话不启动新的 CLI 或线程 |

验收脚本为 [session_startup_recovery_smoke.py](../server/session_startup_recovery_smoke.py)。每个配置检查三个边界：创建进程后真实终止 API；宿主或完整 WSL 握手后真实终止 API；实际登记提交完成后注入确认丢失。前两类保留原持久阶段、身份记录和未知停止结果，后一类要求原持有者实际清理完成，且未放行待执行载荷。

死亡场景先让第二个 API 读取仍存活 owner 的记录，再通过测试自己持有的 API 进程句柄终止原实例。测试持有只读 Windows 查询句柄核对实际出生身份与退出，避免 PID 复用干扰证据。WSL 原生 helper 另行只读核对。随后仅在隔离数据库中把租约期限移到过去以验证过期投影；这一步是明确的测试时钟注入，不用作实际进程退出证明。对旧会话的控制请求、重复读取和跨用户访问不得创建新记录、启动载荷或伪造停止回执。

这批测试包含真实启动门、原生进程、持久事务、两实例读写和正式会话路由。CLI 载荷使用既有受控协议夹具，未使用商业账户或正式 OpenAI 联调。NativeRequest 的通用历史展示和人工处理入口属于后续 N5-07；Agent checkpoint 与工具副作用恢复属于 N5-06，本批不把底层启动记录验收扩张为工作流恢复验收。

N5-03 的前置 N5-02 尚未整体验收，因此即使本批矩阵通过，也只记录对应实现覆盖和证据，不修改依赖关系或提前标记完成。下一步按 N5-01/N5-02 的所有者、租约及逐项对账条件核对剩余工作，再推进 N5-04 的收尾幂等恢复。上一归档已复现的报告 finalizer 停止回执持久化失败继续保留在该范围内。

当前运行与原始失败记录见 `tmp/session-startup-recovery-pending.json`；验收结束后记录双库结果、测试源码/产品源码绑定和归档位置。开发数据库与端口 8000 不用于本批测试。


本批结果：N5-03独立请求和会话启动窗口已补齐验收：Windows/WSL准备请求、cmd/WSL/sandbox终端、Windows可视化、Windows/非root WSL CLI共8配置，真实创建/握手后API退出与登记提交确认丢失，SQLite/PostgreSQL各24项通过。原有产品实现满足本批条件；未知停止保留需处理状态，原持有者实际清理才保存停止证明。N5-03仍受N5-02前置整体验收约束；主线11完成/19进行中/23待开始，剩42，全功能295项严格验收仍0。正式SIWC联调继续暂缓。

规范证据：`tmp/mainline-N5-session-startup-evidence/`。首批Windows/SQLite12项与最终双库各24项重叠，不累计计数。源码白名单950文件、Python569文件；app248、路由227、边界350不变；注册套件all276/offline104/writes30。未运行全量offline/writes/前端回归；未恢复新归档后重跑。head6a4fc1b5e087、36业务表不变。
