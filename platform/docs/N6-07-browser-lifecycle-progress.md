# N6-07完整浏览器入口与生命周期

当前进展（2026-11-12退出核对）：Windows、沙箱和普通WSL运行中重启恢复及原四条remaining已逐项核对，七项原浏览器条件满足，见[N6-07原条件退出核对](N6-07-original-closure-audit.md)。主线45/53、剩8；以下各增量保留其发生时的阶段结论和失败记录，不改写为当时已完成。N7全阶段故障/部署矩阵及295项全功能目标继续有效。

2026-11-12开始。主线44/53、剩9项；全部53项和295项原目标继续有效。原N6-07为“合并单作业/批量/工作流入口，补完整浏览器流程”，验收原文为“启动、确认、取消、重试、重启恢复、查看/下载结果可在页面完成”。台账原四条remaining继续保留。

固定三个验收组：

1. 生命周期缺陷：定位并修复实际PTY关闭SQLite锁/持久行仍active，以及runtime.python_probe原生通道关闭后的未收集异常；实际进程/停止证明和原持久记录匹配，独立恢复不能伪造退出或重复执行。
2. 入口合并：单作业、批量、工作流使用清楚且一致的浏览器启动/确认/取消/重试/恢复入口，保留各自原执行语义、所属case/用户、幂等和历史，错误与运行状态可见。
3. 完整浏览器路径：页面完成启动至结果查看/下载，覆盖刷新/重启、权限、失败和桌面/375px默认布局；完整移动工作台不能用手动放大Dock或孤立组件截图替代。涉及Agent推理/工具选择/流式输出仅用授权真实DeepSeek，其他真实API/原生及协议故障单列。

当前首要证据：`tmp/case-workflow-controls-wxeox1fx/controls-result.json`和`owned-pty-after-exit-verification.json`记录真实PTY关闭数据库锁及进程已退出但行未完成；`tmp/case-workflow-controls-80ysbjpl/api.log`再次记录实际探测to_thread任务未收集的ChildExecutionError。日志可能含隔离认证URL，检查时脱敏，不输出密钥或原始认证URL。

初审发现RequestExecutor.run_sync/run_finalizer在取消后的close出错或再次取消时可能跳过operation排空。该风险尚未据源码判断宣称修复，下一步用实际原生探测及受控重复取消验证。PTY锁仍须独立定位，完整入口和移动验收尚未开始，N6-07未完成。

## 第一组增量：实际原生请求取消与异常收集

实际入口`platform/server/native_request_cancel_acceptance.py`及完整实际辅助代码在执行前经哈希审查。没有导入/运行历史推理套件，也没有改写原生输出、替换命令或生成停止证明。

基线`tmp/native-request-cancel-98ahwatf/native-cancel-result.json`保留为失败：真实Python探测请求分配原生进程后，在实际准备和关闭边界暂停；显式关闭后OSError故障使原to_thread的真实ChildExecutionError成为未收集异常。双重取消和正常探测的基线分别通过，未声称所有取消都复现。三种场景的实际子进程停止和API退出均核验。

修复在RequestExecutor共用的close-and-drain中以finally保证原操作始终被收集，关闭再次取消或报错也不能跳过。修复后`tmp/native-request-cancel-6u10yaz_/native-cancel-result.json`三种实际场景均通过、未收集异常为0、所有子进程和API退出核验。正常场景读取真实解释器与包探测结果；故障场景使用真实原生准备/关闭及真实通道错误，不伪造成功结果。该增量没有新增模型调用，模拟LLM0，不能当作模型验收。

审查分别为`tmp/N6-native-cancel-review-v1-20261112.json`和`v2-20261112.json`。真实浏览器并发/断开下的复验、其他调用者包括异步Job finalizer的完整回归及PTY问题仍继续，第一验收组未整体关闭。

PTY只读核对：`wxeox1fx`实际cleanup为30秒观察超时，未完成的终端为无case绑定的active行；`ocu6u8b_`两个无case终端则已持久failed/OperationalError且停止验证为true。API日志中的case_event_counters锁堆栈来自工作流依赖或批次读取，不能据此断言它们就是持锁者或PTY报错根因。下一步在实际并发页面/终端上记录脱敏事务持有区间与失败来源，不放宽停止/所有权门槛。

## 第二增量：真实写锁反例与关闭结果接续

