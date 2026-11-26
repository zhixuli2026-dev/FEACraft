# N4-03.e：PTY 会话的持久归属与退出核验

N4-03.h：复现并修复API退出后WSL只读身份探测残留；身份/ancestry改用所属Windows范围、固定Linux读程序、10秒原生期限及管道退出。专项11（取消0.328秒/API猝死0.671秒/期限10.532秒）、登记双库各20、PTY双库各44、CLI/基础兼容5/5通过（实际Linux CLI10、准备9、传输7）。不创建业务Job，不改变原持久登记后放行；53/295、11完成/16实施中/26待实施和head3d1c98e2b754/35表不变。 详见[所属只读身份探测](owned-control-readers.md)。

最终验证：PTY SQLite/临时PG各44项，S3可控适配各9项，迁移各53约束及历史降级保护，启动门8/树10、原请求各14、runtime各25、core14/14、两套旧终端兼容（12个PTY后真实LaTeX）与前端构建通过。780文件基线及来源见tmp/mainline-N4-pty-evidence。

日期：2026-11-03。正式 WebSocket 终端已接入独立 NativeSession，不创建求解 Job。完整目标仍为 53 项主线和 295 项功能；viz、Agent／CLI、专业入口、统一预算／调度／停机和恢复继续实施，不能因本单元验收而关闭 N4-03 整项。

## 启动与交互

`terminal_ws → TerminalSession → NativeSession → OwnedTerminal → OwnedProcess → terminal_bridge → terminal_pty_gate → cmd／bash`。

先在短 case 写协调中选择源目录；S3 路径同时准备本次独占 staging，随后提交并释放连接。终端在独立短事务中提交 starting owner，再创建所属进程根，独立核验实际出生身份，提交根记录后才启动交互桥。桥只创建一个等待放行的 PTY gate：cmd 使用 ConPTY，WSL 使用私有 PID namespace 内的 Linux PTY。

Windows gate 用真实解释器创建，避免 venv python.exe 的转发 PID；API 保留其查询／退出句柄，确认它确实属于本次 Windows Job。WSL gate 的握手 PID 是私有 namespace 内局部值，独立只读探针从已登记根遍历后代，结合 namespace、NSpid／NSpgid／NSsid、启动 ticks、boot 和 uid 映射成外部可核验身份。不能把局部 PID 直接当作系统 PID，也不能用一条真实但不属于本次根的外部身份冒充 gate。

gate 和选定 profile／源码摘要提交成功后，再重新验证用户、case 所有权及可写状态，最后放行命令解释器。输入、窗口调整和输出继续使用原 WebSocket 格式。数据先编码为有界 terminal_io 帧，再通过所属根的控制 stdin 分流到独立数据管道；release／stop 控制不会与用户终端输入混读。控制读取和数据写入使用独立线程及有界队列，写满数据管道不会阻塞停止控制读取。普通批处理根的 terminal_io 默认关闭。

输入权限检查后只进入会话自己的有界队列，最多保留 2 MiB／1024 项待处理输入，不在 case 事务中等待 PTY 消费。逐块确认让发送方按实际 PTY 写入进度推进；确认不是命令执行成功的凭据。窗口调整合并到最新尺寸，与输入串行确认，连续 resize 不会冲垮控制队列。超限返回 terminal_input_limit，会话继续运行；接收循环不被慢输入占住，断线仍能及时解除订阅，显式关闭仍可停止不读取输入的程序。

初版在真实 WSL 沙箱 API 中暴露了动态 localhost 端口转发不稳定；最终实现用管道传输 API 与交互桥的数据，不依赖跨 Windows／WSL 的动态端口转发。桥与等待 gate 之间的短期本机认证通道只用于出生握手及一次 release，不是终端输出通道，凭据不写入数据库或回放内容。

## 会话、权限和退出

WebSocket 断开保留会话；携同一 session ID 重连复用原根及 gate，先回放缓存，不创建第二个终端。普通 cmd／WSL 可以只绑定用户、case 为空；WSL 沙箱必须绑定 case，并继续原有 namespace、nobody、私有 case 副本及退出同步行为。终端保留 case 所有者权限，共享 viewer 不获得 shell。

