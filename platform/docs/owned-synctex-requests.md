# N4-03.d：所属 SyncTeX 只读查询

最终验证：SyncTeX SQLite/临时PostgreSQL各25项、迁移各35项约束/前版本带数据往返/历史降级保护、请求基础各14项、文档准备各16项、LaTeX全链路各32项，core14/14通过。764文件基线及各批运行顺序见tmp/mainline-N4-synctex-evidence。

日期：2026-11-03。正式 `/synctex:query` 的注册 LaTeX 发布与旧版 PDF／sidecar 两条路径均已接入 NativeRequest。完整主线仍为 53 项、全功能仍为 295 项；N4-03 的 runtime／CLI、PTY／viz、Agent 和其他专业入口继续实施。

## 查询阶段与来源

先在短 case 写协调中选定来源，查询工具运行期间不保留该事务或数据库连接。注册发布先验证清单、PDF／gzip sidecar 字节和主文件归属；显式历史 ID 固定到该版本，不因当前发布指针变化而改查另一个 PDF。旧版来源要求 case 内的 PDF 与相邻 `.synctex.gz`，并从有界 gzip 头部捕获可映射的 `.tex` 文件哈希。

选中的 PDF 和 sidecar 复制到文件存储根下独占的 `.vibeflow-native-queries/<uuid>`，不放在用户算例树或正式发布目录中。目录只允许 `snapshot.pdf`、`snapshot.synctex.gz` 和固定配置三个文件。配置绑定 PDF／sidecar SHA-256、页码和坐标；broker 放行前与原生 helper 放行后分别核对摘要和固定形状。新增一个未选择的 `.synctex` 也会拒绝执行，避免工具自行挑选另一个映射文件。

工具返回后再取得短 case 协调并重新验证用户、共享权限和来源。注册版本继续使用所选不可变源码映射，返回原源码哈希、当前源码是否匹配，以及此刻的 current／stale 信息。旧版没有历史源码清单，因此 PDF、sidecar 或实际映射源码变化会返回 input_stale；查询期间新出现注册发布头也会拒绝旧版结果。

## 原生范围与只读行为

`request_lifecycle.wait → synctex_queries → NativeRequest/RequestExecutor → OwnedProcess → synctex_native → synctex edit`。

新增用途仅为 `synctex.query/synctex_query`，使用配置发行版或所选历史发布记录中的发行版。不能请求任意 argv、编译、编辑器或其他用途。沿用 preparing 提交、实际 host／native 出生身份独立核验、running 提交、启动门放行、停止与实际退出核验，再持久保存结果的协议。

本机 `synctex help edit` 明确说明：未给 `-x` 时可执行 `SYNCTEX_EDITOR` 的内容。原生 helper 现在显式清除所有 `SYNCTEX_*` 环境提示，固定 `edit -o` 只返回位置。故障用例注入一个只会写测试标记的编辑器命令，验证它没有执行，同时真实源码定位仍然正确。工具的候选 sidecar 清理也只可能发生在本次私有快照中，原 PDF、sidecar、源码与发布版本不由查询改写。

请求属于已认证用户及 case，不创建 Job、attempt 或发布代次。共享 viewer 可以查询，但仍不能写文件；查询期间撤销分享，最终返回 404，不泄露已计算的位置。操作记录 succeeded 表示原生调用及清理成功，并不越过结果返回前的权限／来源重验。

HTTP 断线通过共用的 ASGI receive 等待者取消请求，随后等待本次 Windows／WSL 范围停止、持久回执及同步线程结束。该等待者从文档准备服务提取，保留原有工作流状态监视和取消等待语义。超时返回 504；登记、工具输出、清理证据或持久回执失败不能变成成功位置。

## 有界调用与持久模型

PDF 上限 128 MiB、压缩 sidecar 上限 16 MiB；旧版头部最多读取 8 MiB、单行 16 KiB、最多 10,000 个源文件映射。工具 stdout／stderr 分别限制为 128／64 KiB，独立捕获后通过有界结果帧返回。原生工具内部期限 10 秒、外层监督 20 秒、同步请求等待 30 秒。整次磁盘／内存预算、公平并发和记录保留政策继续 N4-05/06。

迁移 `9c31e8a2d7b4` 在已有 NativeRequest kind CHECK 中增加 SyncTeX，用途和 JSON 台账 schema 不变，仍为 23 张业务表。前置为 `8b20d7f1c6a3`。已有 SyncTeX 历史时，直接回退旧约束会被拒绝；迁移不静默删除这些记录。双库验证检查带数据的前一版本往返和阻止有历史的降级。

正常、失败及 HTTP 取消都会清理本次私有目录。API 猝死由 OS 所属范围清理真实工具，数据库不会凭目录或内存状态补造停止回执／结束时间；新 API 对过期活跃记录只投影 recovery_required。私有快照孤儿、已退出但缺回执的恢复、统一活动列表／case 级取消／停机以及审计保留仍由 N5／N4-05／N4-07 推进，不自动重放查询。