实际浏览器/PTY事务观测位于`tmp/case-workflow-controls-8lg5o8m6/transaction-trace.jsonl`。自然页面并发的12个依赖、树和索引请求均返回200；已观测最长写事务约1141ms，来源为`websocket /ws/events`。这一轮没有自然复现30秒写锁，不能据此认定旧故障的持锁者或原始根因。随后显式持有真实SQLite空写事务：原PTY物理停止已核验，但完成UPDATE等待31687ms后SQLITE_BUSY；首次DELETE在36563ms后502，数据库仍未保存停止结果，释放锁后仅重提同一份完成结果成功，物理关闭只执行一次。`PTY-SQL-counterexample-v1.json`明确保留passed=false；宿主的passed仅表示观测和清理完成，不表示产品问题通过。

生产实现现在在原物理停止、必要导出/同步之后、SQL提交之前，把原所有者能力和确切关闭载荷保存到私有加密文件。数据库提交失败仍保留该文件；原API确切出生身份已退出时，另一API可以通过原DELETE入口提交同一份受所有者约束的结果。恢复不启动或停止进程，不重放命令，不重做导出。数据库已提交的确切结果可幂等确认；文件保存或确认失败不会否定已经成功的SQL关闭。私有文件使用稳定配置的JWT密钥派生加密，存在待提交结果期间须保持原密钥和私有审计目录；这不是跨主机或整机掉电持久性的验收。

本地关闭增加可选`?wait=false`，31ms返回202；原默认等待语义继续保留。查询把`completion_pending`、`completion_retained`、本地`actual_stop_observed`和`close_error`分别暴露，数据库权威字段`cleanup_verified`不会因观察到文件或内存停止而被改成true。页面仍未接入这些新状态；异步关闭只对本地可控制会话立即接受，非本地关闭结果重提仍等待提交。

新入口`platform/server/terminal_completion_acceptance.py`及完整辅助代码在执行前审查并绑定982份源码摘要。首轮`tmp/terminal-completion-qgl8ryp5/terminal-completion-result.json`失败在测试端OSC解析，命令实际已执行；失败原件保留，两API与实际终端停止均核验。修正非贪婪OSC解析和CMD命令分隔空白后，`tmp/terminal-completion-8rzitfh3/terminal-completion-result.json`通过：

- 两个真实API共享隔离数据库和稳定密钥；实际cmd PTY执行一次命令并留下唯一文件行。真实写锁下，停止已发生而数据库仍active/无停止记录，私有加密结果已保留。
- 原API仍活着时拒绝接续，另一账户404，实际篡改加密文件409；恢复文件原字节后，在原API确切退出且锁释放后，另一API提交原root/stop/tail，命令执行数仍为1，原物理close调用数为1，文件确认后移除。
- 私有保存目录实际被文件阻塞时，另一真实PTY仍按真实停止结果完成SQL关闭，不把保存文件不可用误报成进程未退出。两个PTY停止及两个API退出均独立核验。

审查为`tmp/N6-terminal-completion-review-v1-20261112.json`和`v2`。本增量真实模型0、模拟LLM0，是实际基础设施验收。原N6-07四条remaining保留，第一组整体未关闭：完整浏览器/并发原生取消复验、页面关闭状态与重试、入口合并、完整375px工作台及全部原浏览器路径仍待验收。WSL/sandbox导出、其他存储/数据库及跨主机的完整矩阵尚未据本轮通过。主线仍44/53、剩9项。

## 第三增量：终端可见状态、重连和移动工作台

页面现在区分启动、运行、正在关闭、实际进程已停但关闭结果尚未保存、保存失败和停止已核验。关闭使用可选异步接口，失败显示原结果重试；最后一个标签关闭后保留空状态，只有明确新建才创建PTY。新建、选择已有终端和WebSocket连接请求分开，首次ready不会再为更新标签编号而重连。会话列表、当前选择和输出绑定账户/算例，旧响应及旧连接回调不能覆盖新范围。新增明确的“重新连接原会话”；存储占用拒绝保持可见，不通过新建替代重连。

CLI启动命令改为只属于当次明确的新建请求，首次ready至多发送一次；普通新建和已有会话重连不继承残留命令。本轮没有执行CLI模型回合，因此不以这一代码修正冒充真实CLI推理验收。