显式关闭、空闲回收、真实命令退出、启动取消和心跳失败都收敛到同一关闭任务；重复关闭不再发送第二套停止操作。关闭等待整个 Windows Job／WSL namespace 及实际 PTY gate 退出，包含命令派生的脱离控制台／新 session 子进程。使用保留的 Job、handle 和 pidfd；不调用 pywinpty.terminate 的裸 PID 信号路径。工具根退出、PTY EOF、注册表为空都不能替代退出证据。

停止证据和请求终态保存成功后才发送正常 exit。实际命令返回 7 时，WebSocket 和持久记录均为 7；显式强制关闭但没有取得命令退出码时，保留 NULL，界面显示未知。原生范围已核验退出、S3 收尾尚未提交时，列表 alive=false、finalizing=true，持久终态和正常 exit 继续等待，不能把会话收尾中误报为进程仍存活。登记、停止核验、输出或终态提交失败通过 error 返回；界面显示该结构化错误。停止未核验不能成为成功 DELETE；终态提交失败保留原活跃记录、空结束时间和缺失回执，不凭内存清空补造完成。

case 级取消现在会等待终端的异步关闭回调。成功后才移除其监督条目；失败条目保持可见。其他作业／Agent 的完整取消与通知矩阵仍由 N4-04／N4-07 继续完成。

## 持久记录与重启

迁移 `b53a10c4f9d6` 前置 `ad42f9b3e8c5`，新增 native_sessions，代码共 24 张业务表。保存发起用户、可选 case、shell、owner 身份／token 摘要／nonce、固定 profile、root／PTY gate 实际出生身份、心跳／租约、停止回执、命令退出码、错误类别和结束时间。表约束限定 shell、沙箱 case、starting／active／closed／failed、非负版本和终态时间。

记录变更通过 owner／token 摘要／nonce／活跃状态条件 UPDATE 串行化。租约 30 秒，心跳间隔 5 秒；登记前后均复核租约。原始 token、短期 gate 密钥不进入公有响应。读取会校验 journal 的范围、profile、源码摘要及停止 nonce；损坏记录投影 evidence_invalid，不返回可信清理结论。

`GET /api/v1/terminal/sessions` 保留活动会话列表，增加 phase、cleanup_verified 和 owner_available；`include_closed=true` 可查看持久历史。新增 `GET /api/v1/terminal/sessions/{id}` 返回本用户可访问的持久证据。正常关闭的历史 ID 可只读回放最后 256 KiB 尾部，不重新创建或执行进程；用户显式关闭后默认列表不再重新出现该会话。

API 猝死时由所属 OS 范围清理进程。新 API 对原活跃记录只读投影；租约过期返回 recovery_required，alive 为未知，重连返回结构化错误和 4409，不自动另建或重放命令，也不补造停止回执／结束时间。崩溃前尚未提交的终端输出不假装已经持久保存。历史保留、缺回执恢复、跨实例路由、S3 同步失败后的显式恢复、统一停机及完整资源配额仍属 N5／N4 后续项。

## S3 收尾与边界

原生范围退出核验完成后，S3 staging 在重新取得 case 写协调、确认用户与来源仍有效后才进行冲突预检和同步。同步成功且 case 事务提交后才清理本次 staging。远端被更新、case 已不可写或同步失败时，保留远端胜出内容和本次 staging／manifest，保存具体错误类别；不能因原生进程已退出而宣称全部收尾成功。复制完成但协程尚未接到 staging 返回值时取消，以及 case 选择事务提交失败，都会清理尚未绑定 owner 的本次副本；分配结果在线程内捕获，不靠取消后的局部变量猜测是否创建过目录。

收尾同时核对 case 当前前缀仍与选择的来源相同；重新绑定后拒绝上传旧副本。清理前写入 terminal-cleanup 标记，成功同步后的标记记录 source_synchronized=true；共享 staging 同步函数见到该标记立即拒绝再上传，避免把部分清理后的文件缺失当作用户删除。目录实际删除后才移除 manifest／标记；临时目录锁进行有界重试，仍失败则保存 TerminalStagingCleanupError。该类记录只能继续清理，不能当作未同步副本重传。标记表达清理意图，不能替代原生退出回执。

