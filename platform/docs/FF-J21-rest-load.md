# FF-J21 的 50 用户 REST 复验

2026-11-25，用户决定将 REST p95 300 ms 移出交付和自动化门槛。后续压测继续记录 REST 延迟、并发和业务检查，以下旧回执保留当时的阈值和判定结果。 [当前交付口径](implementation-master-plan.md#93-建议性能门槛)

移除延迟阈值后，在 Linux 隔离 API、原生源码卷和 PostgreSQL 上用 k6 复验 10 个已登录账户。120/120 读请求及业务检查成功，读取 p95 为 472.5 ms，k6 返回 0。保留请求数、错误率和业务检查条件，读取耗时由独立指标统计，不含注册和算例准备。 [复验回执](../../tmp/ff-j21-rest-20261124/linux-postgresql-steady-k6-10-timings-native-app.json)

2026-11-24 在独立数据库和账号下运行。Windows API 分别使用 SQLite、PostgreSQL 16 与本地文件系统，Linux API 以 UID 65534 运行，使用独立 PostgreSQL 16 和 Docker 原生存储卷。负载由同一 Windows 客户端发起。每个用户顺序注册、建工作区、建算例、读取账户和文件树，50 个用户并发，共计 250 个请求。计时前先另建对照算例，验证其所有者读取 HTTP 200。计时内的 50 次文件树请求中，49 个其他账号访问该算例均返回 HTTP 404，另 1 个账号读取自己的算例返回 HTTP 200。模型调用 0 次。

| API 与存储 | 成功请求 | 总体 p95 | 注册 p95 | 创建算例 p95 | 账户 p95 | 文件树 p95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Windows、SQLite、本机文件系统 | 250/250 | 12,870.24 ms | 6,850.63 ms | 18,219.97 ms | 306.10 ms | 638.78 ms |
| Windows、PostgreSQL、本机文件系统 | 250/250 | 21,192.06 ms | 4,470.87 ms | 26,758.27 ms | 160.49 ms | 459.22 ms |
| Linux、PostgreSQL、原生卷 | 250/250 | 10,901.99 ms | 1,478.40 ms | 13,366.51 ms | 20.57 ms | 69.07 ms |

每轮错误为零。Linux 工作区创建 p95 为 450.50 ms。首轮未修复的 Windows SQLite 完成 216 个请求，出现数据库写锁，PostgreSQL 完成 114 个请求，出现算例创建 429 和连接池等待超时。首轮分位数只覆盖部分请求。2026-10-29 的历史负载另有约 2.1 秒结果，本页给出当前实现与明确存在的跨租户算例之复验。 [SQLite](../../tmp/ff-j21-rest-20261124/sqlite.json) · [PostgreSQL](../../tmp/ff-j21-rest-20261124/postgresql.json) · [Linux](../../tmp/ff-j21-rest-20261124/linux-postgresql.json)

注册初次查重后结束读取事务，随后进行 PBKDF2 和唯一约束写入。新建算例在持有业务数据库连接前获取现有算例 I/O 名额。在同一账户配额事务内，一次核对默认算例的存储峰值。额度不足以覆盖峰值时，逐文件执行原检查。单算例创建的账户存储盘点从 17 次降到 2 次，三次样本耗时由 SQLite 的 884.8–1004.5 ms 降至 401.1–535.6 ms，PostgreSQL 由 974.4–1146.3 ms 降至 459.6–617.4 ms。 [SQLite 前后](../../tmp/ff-j21-rest-20261124/sqlite-provision-profile.json) · [PostgreSQL 前后](../../tmp/ff-j21-rest-20261124/postgresql-provision-profile.json)

两种数据库均核验正常创建 HTTP 200、峰值额度少 1 字节时逐文件核对后 HTTP 200、零额度 HTTP 429 和盘点不可用 HTTP 503。两个拒绝路径均验证没有残留的算例记录或文件。八次相同账号注册并发均为一次成功、七次 HTTP 409。核心 API 回归通过。Linux 隔离容器、网络和工作卷已移除，原运行容器清单一致。Linux 复验前后 C 盘为 41,329,766,400 B 与 41,329,463,296 B 可用，Docker 阶段未执行回收。 [SQLite 边界](../../tmp/ff-j21-rest-20261124/sqlite-quota-boundary.json) · [PostgreSQL 边界](../../tmp/ff-j21-rest-20261124/postgresql-quota-boundary.json) · [核心 API](../../tmp/ff-j21-rest-20261124/core-integrity-after-batch.log)

### 性能目标与其他验收证据

初始方案把 REST p95 小于 300 ms 列入 50 用户同时运行 PTY、可视化和 Agent SSE 的整体验收，实施主计划在「建议性能门槛」中沿用，记载 M0 复核后冻结。该目标约束的是一组样本中至少 95% 的 REST 请求耗时，超出耗时目标的请求仍可正常完成。两份文档未记载 300 ms 的推导、线上基线或各类端点的时间预算。注册、建工作区和建算例作为独立写入路径单独汇报，日常读请求按已登录用户另测。上述 HTTP 压测均经本机回环，Linux 走 Docker 端口转发。环境对比包含系统、文件系统和数据库连接路径的差异。 [初始负载方案](plan-cs-to-bs.md#验证方案) · [总体性能指标](implementation-master-plan.md#93-建议性能门槛)

200 作业公平性已有 2026-11-23 证据。SQLite 和 PostgreSQL 各封存 200 个积压项加另一个账号的单项，使用真实 Redis 和两个 worker。小任务账号先完成，第二 API 取消 100 个未领取项，并核验每轮 10 次跨账号拒绝。两个回执各含 2 次真实 DeepSeek HTTP 200，模拟 0 次。完整 50 用户混合负载、SSE 首 token 和可视化互动仍待按原指标验收。 [双库公平性](fair-queue-and-batch-reconciliation.md#201-项积压验收)

### 已登录账户与文件树

在 Linux API、PostgreSQL 16 与原生卷上另建 50 个账号及各自算例。计时从账号和算例准备完毕后开始。六轮各 50 个用户，同时读取自己的账户信息和文件树，每轮间隔 100 ms。文件树返回实际条目，账户响应逐个匹配账号。路径仍是本机回环，经 Docker 转发。 [负载入口](../server/load_rest_50_smoke.py)

首轮 600 个请求中 439 个成功，161 个自有文件树请求返回 HTTP 429。修改本地文件树及补丁读取，在请求已有期限内等待现有四个文件读取名额。保持扫描前后核对流水位和存储标记。请求被取消时仍等待提交的 I/O 退出。在同场景重跑取得 600/600 成功，整体 p95 为 4,996.62 ms，账户信息 3,817.35 ms，文件树 5,236.69 ms。首轮分位数包含 429，本次分别保留两个原始回执。两轮均无模型调用。 [首轮](../../tmp/ff-j21-rest-20261124/linux-postgresql-steady-before.json) · [等待名额后](../../tmp/ff-j21-rest-20261124/linux-postgresql-steady.json)

同一负载在独立 Linux 客户端容器经 Docker 本地网桥直连，600/600 成功，整体 p95 为 2,118.08 ms，账户信息 1,050.49 ms，文件树 2,277.56 ms。该轮省去 Windows 客户端与端口转发。三轮均不经过公网，两种客户端结果独立记录。已有测试还不能量化主机负载、客户端调度和端口转发各自的耗时。隔离环境回收完毕，核心 API 回归通过。 [Linux 客户端复验](../../tmp/ff-j21-rest-20261124/linux-postgresql-steady-local-client.json) · [核心 API](../../tmp/ff-j21-rest-20261124/core-integrity-after-tree-admission.log)

Grafana k6 2.0.0 用 50 个虚拟用户各执行六轮，在同一类 Linux 隔离拓扑复验。账号/工作区/算例的准备请求从性能指标排除。两轮实测共 1,200 个读取和 1,200 次业务检查，均通过，零 HTTP 错误。k6 的读取 `http_req_duration` p95 分别为 2,357.48 ms 和 1,797.12 ms，两个进程均按 300 ms 阈值返回 99。k6 的每用户间隔从各自上一轮结束后开始，HTTPX 每轮有所有用户的同步点。回执只保存汇总指标和运行元数据。模型调用为零。本结果覆盖已登录读取。 [Grafana k6 发行版](https://github.com/grafana/k6/releases/tag/v2.0.0) · [脚本](../tools/k6-rest-reads.js) · [首轮回执](../../tmp/ff-j21-rest-20261124/linux-postgresql-steady-k6-first.json) · [复验回执](../../tmp/ff-j21-rest-20261124/linux-postgresql-steady-k6.json)

2026-11-25 在相同镜像、源码、独立 PostgreSQL 16 和本机 Docker 网桥拓扑下，从 1、10、50 用户依次复测。三档 732 次读取及业务检查全部通过。表内是各指标的 p95，分位数不可相加。等待响应从请求发出计至收到第一个响应字节，包含本机应用处理和调度耗时。三轮连接 p95 均低于 1 ms。早前 50 用户两轮与本轮存在运行间波动，本轮结果保留在 50 用户的观测范围内。 [1 用户](../../tmp/ff-j21-rest-20261124/linux-postgresql-steady-k6-1-timings.json) · [10 用户](../../tmp/ff-j21-rest-20261124/linux-postgresql-steady-k6-10-timings.json) · [50 用户](../../tmp/ff-j21-rest-20261124/linux-postgresql-steady-k6-50-timings.json)

| 用户 | 成功读取 | 总体 REST | 账户 | 文件树 | 等待响应 | 建立连接 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 12/12 | 271.53 ms | 18.79 ms | 366.36 ms | 271.26 ms | 0.125 ms |
| 10 | 120/120 | 1,425.01 ms | 833.76 ms | 1,547.72 ms | 1,421.75 ms | 0.118 ms |
| 50 | 600/600 | 5,606.75 ms | 3,532.80 ms | 6,489.39 ms | 5,589.83 ms | 0.861 ms |

三轮模型调用均为零。首档只有 12 次读取，单用户 p95 作为本轮对照。50 用户总体目标和完整混合负载继续验收。C 盘可用从 41,254,187,008 B 变为 41,252,691,968 B，Docker 构建缓存回收 0 B。原 11 个服务继续运行，隔离栈均已回收。

功能结果、并发容量和响应时间分别记录。300 ms 为早期 50 用户混合场景的历史参考。该轮本机网桥的主要耗时在等首字节。公网访问和真实 DeepSeek 响应另有各自的计时起点和证据。

另将当前同一份应用源码按发布镜像的目录范围复制到本机 Docker 原生卷并以只读挂载启动。重新执行 50 用户读取，600/600 通过。总体 REST p95 为 1,959.89 ms，等待首字节 p95 为 1,941.68 ms，连接 p95 为 2.20 ms。账户 p95 为 1,089.57 ms，文件树 p95 为 2,167.22 ms。此轮减少 Windows 源码挂载的影响，结果与前两轮 1.80–2.36 秒的范围接近。各轮存在独立启动和主机负载波动，差值不用于计算某个组件的固定耗时。模型调用 0 次，原服务仍运行，隔离栈已回收。 [原生源码卷复验](../../tmp/ff-j21-rest-20261124/linux-postgresql-steady-k6-50-timings-native-app.json)

同一原生源码拓扑启用隔离服务端计时。50 用户 600 次均成功，客户端 p95 为 1,847.33 ms，首字节等待 1,841.60 ms，连接 0.63 ms。服务端从进入 ASGI 到响应头的 p95，账户为 990.92 ms、文件树为 1,975.70 ms。文件树每次执行 19 条 SQL，单请求累计游标调用时间 p95 355.08 ms。三段文件读取的总范围 p95 466.42 ms，底层实际函数累计 p95 85.98 ms。按同一请求相减后的等待、调度与分派时间 p95 为 403.91 ms。游标计时从发送 SQL 前开始，连接池等待还需独立采样。观测器只记操作类别、状态、次数和耗时。模型调用 0 次。 [服务端分解](../../tmp/ff-j21-rest-20261124/linux-postgresql-steady-k6-50-timings-native-app-profiled-stages.json) · [客户端回执](../../tmp/ff-j21-rest-20261124/linux-postgresql-steady-k6-50-timings-native-app-profiled.json)

存储准入改成一次查询，同时核对正在执行及未收尾记录，维持原活动记录优先和执行者匹配。真实 SQLite、PostgreSQL 各通过七种状态，包括活动记录与未收尾历史同时存在。隔离 API 完整性通过。50 用户同拓扑复测 600/600，文件树 SQL 从每次 19 条降为 17 条，单请求游标累计 p95 从 355.08 ms 降到 273.09 ms。客户端总体 p95 1,830.96 ms，文件树服务端 p95 2,046.05 ms。账面 SQL 减少有逐请求证据，两次整轮延迟接近。模型调用 0 次，隔离资源已回收。 [双库状态检查](../../tmp/ff-j21-rest-20261124/storage-admission-checks.json) · [服务端对照](../../tmp/ff-j21-rest-20261124/linux-postgresql-steady-k6-50-timings-native-app-profiled-admission-query-stages.json) · [核心 API](../../tmp/ff-j21-rest-20261124/core-integrity-after-admission-query.log)

### 真实 DeepSeek 首 token

另在隔离 Windows SQLite API 为 50 个账号分别准备算例、方案模式会话与官方 `deepseek-flash` 配置，并发发送短问答。首轮真实上游 7 次 HTTP 200，6 个请求记录到首个可见正文片段，5 个回合完整结束。部分样本 p95 为 39,204.76 ms。服务端日志含 20 次数据库连接池超时，部分回合写入了算例记忆读取失败。模拟模型 0 次。 [首轮回执](../../tmp/live-j21-sse-ylebxk54/live-result.json) · [真实模型测试入口](../server/live_llm_j21_sse_load_acceptance.py)

会话历史读取在进入独立的算例记忆事务前结束。在获取外部密钥前释放连接，并在组织配置复核后结束只读事务。Agent 审批与上下游持久事件仍经各自事务。普通核心 API 复验已通过。另用真实 DeepSeek 完成非流式翻译、普通流式问答与实际工具读回续答，4 次 HTTP 200 推理及 1 次模型目录访问。50 个流式会话复测和混合负载仍按原目标验收。 [核心 API](../../tmp/ff-j21-rest-20261124/core-integrity-after-agent-session.log) · [真实模型回归](../../tmp/ff-j21-rest-20261124/agent-session-regression-live.json)

同一 SQLite 拓扑复测 50 个会话。上游 `deepseek-flash` 有 47 次实际 HTTP 200，数据库连接池超时降为零，28 个回合完成，另 22 个留下取消终态。41 个观察到可见首 token 的请求 p95 为 41,037.78 ms，只作为部分样本。47 个实际上游 HTTP 响应头在模型请求发出后的 p95 为 1,438 ms。两个分位数的起点与事件不同，分别保存。模拟模型 0 次，隔离 API 退出，50 个会话执行归属全部回到 idle。 [复测回执](../../tmp/live-j21-sse-dfv8stf7/live-result.json) · [上游回执](../../tmp/live-j21-sse-dfv8stf7/upstream.jsonl)

Windows API 换用独立 PostgreSQL 后，49 次实际 DeepSeek HTTP 200，29 个会话完整结束，41 个首片段样本 p95 38,946.22 ms。连接池超时为零，新增诊断记录 24 次执行租约过期。后续按实际流逝时间安排续租，并在已经登记的流开始执行时续租。用同一模型、数据库拓扑和 50 用户复测，50 次实际 HTTP 200、50 个可见首片段与 50 个 `run.completed`，租约过期及连接池超时均为零。首片段 p95 40,000.41 ms，两条连接在收到完成事件后仍等到 120 秒客户端期限，完整传输核对 48/50。两个独立 PostgreSQL 数据库已清理。 [PostgreSQL 首轮](../../tmp/live-j21-sse-ipvmdgu8/live-result.json) · [续租后](../../tmp/live-j21-sse-vf9u6nyx/live-result.json) · [临时库清理](../../tmp/terminal-agent-postgres-kol8sthg/cleanup.json)

### 50 路原生 PTY

在最新 Linux 发布候选镜像、独立 PostgreSQL 和原生 Docker 卷中，先为每名用户建立自己的账号和算例。实际程序化 WebSocket 使用请求头认证，经本机 Docker 网桥直连。初次压测容器遗漏生产 Compose 的进程命名空间配置，50 个启动均失败。复验使用生产 Compose 配方，在 UID 65534 下启动 50 个本地 Bash PTY。50 个会话同时保持连接，每个实际执行一次 `printf` 并核对输出，发送命令到读回标识的 p95 为 3,241.38 ms。50 个会话分别经所属用户关闭且返回有效停止证据。隔离容器、网络与卷已回收，原运行容器均保有。模型调用 0 次。 [验收入口](../server/terminal_load_50_acceptance.py) · [默认配置失败](../../tmp/ff-j21-rest-20261124/linux-postgresql-pty-local-client-default-profile.json) · [50 路回执](../../tmp/ff-j21-rest-20261124/linux-postgresql-pty-local-client.json)

### 50 账号混合负载

2026-11-25，使用同宿主隔离 Nginx、两套 Linux API、PostgreSQL、Redis 和原生存储卷。同一批账号先建立真实 Linux PTY 和原生 ParaView 会话，再于同一时间开始终端命令、鼠标拖动、Agent SSE 与 k6 读请求。50 场景包含 5 个百万单元视图。每名用户的 k6 流程有 6 次账户及算例文件树读取，共 12 次。模型为已授权的官方 DeepSeek `deepseek-flash`，采样只记录状态、时间和模型响应标识。 [显式验收入口](../server/live_llm_j21_mixed_acceptance.py) · [两账号联通回执](../../tmp/ff-j21-mixed-20261125/mixed-2.json) · [k6 入口](../../tmp/ff-j21-mixed-20261125/reads.js)

首轮 50 账号中，600 次读取、50 条终端命令、50 次官方请求及 50 个 SSE 回合均成功。最早准备好的 33 路视图连接在开始拖动时立即报 `ClientConnectionResetError`。其余 17 路成功，17 路的 196.6 ms p95 仅代表部分样本。最终仍读到 50 条 PTY 和 50 条视图停止证明。该轮请求端 REST p95 3,631.4 ms、Agent 首可见片段 p95 19,429.7 ms、终端命令 p95 3,799.0 ms。最早实际 DeepSeek 出站请求在统一起跑后约 6.14 秒发出，上游发出至响应头 p95 897 ms。 [首轮 50 账号](../../tmp/ff-j21-mixed-20261125/mixed-50-first.json)

压测客户端在场景准备期间补上 WebSocket 活动，起跑前逐个验证视图的真实 RPC。首个视图在复验开始前已经连接约 92.7 秒。复验取得 600/600 次读取、50/50 条终端输出、50/50 张拖动帧、50/50 个模型完成标识和 100 条停止证明。四种客户端活动窗口重叠 1.717 秒。实际第一条出站模型请求在起跑后 7.281 秒发出，因此另用真实上游响应触发第二轮原生操作。 [保活复验](../../tmp/ff-j21-mixed-20261125/mixed-50-functional.json)

第三轮观测到真实 DeepSeek HTTP 200 后再次触发终端命令和鼠标拖动，取得 50 条真实终端输出和 50 张原生图像。按 REST、终端与可视化活动的共同时间窗计算，29 条真实模型 HTTP 请求的执行区间与该窗口重叠。全程 600/600 次读取和对应业务检查成功，50/50 个 SSE 均收到可见正文及回合完成事件。官方上游返回 50 个唯一 `deepseek-flash` 完成标识。50 路终端与 50 路视图均取得停止证据。 [第三轮原始回执](../../tmp/ff-j21-mixed-20261125/mixed-50.json) · [阶段数据](../../tmp/ff-j21-mixed-20261125/phase-review.json)

| 50 账号实测 | 原计划 p95 目标 | 第二轮 p95 | 第三轮 p95 |
| --- | ---: | ---: | ---: |
| 已登录 REST 读取 | <300 ms | 3,761.1 ms | 4,424.7 ms |
| Agent 首可见正文 | <2,000 ms | 21,282.7 ms | 22,779.1 ms |
| ParaView 第一轮拖动至图像 | <200 ms | 445.7 ms | 393.6 ms |
| 实际模型请求期间的 ParaView 拖动至图像 | <200 ms | 未采样 | 404.1 ms |
| PTY 首轮命令至输出 | — | 4,340.8 ms | 4,529.4 ms |

第三轮 k6 的 600 次已登录读取都走本机 Docker 网桥。连接建立 p95 为 0.15 ms，等待首字节 p95 为 4,424.6 ms。用户公网网速处于这条 REST 测量路径之外。Agent 的第一条实际出站请求在起跑后 7.051 秒发出。从出站到上游 HTTP 响应头 p95 为 1.054 秒，到观测到模型首段正文 p95 为 14.616 秒。后一项包含公网传输、供应商排队和生成以及本机客户端读取调度，现有计时无法拆出公网延迟的单独贡献。各项分位数按各自样本计算。 [第三轮原始回执](../../tmp/ff-j21-mixed-20261125/mixed-50.json) · [阶段数据](../../tmp/ff-j21-mixed-20261125/phase-review.json)

本机混合场景的功能与退出核对通过，三项原性能目标未达到。联通试验和三轮 50 账号合计 152 次真实 DeepSeek HTTP 200，模拟 0 次。三轮的隔离栈已回收，原 11 个运行容器身份相同。第三轮前后 C 盘可用字节为 40,724,660,224 与 40,726,949,888，Docker 构建缓存维持 5.819 GB，本阶段回收 0 B。03:00 定时维护因空间高于触发线跳过。 [C 盘与 D 盘读数](../../tmp/ff-j21-mixed-20261125/sustained-disk-after.json) · [Docker 占用](../../tmp/ff-j21-mixed-20261125/sustained-docker-after.txt)

### 2026-11-26 流式首段正文复验

本机 Windows 配置了系统代理。原实现每个模型 HTTP 客户端初始化时重复载入 CA 证书，12 次只构造客户端的本机对照中位数为 447.04 ms。现在复用默认 CA 验证上下文，仍为每个请求创建独立 HTTP 客户端。复用涵盖两种流式协议、非流式补全和模型目录。HTTPS 仍验证主机名。显式配置的 CA 路径在请求时重新加载，回环访问沿用直连。预热后的 49 次客户端初始化中位数为 0.37 ms，首次为 211.59 ms。 [模型客户端](../server/app/services/llm.py)

同时修复工作台选择「关闭」推理时官方 DeepSeek 仍可能执行推理的问题。对官方 `deepseek-flash` 和 `deepseek-v4-pro` 的 Chat Completions 请求显式传入 `reasoning_effort=none`。仅复用 TLS 的分段轮有 14/50 个请求返回推理，修复后为 0/50。显式选择 `low` 的单独请求实际返回推理和正文。首次低档探针的 PowerShell 管道将中文输入转换为问号，保留该次回执，改用英文探针后通过。 [DeepSeek Chat Completions 参数](https://api-docs.deepseek.com/api/create-chat-completion/) · [低档复验](../../tmp/ff-j21-sse-tls-20261126/deepseek-low-retry.json)

四轮均使用 Windows 隔离 API、临时 PostgreSQL 数据库及 50 个独立账号。每轮实际 DeepSeek HTTP 200 和唯一完成标识均为 50，首段正文样本均为 50。

| 场景 | 完整 SSE | 首段正文 p95 | 回执 |
| --- | ---: | ---: | --- |
| 修复前 | 49/50 | 30,606.12 ms | [基线](../../tmp/live-j21-sse-hz8rz34k/live-result.json) |
| 复用 TLS 验证上下文 | 50/50 | 5,903.07 ms | [对照](../../tmp/live-j21-sse-4a46i0t3/live-result.json) |
| 复用 TLS，增加阶段计时 | 50/50 | 6,579.14 ms | [分段](../../tmp/live-j21-sse-40sy7kxj/live-result.json) |
| 复用 TLS，显式关闭推理 | 50/50 | 6,049.07 ms | [最终](../../tmp/live-j21-sse-k4mlufpw/live-result.json) |

基线有一路已收到 `run.completed`，其 HTTP 连接仍等到客户端期限。最终轮从提交到 HTTP 响应头、`ready` 和 `context.window` 的 p95 分别为 1,918.39、3,596.74 和 4,968.97 ms。同一请求从 `context.window` 至首段正文的 p95 从分段轮的 2,959.77 ms 变为 1,622.95 ms。该段包含上游连接、生成及平台转发。端到端 2,000 ms 目标仍待达到。本轮覆盖单独 SSE 压力，Linux 混合压测沿用上文历史回执。 [分段验收脚本](../server/live_llm_j21_sse_load_acceptance.py)

模型目录、非流式翻译、流式问答持久化、实际文件工具读取及续答复验通过。四轮负载、常规回归和两次低档探针合计 206 次真实推理 HTTP 200，另有一次模型目录 HTTP 200，模拟 0 次。四个隔离数据库均已删除，原 11 个 Docker 服务继续运行，本轮未构建镜像。 [常规回归](../../tmp/live-deepseek-n19f17pd/live-result.json) · [最终轮临时库清理](../../tmp/terminal-agent-postgres-du2fzhgu/cleanup.json)

### 2026-11-26 Linux 混合复跑与算例记忆

旧读取入口按整棵文件树的快照验证 Agent 所需的长期记忆。现在在算例协调锁内重新检查账号和访问权限，只读取受版本校验的 `0-caseDict/memory.md`。读取上限仍为 64 KiB，文件缺失时记忆为空。读取其他工具输入和目录依然使用各自原有的快照路径。独立 PostgreSQL 与本地文件系统对同一文件交替执行两种读取各 16 次。每次 SQL 从 17 条降为 11 条，服务端中位数从 29.14 ms 降为 15.82 ms。实际删除后的读取为空，其他账号返回 404。此对照没有请求模型。 [读取实现](../server/app/services/agent_storage_capacity.py) · [本地对照回执](../../tmp/prompt-memory-snapshot-isds46wt/read-paths.json)

认证 SeaweedFS 与独立 PostgreSQL 验证了真实模型追加记忆、在新会话召回，以及从 147,081 字节的记忆文件中按版本读取末尾的 65,508 字节。发给 DeepSeek 的上下文包含保留标识，过期标识已裁掉，模型答复复现保留标识。四次真实模型请求均为 HTTP 200，模拟 0 次。存储额度已结算，临时库已删除，S3 进程退出及临时身份文件的移除均已核验。 [S3 实测](../../tmp/live-deepseek-edit-memory-7fn40fdp/live-result.json) · [数据库清理](../../tmp/live-approval-postgres-24v2euqd/cleanup.json)

三轮在两套隔离 Linux API、Nginx、本机 PostgreSQL 和 Redis 上分别使用 50 个独立账号。每轮均完成 600/600 次 REST 读取、50/50 个 DeepSeek 回合、50/50 条 PTY 命令、50/50 张拖动画面和 100 条会话停止证明。每轮有 50 次真实 `deepseek-flash` HTTP 200，模拟 0 次。

| 50 用户场景 | REST p95 | 首段正文 p95 | 拖动画面 p95 | 模型请求期间拖动画面 p95 |
| --- | ---: | ---: | ---: | ---: |
| TLS 与推理设置修复后 | 3,825.9 ms | 14,119.7 ms | 291.3 ms | 369.8 ms |
| 记忆按单文件读取 | 4,008.5 ms | 14,755.2 ms | 323.3 ms | 268.0 ms |
| 仓库内验收入口复跑 | 3,970.0 ms | 14,541.4 ms | 310.6 ms | 410.7 ms |

最后一轮核对 50 次单文件读取，读取本身的 p95 为 2,824.5 ms。HTTP 响应头、`ready` 和 `context.window` 的 p95 分别为 5,587.16、11,324.21 和 13,177.81 ms。20 条真实上游请求与 REST、终端及可视化共同活动窗口重叠。整轮首段正文未出现稳定改善，仍需达到 2,000 ms，拖动仍需达到 200 ms。原 11 个容器标识逐项一致，临时栈已回收。C 盘阶段前约 39.9 GB 可用，结束实测约 39.8 GB，未构建镜像。 [修复后首轮](../../tmp/ff-j21-mixed-20261126/mixed-50.json) · [单文件首轮](../../tmp/ff-j21-mixed-memory-20261126/mixed-50.json) · [最终混合复跑](../../tmp/ff-j21-mixed-3yby2821/mixed-50.json)

混合验收所需的双副本启动、实际渲染连接、上游观测和 k6 脚本均在仓库。命令接收本机 Linux amd64 k6 与两份 VTP 文件。可用 `--api-image` 和 `--web-image` 指定已准备的 ParaView API 和 Nginx 镜像，默认沿用本轮已验的镜像。每次生成独立的 `tmp/ff-j21-mixed-*` 回执目录。

```powershell
& platform/server/.venv/Scripts/python.exe -B -u platform/server/live_llm_j21_mixed_acceptance.py --source-profile-id 26 --users 50 --k6 tmp/ff-j21-rest-20261124/k6-linux-amd64 --small-grid tmp/FF-D01-native-viewport-20261123/sample.vtp --large-grid tmp/FF-D22-viz-load-20261123/million-cells.vtp
```

## 2026-11-26 混合负载完成帧复验

隔离的渲染观察器给每次实际鼠标移动登记序号。客户端等到移动确认和同序号的 480×360 完整 JPEG 后才认定样本有效，计时截止到收到完整图像通知。上一移动的帧和编码仍在进行时的发布会被跳过。上方三轮的拖动数据按各自原口径保留。 [验收入口](../server/live_llm_j21_mixed_acceptance.py) · [观察与匹配实现](../server/mixed_load_test_support.py)

50 用户实测完成 600/600 次 REST 和业务检查、50/50 个真实 DeepSeek 回合、两轮各 50 条 PTY 命令和两轮各 50 张匹配完成帧。REST p95 为 4,018.2 ms，成功率 100%。首段正文 p95 为 13,182.5 ms，首轮拖动完成帧 p95 为 331.0 ms，上游请求期间拖动完成帧 p95 为 368.9 ms。13 次上游调用与后一轮共同活动窗口重叠。50 次真实 `deepseek-flash` 推理均返回 HTTP 200，模拟 0 次。SSE 2,000 ms 和拖动 200 ms 继续验收。 [混合回执](../../tmp/ff-j21-mixed-5j242rlx/mixed-50.json)

100 条会话停止证明齐全，隔离容器、数据卷和网络已经移除，原 11 个 Docker 容器 ID 逐项一致。C 盘可用从 39,827,193,856 B 变为 39,825,453,056 B。使用现有镜像，Docker 构建缓存回收 0 B。 [磁盘回执](../../tmp/ff-d22-phase-20261126/mixed-disk-after.json) · [Docker 占用](../../tmp/ff-d22-phase-20261126/docker-after.txt)

## 2026-11-26 合并 Agent 启动事务

SSE 启动把执行者续租、会话及算例元数据、待审批判断和 `run_state.started` 合并到一个数据库事务。无工具的问答模式跳过组织技能目录所需的成员查询。模型配置授权、长期记忆读取及推理前的请求回执继续执行。 [Agent 路由](../server/app/routers/agent.py)

以相同隔离双 API、PostgreSQL、Redis、k6 脚本和网格复测，600 次 REST 请求及业务检查成功率均为 100%，50 个真实 `deepseek-flash` 回合均收到正文并结束。两轮各完成 50 条 PTY 命令及 50 张对应 JPEG，100 条会话停止证明齐全，9 次真实上游调用与第二轮活动窗口重叠。模型 HTTP 200 共 50 次，模拟调用 0 次。 [复验回执](../../tmp/ff-j21-mixed-ilz4vqgr/mixed-50.json)

| 指标 p95（ms） | 完成帧基线 | 合并启动事务 |
|---|---:|---:|
| REST | 4,018.2 | 3,624.2 |
| 收到响应头至 `ready`（逐回合差值） | 6,262.3 | 5,794.7 |
| 首段正文 | 13,182.5 | 12,781.0 |
| 上游实际请求至首段正文（逐回合差值） | 1,409.3 | 1,391.4 |
| 首轮拖动完成帧 | 331.0 | 449.3 |
| 上游活动期间拖动完成帧 | 368.9 | 288.5 |

单轮对比中启动阶段耗时缩短，长期记忆读取 p95 从 2,880.5 ms 到 3,408.0 ms，首轮拖动也变慢。首段正文 2,000 ms 和拖动 200 ms 两项目标仍未达到。隔离容器、网络和卷已回收，原 11 个 Docker 容器 ID 一致。C 盘可用从 39,803,109,376 B 变为 39,782,060,032 B，构建缓存维持 5.857 GB。

另外用实际 DeepSeek 发出两个写入提案并重启隔离 Windows API。待审批目标和首次批准均没有发出推理请求，第二次批准才恢复推理并完成。重复批准返回 409，两份文件与审批回执一致。 [双提案回执](../../tmp/live-cli-startup-kovyeee1/pending-approval-recovery.json)

## 2026-11-26 混合负载加入 CalculiX 作业

每个账户准备交互算例和一个本机求解算例。求解算例用工程作业接口提交带材料依赖的 C3D8 单元，顶面每节点载荷按账户递增。HTTP 提交最多四条并行，Redis 工作者领取作业。以独立八点积分刚度矩阵核对全部八个节点的位移，逐件比较原路径 FRD、不可变发布副本和发布摘要，并检查作业根进程及 CalculiX 子进程的停止证明。 [混合验收入口](../server/live_llm_j21_mixed_acceptance.py) · [求解与读取证明](../server/mixed_load_calculix.py)

隔离部署复用原 ParaView API 和 Web 镜像。CalculiX 工作者使用已有 `vibeflow/api:linux-calculix-v1-20261118` 镜像，API 与工作者以只读卷共享其启动程序，工作者使用该镜像中的原生依赖。可用 `--ccx-image` 指定同样提供 `/usr/local/bin/ccx` 的本机镜像。 [CalculiX 镜像构建说明](linux-calculix-deployment.md)

双用户首轮中，两个求解器都已计算并写出 FRD。作业源校验等待同算例的可写 PTY 退出，读回超时。将求解工作移到各账户的独立算例后，两个作业均由实际 Linux 工作者完成并通过数值、发布和四份停止回执核对。两轮各有 2 次实际 DeepSeek HTTP 200，模拟调用 0 次。这个小规模试跑的原生作业登记晚于第一轮的共同活动窗口。 [首轮回执](../../tmp/ff-j21-mixed-argaakoo/mixed-2.json) · [双用户复验](../../tmp/ff-j21-mixed-4pywhy87/mixed-2.json)

50 用户首轮从同一时刻发起五类操作。600 次 REST 检查、50 个实际 DeepSeek 回合和两轮 PTY 与可视化均完成。46 个求解作业已完成数值、发布和跨账户隔离核对，另有 4 次提交返回 409。第一份原生子进程登记比约定起点晚 20.53 秒，当时 PTY 与可视化的共同窗口已经结束。复验在确认 Redis 工作者实际开始作业后再起交互负载，记录每次提交状态，并使用相同幂等键重试明确的临时容量拒绝。 [首轮 50 用户数据](../../tmp/ff-j21-mixed-9686ynpe/mixed-50.json)

50 用户第二轮接受并完成 50 个作业。全部根进程与子进程停止回执齐全，跨账户读取的 100 次检查都返回 404。按数据库登记的根进程启动到 CalculiX 子进程停止计，4 个作业与真实上游推理及第二轮 PTY、可视化和 REST 的共同窗口重叠。两份下载遇到 429，48 个作业完成数值与归档核对。后续测试按 `raw_download_busy` 的 `Retry-After` 重试，并记录提交及下载的 HTTP 状态。 [第二轮数据](../../tmp/ff-j21-mixed-gn6orbkk/mixed-50.json)

最终复验中，50/50 个作业由真实 Linux Redis 工作者执行，50 个不同载荷的 FRD 均通过独立数值比对、不可变文件比对与摘要核验。最大位移绝对误差为 3.34×10⁻⁸，以算例长度单位计。150 次产物读取成功，4 次 `raw_download_busy` 按服务器等待时间恢复。50 组跨账户作业和文件访问共 100 次均返回 404。两轮各完成 50 条 PTY 命令、50 张匹配完成帧，终端、可视化、作业根和子进程共 200 份停止证明。50 个真实 `deepseek-flash` 回合完整，HTTP 200 共 50 次，模型模拟 0 次。 [最终 50 用户回执](../../tmp/ff-j21-mixed-8yduyzzx/mixed-50.json)

Redis 作业提交起点比交互组早 9.05 秒。第二轮 PTY、可视化和 REST 的共同窗口内，有 13 次真实上游模型调用及 4 个已登记的求解作业根活动。从 CalculiX 子进程登记至停止计算的重叠计数为 0。双 API 及工作者的 cgroup 内存峰值分别为 6,009、5,051、618 MiB，清理后分别为 347、259、188 MiB。隔离容器、卷和网络已回收，原 11 个运行容器的标识一致。C 盘可用由 39,769,976,832 B 变为 39,765,049,344 B，构建缓存回收 0 B。 [资源与时序数据](../../tmp/ff-j21-mixed-8yduyzzx/mixed-50.json)

| 最终复验指标 | 结果 |
| --- | ---: |
| REST 请求及业务检查 | 600/600 |
| REST p95 | 6,603.9 ms |
| SSE 首段正文 p95 | 20,583.5 ms |
| 两轮拖动完成帧 p95 | 539.8 / 533.3 ms |
| 从实际发出请求至作业受理 p95 | 16,758 ms |
| 从安排提交到数值核验完成 p95 | 146,583 ms |

50 用户五类工作负载的功能核对通过。SSE 首段正文 2,000 ms、可视化拖动 200 ms 仍待优化与验收。

### 2026-11-26 算例加锁与事件序号复验

PostgreSQL 对已有算例计数器使用 `FOR NO KEY UPDATE`，保留同一行的互斥锁，减少一次 SQL 往返和一次无值变更的记录更新。首次加锁仍在当前事务中建立缺失的计数器。事件序号通过单次 UPSERT 分配，事务回滚后可重新使用该序号。访问算例和工作区的回收站状态时复用一次索引读取。 [算例序号和锁](../server/app/services/durable_events.py) · [算例访问检查](../server/app/routers/workspaces.py)

实际 PostgreSQL 16 的 60 次既有算例加锁，SQL 语句由 120 降至 60，单连接总耗时由 158.32 ms 降至 62.15 ms。验证了锁持有期间同算例事件等待、另一算例事件正常提交，且仅加锁时 PostgreSQL 记录版本不变。PostgreSQL 和 SQLite 各执行 72 次并发事件追加，其中 48 次提交、24 次回滚，提交序号无缺口。旧算例缺少计数器时可加入首条事件，单独的首次加锁回滚后未留下计数器。模型调用 0 次。 [数据库回执](../../tmp/ff-j21-case-counter-20261126/counter-verification.json) · [核验代码](../../tmp/ff-j21-case-counter-20261126/verify-counter.py)

沿用相同镜像与网格复验 50 用户五类混合负载。600/600 次 REST 检查、50 个真实 `deepseek-flash` HTTP 200、50 个 CalculiX 数值与文件摘要核对、两轮各 50 条 PTY 命令和匹配 JPEG 均完成。50 个账户的相邻作业与文件读取共 100 次返回 404，200 份停止证明齐全。一次 `storage_quota_busy` 和四次 `raw_download_busy` 在重试后成功。第二轮共同窗口内有 27 次模型上游请求和 2 个已登记的求解作业根活动，CalculiX 子进程登记到停止的重叠计数为 0。 [完整复验](../../tmp/ff-j21-mixed-7du_gbpp/mixed-50.json)

| 指标 | 上轮 p95 | 本轮 p95 |
| --- | ---: | ---: |
| REST，成功率均为 100% | 6,603.9 ms | 5,540.6 ms |
| SSE 首段正文 | 20,583.5 ms | 19,738.3 ms |
| 上下文就绪 | 19,340.0 ms | 18,790.2 ms |
| 单文件长期记忆读取 | 7,142.9 ms | 4,607.5 ms |
| 首轮拖动完成帧 | 539.8 ms | 504.8 ms |
| 上游活动期间拖动完成帧 | 533.3 ms | 432.4 ms |

本轮真实 DeepSeek 从实际发出至收到首段正文 p95 为 1,437.2 ms。按 2 秒间隔在交互起跑至最后一个首段之间取得 10 次 PostgreSQL 采样。瞬时峰值为 35 个连接，其中不同采样的事务内空闲峰值为 30、执行中峰值为 10、锁等待峰值为 2，未采到 PostgreSQL I/O 等待。接下来核对应用持有空闲事务的调用位置和连接池等待。SSE 2,000 ms 及拖动 200 ms 仍待达到。 [直接数据库采样](../../tmp/ff-j21-case-counter-20261126/mixed-pg-activity.txt)

隔离容器、卷、网桥均已回收，原 11 个运行容器标识一致。C 盘可用空间由 39,758,675,968 B 变为 39,759,876,096 B，Docker 构建缓存仍为 5.857 GB，本轮回收 0 B。 [磁盘读数](../../tmp/ff-j21-case-counter-20261126/disk-after.json) · [Docker 占用](../../tmp/ff-j21-case-counter-20261126/docker-after.txt)

### 2026-11-26 本地文件读取调度复验

本地文件树检查只读取三个文件系统身份标记，Agent 记忆只读取最多 64 KiB。两种短操作共用两个独立读取槽位，完整树扫描和文件摘要仍使用原四个槽位。文件检查保留授权、算例锁、版本比较、时限和取消后排空。S3 读取仍使用原有容量与对象版本检查。 [读取执行器](../server/app/services/case_write.py) · [文件树](../server/app/routers/files.py) · [Agent 文件读取](../server/app/services/agent_storage_capacity.py)

在实际 Windows 文件系统与 SQLite 的隔离 API 中，四个工作线程都完成了实际文件读取并停在受控等待点。共享槽位对照在约 4 秒的观察窗内，两种短操作均未完成，释放后正常返回。调整后，两种短操作在 83.0 ms 内均取得观察结果，完整树扫描仍等到原槽位释放。两轮读回的 65,524 字节记忆摘要一致，均返回 30 条树记录和水位 5。该试验没有调用模型。 [争用实测](../../tmp/ff-j21-metadata-pool-20261126/metadata-contention.json)

Linux 双 API 的同规模复测完成 600/600 次 REST 检查、50 次真实 `deepseek-flash` HTTP 200、50 个 CalculiX 数值与发布核对，以及两轮各 50 次真实终端命令和匹配完成帧。150 次产物读取均返回 200，100 次跨账户访问均为 404，200 份停止证明齐全。模型模拟调用 0 次。 [复测速据](../../tmp/ff-j21-mixed-jiaagodq/mixed-50.json)

| 指标 | 上轮 p95 | 本轮 p95 |
| --- | ---: | ---: |
| REST，成功率均为 100% | 5,540.6 ms | 5,250.9 ms |
| SSE 首段正文 | 19,738.3 ms | 18,187.6 ms |
| 上下文就绪 | 18,790.2 ms | 16,178.6 ms |
| 单文件长期记忆读取 | 4,607.5 ms | 4,027.7 ms |
| 首轮拖动完成帧 | 504.8 ms | 346.3 ms |
| 上游活动期间拖动完成帧 | 432.4 ms | 399.7 ms |

本轮验收脚本退出码为 1。前四个求解子进程在交互起跑前已结束，第二轮拖动窗口在起跑后 7.622 秒结束，下一组求解作业根最早在 7.721 秒开始。该共同窗口观测到 4 次真实上游调用和 0 个作业根，仍需让实际终端和拖动负载覆盖连续的求解批次。PostgreSQL 32 次分类采样中，事务内空闲峰值为 32，最后执行的语句涉及用户、工作区、算例、事件和作业等多条路径。该采样无法单独归因。SSE 2,000 ms 与拖动 200 ms 性能目标均待达到。 [原始时序](../../tmp/ff-j21-mixed-jiaagodq/mixed-50.json) · [数据库分类采样](../../tmp/ff-j21-metadata-pool-20261126/pg-staged-counts.txt)

隔离容器、数据卷及网络均已回收，原 11 个 Docker 服务标识一致。C 盘可用空间由 39,759,392,768 B 变为 39,756,668,928 B，Docker 构建缓存 5.857 GB，回收 0 B。 [磁盘读数](../../tmp/ff-j21-metadata-pool-20261126/disk-after.json) · [Docker 占用](../../tmp/ff-j21-metadata-pool-20261126/docker-after.txt)

### 2026-11-26 五类操作的实际重叠复验

初轮终端和拖动仍按同一起点发起。收到实际 DeepSeek HTTP 200 后，各账户在原连接上继续执行真实 PTY 命令和鼠标拖动，直到 REST 或 Agent 批次完成。每条连接串行操作，两次操作间隔至少 0.5 秒，最多追加 16 次。保留全部样本和错误。k6 记录每次通过业务检查的 REST 请求区间。拖动从移动 RPC 发送计到同序号完整 JPEG，模型从实际收到上游 HTTP 200 计到读取结束。求解区间绑定同一尝试和子进程 ID，从持久 `attempt.child_running` 登记计到核验停止。按单笔区间取交集，批次中的空闲间隔不计入。 [混合验收入口](../server/live_llm_j21_mixed_acceptance.py) · [REST 采样](../tools/k6-mixed-reads.js) · [求解登记](../server/mixed_load_calculix.py)

双用户试跑完成 24 次 REST 检查、2 个真实 DeepSeek 回合和 2 个通过数值比对的 CalculiX 作业。两次原生子进程登记均与其停止回执匹配。小规模试跑没有观测到五类区间重叠，50 用户验收要求至少存在一次真实交集。 [双用户回执](../../tmp/ff-j21-mixed-hr3qx24x/mixed-2.json)

50 用户功能复验通过。600 次 REST 请求的业务检查全部成功，50 次真实 `deepseek-flash` HTTP 200 均有独立完成标识。首轮完成 50 次 PTY 命令和 50 张匹配完成帧，后续各有 50 个用户参与，共完成 337 次 PTY 命令与 800 张匹配完成帧。50 个 CalculiX 作业通过独立位移计算、可变路径与归档字节及 SHA-256 核对，最大位移绝对误差 3.34×10⁻⁸。6 次 `storage_quota_busy` 重试后成功，150 次产物读取均为 HTTP 200。100 次跨账户检查返回 404，200 份停止证明和 50 条求解子进程运行登记齐全。模型模拟 0 次。 [50 用户原始回执](../../tmp/ff-j21-mixed-t1r53_ch/mixed-50.json)

五类区间共有四段交集，累计 **999 ms**。涉及 5 个已登记的求解子进程和 8 个作业根。有 48 次真实上游流与 REST、PTY、拖动共同活动。独立时间扫描器重新计算了相同的四段交集。Docker 与 Windows 时钟的同机回读差在本次样本中约 0.4 ms，将每条 Windows 交互区间的两端各内缩 2 ms 后，交集累计仍为 999 ms。该证据覆盖已登记的原生求解进程活动区间。 [独立核验](../../tmp/ff-j21-sustained-overlap-20261126/overlap-verification.json) · [时钟核对](../../tmp/ff-j21-sustained-overlap-20261126/clock-late.json)

| 50 用户持续混合负载 | p95 | 验收要求 |
| --- | ---: | --- |
| REST | 12,354.0 ms | 记录延迟与成功率 |
| SSE 首段正文 | 34,377.7 ms | 2,000 ms |
| 上游实际发出至首段正文 | 1,623 ms | 诊断指标 |
| 首轮拖动匹配完成帧 | 469.9 ms | 200 ms |
| 持续拖动匹配完成帧 | 476.4 ms | 200 ms |

新增的连续操作提高了并发负载，上表记录该持续场景。SSE 和拖动性能目标仍待达到。两套 API cgroup 内存峰值分别为 5,977 和 5,030 MiB。隔离容器、卷和网络均已清理，原 11 个运行容器标识一致。C 盘可用空间由 39,755,595,776 B 变为 39,758,102,528 B，Docker 构建缓存 5.857 GB，回收 0 B。 [磁盘读数](../../tmp/ff-j21-sustained-overlap-20261126/disk-after.json) · [Docker 占用](../../tmp/ff-j21-sustained-overlap-20261126/docker-after.txt)