原375px工作台把三个横向分组压在一起、底部日志高度不足。现在760px及以下显示完整的当前分组，由原生选择框切换工作面板；放宽窗口后恢复原分组。选择器保留13个真实面板入口，实际操作了项目/算例、终端、作业和设置，其余入口仍须完整路径验收。当前源码实测375px终端正文238px、768px259px、1280px311px；375px实际Job20的日志滚动区314.42px，读取原报告日志，未重跑报告、未手动调整Dock。深浅色按钮文字对比度13.40/17.74，字号14px、高44px；实际键盘Tab的焦点线为2px。impeccable检测执行一次、发现0项；定向lint退出0但保留10条React/ref/effect警告，不能称无警告。

窗口尺寸只在变化时提交；后端保留原权限、case和存储事务检查，仅回报操作种类、短暂占用是否可重试及事务成功后的尺寸入队确认。前端至多自动重试6次幂等尺寸控制，不重发命令输入。实际SQLite写锁下，V2收到retryable resize拒绝；释放锁后收到新的尺寸接受帧并清除提示。此接受只表示控制已入队，不伪称原生应用完成重绘。

### 实际记录与失败

- 修改前实际图及停止记录：`tmp/N6-terminal-incumbent-desktop-20261112.png`、`tmp/case-workflow-controls-7xd6_pys/controls-result.json`。仅为原界面参考，该宿主通过不代表N6-07完成。
- V1：`tmp/case-workflow-controls-t7ru60cr/terminal-browser-increment-v1.json`，passed=false。实际cmd页面关闭51ms返回202；真实写锁使SQL保存失败，页面明确点击“重试保存关闭结果”成功，物理关闭1次、结果提交2次，命令文件1行；WSL实际命令和同会话重连有证据。375px正文仅10px以及沙箱导出失败均保留。
- V2：`tmp/case-workflow-controls-wh9lwkqv/terminal-mobile-increment-v2.json`，passed=false。移动布局、实际Job20日志、真实resize拒绝/释放后接受，以及cmd手动原结果重试有证据；首次快速创建和切回已有会话被存储占用拒绝，缺少明确重连操作导致完整路径未通过。命令文件保持1行。部分Python探测在实际锁故障中failed/PendingRollbackError，停止已核验；没有未收集异常标记。
- V3当前源码：`tmp/case-workflow-controls-l9481ip3/terminal-browser-increment-v3.json`，passed=false。真实写锁拒绝首次创建，SQL终端数量未增加，页面错误可见；释放后明确新建WSL、执行一次命令。冷刷新连接同一个PTY、没有新输入帧、文件仍1行；真实HTTP200原响应延迟至切换算例后放行，旧会话未显示在新算例。375/768/1280、深浅色与键盘结果见上述数值。最终关闭34ms返回202，进程实际已停而SQL仍active/cleanup_verified=null，加密结果已保留；第一次保存31735ms后OperationalError。手动按钮观察未在40秒窗口内获得，随后原后台回收器自动重提相同载荷，最终closed/停止已核验，物理关闭1次、提交2次、命令文件1行。不能把这轮自动接续说成手动按钮验收。详情见`actual-terminal-close-trace-v3.json`。

沙箱失败为V1实际会话`14ec3020-d031-4157-8096-30486d41f1ea`，导出请求`a006b0e3-baac-4592-a38c-0b041d9b2b73`的真实子执行以125退出，terminal.export及会话持久failed/ChildExecutionError，实际停止已核验。私有diagnostic.txt为0字节，尚无充分根因证据；保留128MiB卷和原settings，不将普通WSL成功代替沙箱验收。

四个宿主的API/preview退出、终端停止、源数据库/索引及旧workflow/step/job/attempt历史保持均核验；没有新增求解作业。本轮原生能力探测有真实成功和故障记录，当前V3的已登记子执行停止均核验，另外3条failed/ChildExecutionError保留空children文档，不能凭空文档声称停止证明。各宿主日志均未发现未收集Task/Future/线程异常标记；这不等于全部探测成功。测试端的输出选择器、Fetch状态取值、延迟响应解除及主题控件选择错误都保留在对应driver和原始工具结果中；没有为观察超时重启原生命令。