交互帧上限 128 KiB，单个原生输入块解码后最多 16 KiB，会话排队输入上限 2 MiB，管道队列及缓存有界；终态保存最多 256 KiB 尾部。整次 IO／内存／磁盘／并发配额、公平排队、日志保留和完整部署安全边界继续后续交付，不由这些局部上限替代。

## 验证来源

[terminal_execution_smoke.py](../server/terminal_execution_smoke.py) 使用真实 cmd、WSL 和 WSL 沙箱、正式 API、SQLite／命名临时 PostgreSQL。验证真实文件写入和 stty 窗口大小、同一出生身份重连、退出码 7、全局用户范围、放行前无命令解释器、并发 case 写入、启动取消、错误 root／gate／真实外部身份、profile／登记／release／租约失败、空闲／心跳收尾、未核验停止、终态提交失败、损坏 journal、跨用户隔离、case 取消、实际脱离子进程关闭与 owner 猝死、重启回放及不重放未知会话。外部 Windows／Linux 哨兵的真实出生身份全程保持匹配。

[terminal_staging_smoke.py](../server/terminal_staging_smoke.py) 用真实终端与可控 S3 适配层验证同步成功清理、远端冲突、case 不可写／来源重绑、原生已退出但同步待提交、临时及持续目录锁、清理副本禁止重传、复制完成后取消及选择提交失败；没有据此宣称真实远端 S3 服务验收。已有多会话和线程池隔离脚本继续通过隔离 API 验证，保留旧默认线程池规模的 12 个 PTY 后仍可编译真实 LaTeX 的断言（脚本上限 32）。共享启动门、进程树、请求基础、runtime 能力、core 和前端构建结果以本批 `tmp/mainline-N4-pty-evidence/` 通过记录为准；开发库没有迁移或重载。

实现：[路由](../server/app/routers/terminal.py)、[会话生命周期](../server/app/services/terminal_sessions.py)、[持久台账](../server/app/services/terminal_records.py)、[所属交互传输](../server/app/services/owned_terminal.py)、[交互桥](../server/app/services/terminal_bridge.py)、[PTY gate](../server/app/services/terminal_pty_gate.py)、[原生身份映射](../server/app/services/terminal_identity_probe.py)、[迁移](../server/migrations/versions/b53a10c4f9d6_native_terminal_sessions.py)。

## Linux PTY 内核额度

Linux 普通终端可以配置单会话累计 CPU 与内存额度。API 先核对私有 cgroup v1 控制器，冻结当前额度并登记 PID 命名空间，再经原有 PTY gate 放行交互 Shell。终端及其子进程累计计量。达到 CPU 或内存额度时，WebSocket 返回 `terminal_resource_limit` 和相应原因，历史保留验证过的退出及用量 `stop.linux_compute_usage`。 [部署配置](deployment.md#可视化会话内核额度) · [实测入口](../server/linux_terminal_compute_acceptance.py)

2026-11-26 在本机 Docker Linux、私有 cgroup v1 与实际 PostgreSQL 上验收。通过正式 WebSocket 输入命令，实测两个后台 Python 子进程加入同一终端的 CPU 计量组。累计 **1.023 秒**触发 1 秒额度，整个进程范围经核实为空。同一 API 上的另一账户仍能执行命令，窗口调整后 `stty` 返回 29 行、93 列，主动关闭后对端 API 读回一致的用量回执。其他账户访问负载会话返回 404。 [最终回执](../../tmp/j16-pty-compute-m05we_sf/result.json)

在 64 MiB 额度内执行 160 MiB 分配，内核记录一次 OOM，峰值 67,108,864 字节，终端收到 `memory_limit`。默认配置完成实际输入和正常关闭。无控制器挂载权限的实例返回 `compute_unverified`，会话记录为零。四条退出回执均核验通过，临时控制组与 PTY 进程归零，原 11 个 Docker 服务标识一致。模型调用 0 次。
