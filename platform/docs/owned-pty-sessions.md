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

## 普通 Linux 终端隔离

普通 Linux 终端在已登记的 PTY gate 放行后，用专用文件系统、PID 和网络命名空间启动交互 Bash。拥有算例权限时挂载该算例目录或该会话独占的 S3 暂存副本。临时配置下，全局终端从自己的 `/tmp` 启动，Shell 的 HOME 和历史也在该临时目录。运行时目录只读，进程没有 Linux capabilities，网络命名空间没有外部接口。会话回执通过 `linux_launcher_sha256` 记录选定的启动器版本。 [启动器](../server/app/services/agent_shell_linux.py) · [PTY gate](../server/app/services/terminal_pty_gate.py)

Docker 中用两个真实账户和 PostgreSQL 验证了算例读写、跨账户文件不可见、跨账户会话详情返回 404、宿主临时文件不可见，以及对 Docker 网络内实际可达的测试 Redis 连接被拒。前台命令经实际 Ctrl+C 返回 130，跨实例重连保留同一会话及临时文件，窗口调整后 `stty` 返回 34 行、107 列。全局终端退出码 7 同时出现在 WebSocket 与持久回执。CPU 累计 1.012 秒触发额度，64 MiB 限制触发一次内核 OOM。 [文件系统回执](../../tmp/j16-pty-compute-q0ws5a1p/result.json)

认证 SeaweedFS 另在 UID 65534 的两个 API 上验收。算例写回后，空缓存实例读到了相同字节和一致的停止回执，暂存目录清空。两种存储的终端测试都通过实际 WebSocket 执行命令，模型调用 0 次。API 作为 root 运行时，沙箱映射该宿主 UID，所有入口强制使用非特权宿主 UID 及统一常驻环境仍属 FF-J16。 [认证 S3 回执](../../tmp/j16-pty-compute-y4yizgt7/result.json) · [实测入口](../server/linux_terminal_compute_acceptance.py)

Docker 实测复用本机镜像，原 11 个运行服务的容器标识均已核对。构建缓存为 5.857 GB，本轮回收 0 B。Windows C 盘可用空间由 39,740,411,904 B 变为 39,756,001,280 B。

## Linux 账户 HOME 持久化

配置 `VIBEFLOW_LINUX_SHELL_STATE_ROOT` 后，普通 Linux 终端和 Agent Shell 按已认证账户选择同一个目录，挂载为 `/home/user`。无算例终端从 HOME 启动，算例终端和 Agent Shell 仍从授权算例或其 S3 暂存目录启动。终端历史和 `/tmp` 仍按会话隔离。空配置沿用隔离的临时环境。 [账户目录选择](../server/app/services/linux_shell_home.py) · [Linux 启动器](../server/app/services/agent_shell_linux.py)