审查为`tmp/N6-terminal-browser-baseline-review-v1-20261112.json`及`tmp/N6-terminal-browser-review-v1/v2/v3-20261112.json`；最终绑定983份源码与实际构建文件。模型0、模拟LLM0，本轮为真实浏览器/API/OS/数据库及故障测试。N6-07及三个验收组均未整体完成，原四条remaining保留：继续沙箱导出、最终源码手动关闭结果重试、原入口合并和完整启动/确认/取消/重试/重启/结果查看下载路径；仍需实际Agent模型回合及其余部署矩阵。主线44/53，剩9项；295项目标不变。

## 第四增量：沙箱复制等待与会话轮询

原沙箱失败继续保留。真实取证期间，原PTY gate仍存活并等待子bash，子bash等待`cp -a`复制Windows挂载算例；复制进程实际停在`p9_client_rpc`，15秒时`/tmp/case`尚不存在。最初怀疑unshare父进程根目录选择有误而实现的PID1子进程选择候选已完整撤回；`sandbox_volume.py`恢复原SHA256 `b170f1a6e2f0d56a88de5ff686b31207be259b833aa6dc1cfc37813dd2c5132b`。Linux的pivot_root会更新同一挂载命名空间内指向旧根的进程根目录，不能把父进程身份直接视为错误根目录证据。参见[pivot_root系统调用说明](https://www.man7.org/linux/man-pages/man2/pivot_root.2.html)。最终生产修改仅将`sandbox_export_native.pin()`等待由15秒调整至45秒，仍受原60秒启动期限约束，保留原权限、身份、文件描述符、卷、容量、停止和发布核验。

只调整等待、保留原卷实现的对照宿主`tmp/case-workflow-controls-2dj9l2r2`通过真实创建/关闭/导出及停止核验。随后`n0zjk7v7`在375px浏览器真实创建沙箱`9926b99e-7699-4a08-b026-b4788bcec4f2`，执行一次文件写入与权限检查，实际uid65534，隔离目录检查通过；关闭前及冷刷新重连后正式文件接口均404，未新建PTY。实际关闭554ms返回202，原进程范围已空且6个Windows成员退出已核验；发布回执后正式文件200且标记仅1行。此宿主沙箱发布子流程通过，但后加的手动结果重试观察超时，整体passed=false；没有改写旧失败。

该手动观察失败引出了独立页面缺陷：`reloadSessions()`每1.5秒启动请求，并以递增序号使旧请求作废。宿主`nanqwa71`真实创建无输入的后台cmd，将实际会话列表HTTP200原正文延迟2200ms；5个含真实会话的响应均未使页面显示它，并发读取峰值2；解除延迟后才显示。原PTY最终实际关闭、停止核验通过。该反例passed=false，见`poll-counterexample-before.json`。

当前前端对同账户/算例范围合并在途读取，慢响应完成前不再替换；切换范围或卸载用AbortController取消原读取，保留范围和序号核验，并复用原带认证的`sessionHistoryRequest()`。宿主`ob54pfr3`在相同真实2200ms延迟下显示会话，并发读取峰值1；原始响应正文未改动。另持有包含原无绑定会话的真实200响应，在页面切换算例时实际请求被`net::ERR_ABORTED`取消，旧标签未进入新算例；原PTY已实际关闭并核验停止。

同一当前源码下，真实cmd `a0807016-57d5-4767-87e3-3feda41870fc`页面关闭36ms返回202。实际SQLite写锁使首次保存31547ms后OperationalError；页面显示进程已停、结果待保存，持久cleanup_verified仍null且加密结果已保留。释放原锁并**实际点击**“重试保存关闭结果”后，最终closed/cleanup_verified=true。trace记录物理关闭1次、结果提交2次；本轮无命令输入或模型调用，没有重启PTY。此前V3及n0宿主的自动回收成功继续只作为自动接续证据，不更名为手动重试通过。

本轮新增10个隔离宿主均完成API/preview退出及停止核验；相对原停止源，完整workflow_runs/workflow_steps/jobs/job_attempts行一致。实际新建12个PTY，25条原生请求的25个已登记子执行停止核验通过；失败阶段和诊断命名空间误用均保留，不将全部探测称为成功。独立核对见`tmp/N6-sandbox-and-poll-independent-history-20261112.json`。最终构建通过，单文件lint退出0但保留8条React/ref/effect警告；本批UI检测一次、0项，已实际查看375px失败重试和关闭空状态截图。983份源码/构建审查见`tmp/N6-terminal-poll-review-v2-20261112.json`。

当前新增代码相对上一检查点为`sandbox_export_native.py`等待修复、`TerminalPanel.tsx`读取修复和无模型的`sandbox_export_trace_acceptance.py`真实诊断入口。本轮真实模型0、模拟LLM0；沙箱完整页面证据发生于轮询修改前，后续读取/手动重试证据发生于最终前端，不声称把所有路径在最终前端重跑。原N6-07四条remaining和三个验收组继续保留，下一步是原入口合并及完整启动/确认/取消/重试/服务重启/结果查看下载。主线仍44/53、余9；295整项目标不变。时间及后续汇报规则见[交付进度记录](delivery-progress-20261112.md)。

## 第五增量：统一提交、原回执恢复及真实工作流启动

作业面板增加统一的单作业、批量命令、工作流提交入口，先核对再确认。请求发送前保存账户/算例绑定的原请求与幂等键；响应丢失后可只读查询原结果，重试沿用原键和正文。新`execution-submission`接口校验原请求摘要、用户、算例及批次成员，不创建执行。批次详情连接成员日志与恢复入口。作业列表及详情合并在途轮询；取消有进行中和错误反馈。前端构建通过，尚未完成此批UI最终检测和完整权限/重启矩阵，代码未归档为最终检查点。

第一宿主`tmp/case-workflow-controls-3bid5g0s`保留整体失败：实际单作业22双击只提交一次，随后同一作业成功且停止核验通过；批次在第一项starting、第二项queued时取消，第一项只有created观察、没有停止/写入排空证明。两个持久状态虽已cancelled，同算例新提交仍409/recovery_required。原PID不再存在只是窄观察，不代替完整停止证明。宿主API/preview均已核验退出，源数据库和索引未改，作业23停止证明仍缺失，不能把此失败改成通过。

启动取消修复：JobProcess在等待门内完成实际身份准备后处理取消；直接取消交给原启动任务收尾，避免与prepare并发stop。原所有者在cancelling时可继续登记真实准备观察；prepared和业务释放仍拒绝取消任务。新入口`job_starting_cancel_acceptance.py`在真实created观察提交后暂停，使用实际取消接口，未替换命令、模型或停止结果。`tmp/job-starting-cancel-z75sz201`中WSL批次和Windows单作业均取消成功，用户载荷未执行，停止/排空记录完整，同一算例的后续真实命令成功；第二个排队成员未领取、无启动事件。整份宿主报告仍false，因为测试脚本切换测试账户后用后一账户清理前一账户作业，得到权限拒绝。原失败保留；`independent-durable-verification.json`只读核对4个已领取作业的真实停止/排空证据和1个未启动排队项，API退出已核验。测试脚本现已改成同账户创建第二算例，后续执行需新审查；没有把原报告改写为通过。

最终本轮源码浏览器宿主`tmp/case-workflow-controls-b761qae4`：

- 实际单作业22已接纳后丢弃真实HTTP响应，同时使原回执查询断网；刷新后原键/正文不变，实际提交仍1次。原观察脚本在刷新默认项目面板上点击隐藏作业面板，超时失败保留；实际选择作业面板后点击“读取原提交结果”，HTTP200返回原作业22，未发送新作业请求，原作业成功且停止核验通过。
- 新隔离算例6由真实API建立，条件写入真实CadQuery盒体脚本。保留初次创建409、存储配额结算占用409及旧页面未刷新算例列表的测试失败；没有改数据库或跳过写入门槛。统一入口核对期间无提交，确认后只创建流程`519dafbf-d308-4535-b546-82522f5b6746`。真实几何导出作业24成功，流程等待B阶段确认；页面跳到原流程的阶段入口，核对不提交，明确确认一次后A→B、输入修订0→1，原流程成功。原几何发布固定为`f31523a6-19b9-4302-aa9b-3b8cd0db5c6a`，未以旧报告流程替代新启动。
- 从文件树打开实际`A-geo/exports/result.step`，真实只读导入成功并显示盒体。原流程提交查询返回原ID，改动正文409/idempotency_conflict，新键查询found=false。结果图已查看；375px几何面板仍横向溢出，而且没有直接下载原STEP/STL的控件，二者明确待修。

此宿主的browser-finished只声明上述子流程通过，明确`N6_complete=false`；不覆盖整组、原四条remaining、服务重启、全权限/故障矩阵或Agent推理。宿主最终退出0，API/preview退出、实际作业22/23/24停止均已核验。独立只读检查确认原2个流程、20个步骤、21个作业及21个尝试的完整旧行未变；新增10条原生请求的全部已登记子执行停止证明通过，源数据库/索引未改。见`independent-history-and-native-stops.json`。审查为`tmp/N6-merged-entry-review-v1/v2-20261112.json`、`tmp/N6-starting-cancel-review-v1-20261112.json`，最终浏览器绑定990份源码/构建文件。真实模型0、模拟LLM0。下一步先补齐结果下载与几何移动布局，再完成最终统一批次页面取消及原完整浏览器路径；涉及Agent必须使用已授权真实DeepSeek。主线仍44/53、剩9；295项范围不变，时间估计未自动顺延。

## 第六增量：固定几何原件下载、移动布局与运行批次取消

几何页新增“已发布的STEP/STL”：读取当前发布后固定ID，显示版本及文件，重新核对沿用原版本；只有明确选择“读取最新发布”才重新解析当前发布。后端新results/artifact接口通过已有不可变发布解析和内容核验读取文件，限制在该发布的几何导出文件内，保留权限及版本/Range边界，不把当前工作区别名当原件。浏览器再校验大小与SHA-256；账户/算例变化会取消旧读取，迟到响应不能触发旧下载。

几何工具栏改为可换行、可滚动的有限高度区域，视角操作按需展开。小屏默认收起对象设置，展开时位于画布上方；画布允许缩小到实际容器宽度，避免原300px画布与侧栏相加导致横向溢出。保留原渲染、相机、材质、剖切和动画操作。新增按钮最小44px、键盘焦点2px，使用现有工作台颜色变量。

宿主`tmp/case-workflow-controls-yr0xkia9`复用上一宿主真实发布`f31523a6-19b9-4302-aa9b-3b8cd0db5c6a`：页面实际下载result.step（15363字节）和result.stl（684字节），独立本地字节检查与原发布清单SHA-256完全一致，没有重新导出几何。下载原响应HTTP200被暂缓后切换算例，迟到响应未触发下载、原结果未显示在新算例。实际接口HEAD200、16字节Range206、错误版本409、跨算例发布404、无认证401、非导出路径404。

Chromium模拟视口320/375/768/1280px下无几何面板横向溢出，主要按钮均44px。375px默认画布375×407，展开对象设置后375×228；真实键盘ArrowRight使金属度0.3→0.35。最初布局driver把程序focus误当键盘focus，整份driver保留passed=false；随后实际Tab到summary观察到focus-visible、2px solid焦点。截图已查看。物理手机触控、Safari和其他操作系统未验收，不以视口截图代替这些范围。

同一最终源码的统一批次页面创建实际WSL作业25/26，两者均进入running且有进程身份后再点击取消。取消HTTP200，两个原尝试均cancelled且真实停止verified=true；成员日志可进入，原批次只读回执返回相同ID。此项覆盖运行时页面取消；第五增量保留的启动/排队取消证据和旧失败仍分别记录。

前端构建通过。最初npm/npx的PowerShell入口受系统策略阻止；随后构建改用npm.cmd。误用ESLint入口因项目没有其配置退出2，未算通过；实际项目oxlint定向执行退出0，保留36条警告（React/ref/effect等），不是零警告。本批impeccable源文件检测一次、0项，不把检测代替实际截图与交互。992份源码/构建哈希审查为`tmp/N6-geometry-download-review-v1-20261112.json`。宿主退出0，API/preview及实际批次作业25/26停止核验通过；相对上一宿主的全部旧流程/步骤/作业/尝试行未变。第五、第六增量两个通过宿主合计新增14条原生请求，14个登记子执行停止证明均核验。独立证据为`tmp/N6-unified-geometry-independent-20261112.json`，它明确保留最初3bid5g0s缺少停止证明的反例，不声称全部历史失败已修复。真实模型0、模拟LLM0。当前仍需原服务重启恢复、权限/故障矩阵及授权真实DeepSeek Agent回合；N6-07未整体完成，主线44/53、剩9，全部295项原目标不变。