## 证据与实际工具

专项 `synctex_execution_smoke.py --postgres` 使用正式 API、实际 WSL SyncTeX、独立 SQLite 和命名临时 PostgreSQL。故障 app 让真实 SyncTeX 等待 Linux 临时 FIFO，原生进程出生身份由所属根以外的独立探针读取；不会把私有 namespace 内的局部 PID 当成外部系统 PID。

覆盖正常与暂停查询、当前源码改动后的冻结定位、私有通道错误凭据、编辑器环境禁用、错误 native 出生身份／发行版、登记失败、登记后配置／PDF／额外 sidecar 改写、输出超限、未核验清理、停止及请求终态回执失败、超时、HTTP 断线、viewer 与权限撤销、旧版 gzip 损坏／源变化／PDF 变化／新注册头，以及实际 API owner 猝死和新 API 持久读取。查询暂停时，另一文件写入必须先完成；外部 Windows／WSL 哨兵保持存活，查询不增加 Job。

最终结果、迁移、文档准备兼容、原请求基础、LaTeX 全链路及 core 回归均以 `tmp/mainline-N4-synctex-evidence/` 的通过结果为准。源码基线包含实现、约束迁移和测试；本批没有迁移或重载原开发数据库，也没有运行全量 offline／writes、CAE 或前端。

实现：[来源选择与结果重验](../server/app/services/synctex_queries.py)、[固定调用](../server/app/services/synctex_execution.py)、[原生 helper](../server/app/services/synctex_native.py)、[共用请求等待](../server/app/services/request_lifecycle.py)、[专项](../server/synctex_execution_smoke.py)、[迁移](../server/migrations/versions/9c31e8a2d7b4_synctex_request_kind.py)。前置见 [独立请求归属](native-request-scopes.md) 和 [文档锁外准备](document-admission.md)。

## 2026-11-19 私有输入节点准入

FF-E10 首轮工作台实际返回 504。持久子进程记录从创建到开始停止为 19.975 秒，与原 20 秒监督期限一致。随后核验到 Windows 范围为空、WSL 原生范围已退出、私有文件清空。同一历史发布在之后的浏览器复验查询成功。工作台自检和失败查询有重叠，现有日志只能确认重叠和实际超时，无法确定自检是唯一原因。[首轮退出记录](../../tmp/e10-synctex-wxgu90u5/cleanup-independent.json) · [浏览器正式记录](FF-E10-latex-diagnostics-synctex.md)

另用相同历史发布的正式 API 验证了已有节点容量边界。隔离存储根实际加入 1 MiB 数据，并将节点上限设为 1 MiB。原路径仍写入 4,023 B PDF、609 B sidecar 和 413 B 设置文件，执行真实 WSL SyncTeX 并返回 200，整个过程没有节点预留。进程停止证据及私有目录退出均通过。[实际基线](../../tmp/synctex-node-capacity-4o1c6r_3/synctex-node-capacity.json) · [执行器摘要](../../tmp/vibeflow-regression-all-20261119-154641-663b18/summary.json)

查询现在在创建私有目录之前申请节点额度，覆盖所选 PDF、sidecar 的实际长度以及已有的 16 KiB 设置上限。预留贯穿快照准备、实际调用和私有目录删除。等待准备线程和原生范围排空后，再释放节点额度。节点测量使用既有存储期限，原有 SyncTeX 工具、外层监督和同步等待期限保持。所选固定发布、工作区来源与共享权限沿用原检查。[私有输入准入](../server/app/services/synctex_queries.py:45) · [设置文件边界](../server/app/services/synctex_execution.py)

修复后，同一低额度返回 HTTP 409 `node_storage_full`，未登记新的 `NativeRequest` 或创建快照目录。外部账号仍返回 404。解除实际压力后，原只读查看者用相同参数命中同一历史发布的 `E-report/latex/main.tex:3`，返回 HTTP 200，原文件摘要一致。实际 WSL 调用前后各观察到 21,016 B 节点预留。删除三个私有文件后该预留仍在，随后归零。持久记录核验原生范围已退出且为空，Windows 子进程身份和隔离 API 也已实际退出。[修复后实际回执](../../tmp/synctex-node-capacity-i5ia4lnf/synctex-node-capacity.json) · [执行器摘要](../../tmp/vibeflow-regression-all-20261119-155224-bb61f7/summary.json)

基线和复验各 1/1，通过时间分别为 43.047 秒和 48.750 秒。两轮使用隔离 SQLite／本地文件系统及已编译的历史原件，以实际 WSL 查询验证私有快照容量和清理。本轮沿用原超时及并发调度，宿主重载下的响应时间仍由原合同处理。C 盘可用空间 22.100→22.091 GiB，D 盘 388.966→388.561 GiB。模型调用和 Docker 写操作均为 0 次。[精确回归入口](../server/synctex_node_capacity_smoke.py)