账户目录位于算例卷和 CLI 私有状态之外。会话配置保存账户目录的设备号和 inode。放行后，Linux 启动器打开目录并复核该身份，再按已打开的目录挂载。HOME 内容由用户在各自的会话内管理。 [Agent Shell 的同账户挂载](owned-agent-shell.md#linux-账户-home)

2026-11-26，在 UID 65534 的两套 Docker API、PostgreSQL 和认证 SeaweedFS 上实测。同一账户的算例终端写入文件，经过审批的真实 Agent 命令读到该文件并写入第二份文件。另一 API 的无算例终端读到两份文件。测试重启该 API 后取得新的端口并再次读回相同字节。另一个账户在自己的 HOME 写了同名文件，两个账户的文件彼此独立。四个终端及 Agent 命令均取得已核验的停止回执，算例结果仍正常写回 S3。 [正式回执](../../tmp/j16-agent-linux-hk6e3pdz/agent-compute.json) · [真实验收入口](../server/live_llm_agent_shell_linux_compute_acceptance.py)

首轮在实测跨实例读回后，重启夹具继续查询旧端口而超时。复跑通过 Docker 读取重启后的实际端口。正式轮收到 2 次真实 DeepSeek HTTP 200，两轮共 4 次，模拟 0 次。另用独立 Linux 进程验证冻结目录被替换时拒绝启动，修正身份后正常挂载。空配置下的终端文件隔离、CPU、OOM、缩放和停止回执已复验。 [首轮回执](../../tmp/j16-agent-linux-qxvp0wt9/agent-compute.json) · [临时 HOME 回归](../../tmp/j16-pty-compute-c3tze2k2/result.json)

原 11 个 Docker 服务均核对。构建缓存 5.857 GB，回收 0 B。Windows C 盘可用空间由 39,765,528,576 B 变为 39,761,289,216 B。本次持久化覆盖同一状态卷上的 Linux 终端和 Agent Shell。

### 账户 HOME 容量控制

普通 Linux 终端和 Agent Shell 按同一账户目录合计保留字节，使用 `VIBEFLOW_EXECUTION_WORKSPACE_LIMIT_MB` 作为上限，默认 4096 MiB。算例目录仍独立计量。执行前、放行前、运行中，以及正常退出后检查目录和所在卷的可用空间。扫描使用冻结的目录身份，符号链接只计链接本身的字节。

发现超限时停止本次受管进程范围，写入 `disk_limit` 停止回执。同账户已经打开的其他会话在各自检查时也会停止。新会话在用量恢复前暂缓放行。采样间隔内可能短暂超过上限，已写入的文件仍留在 HOME。管理员可在状态卷移走或删除已确认的文件后复核，新会话重新测量后可继续。无法核验目录时回执使用 `disk_unverified`。 [部署配置](deployment.md#linux-终端与-agent-shell)

2026-11-26，在两套 UID 65534 的 API、PostgreSQL、Redis 和认证 SeaweedFS 上实测。账户 HOME 上限 1 MiB。Agent 写入 2 MiB 后命令和对端已打开的同账户终端均验证停止，延迟写入的标记未生成。同账户的新 PTY 与新 Agent 命令均被拒绝，另一账户正常执行。清理时仅移除测试文件，原来的小文件和符号链接仍在，终端可重新打开。另一次算例终端写完立即退出，回执仍记录 `disk_limit`。算例独立保留 1,228,800 字节文件；失败 Agent 命令的单份 S3 暂存目录和恢复清单仍可核对。正式轮收到 3 次真实 DeepSeek HTTP 200，模拟调用 0 次。 [容量实测回执](../../tmp/j16-agent-linux-gpedrelw/agent-compute.json) · [实测入口](../server/live_llm_agent_shell_linux_compute_acceptance.py)

前两轮的运行检查均通过，随后夹具对保留的 S3 暂存目录判定错误。最终夹具同时核对目录、清单以及保留的算例文件。三轮容量实测与正常写回复验共收到 11 次真实 DeepSeek HTTP 200，其中最终两轮 5 次，模拟调用 0 次。本地文件系统 PTY 的 CPU、OOM、交互和隔离回归通过，模型调用 0 次。 [首轮](../../tmp/j16-agent-linux-ed4wxx4o/agent-compute.json) · [第二轮](../../tmp/j16-agent-linux-76qlr30v/agent-compute.json) · [正常写回复验](../../tmp/j16-agent-linux-g9u8kn9t/agent-compute.json) · [文件系统 PTY 回归](../../tmp/j16-pty-compute-sybn2siu/result.json)

隔离试验结束后，原 11 个 Docker 服务均核对。构建缓存 5.857 GB，回收 0 B。Windows C 盘的可用空间从 39,752,892,416 B 变为 39,752,470,528 B。

### 账户 HOME 内核硬配额

Linux 部署可以启用 `VIBEFLOW_LINUX_SHELL_REQUIRE_PROJECT_QUOTA`，让普通终端和 Agent Shell 共享同一账户的 ext4 项目硬配额。宿主工具按账户 ID 配置项目，设置硬块上限并从内核读回。两个 API 在放行前核验项目编号、继承属性、宿主生成的配额回执和内核显示的容量。执行门绑定已核验的目录，运行期继续检查卷保留空间。文件系统超过硬上限时直接拒绝块分配并返回 `EDQUOT`。HOME 仍可打开，用户可在终端删除自己的文件恢复空间。 [部署步骤](deployment.md#linux-账户-home-硬配额) · [Linux 配额接口](https://man7.org/linux/man-pages/man2/quotactl.2.html)

2026-11-26，在独立 ext4 测试卷及两套 UID 65534 的 API 上通过认证 SeaweedFS 验收。硬上限为 2 MiB。同账户终端先分配 768 KiB 后，Agent 和对端终端各申请 1,536 KiB，均实际收到 `EDQUOT`。另一个账户可独立分配 1,536 KiB。16 MiB 的稀疏文件正常保留，配额按实际分配块计量。未配置项目的账户返回 `disk_unverified`。API 重启后文件和硬上限都继续有效。另将独立测试文件系统换用不同的设备号重新挂载，核对原数据和 `EDQUOT` 后通过。 [最终硬配额回执](../../tmp/j16-agent-linux-1w90u323/agent-compute.json) · [首次实测](../../tmp/j16-agent-linux-7os2tyyy/agent-compute.json) · [卷重挂载回执](../../tmp/j16-home-quota-remount-20261126.log)

硬配额两轮共 4 次真实 DeepSeek HTTP 200。未启用项目配额时，原采样超限停止及清理后恢复回归通过，收到 3 次真实 DeepSeek HTTP 200。模拟调用 0 次。Compose 核验两个 API 副本共用同一卷和一致的配额设置。原 11 个 Docker 服务及测试卷清理均已核对，构建缓存约 5.857 GB，阶段内回收 0 B。Windows C 盘可用空间从 39,251,075,072 B 变为 39,245,545,472 B。 [采样回归回执](../../tmp/j16-agent-linux-_ddeauvp/agent-compute.json)
