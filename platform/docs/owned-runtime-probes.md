# N4-03.c：全局能力探测的进程归属

最终验证：专项SQLite/临时PostgreSQL各25项、迁移各40项约束与带数据往返/历史降级保护、原请求各14项、文档准备各16项，core14/14及4套隔离运行时兼容通过。769文件基线及运行来源见tmp/mainline-N4-runtime-evidence。

日期：2026-11-03。`GET /api/v1/runtime/capabilities` 的 Python 依赖和 WSL 工具探测已接入 NativeRequest。主线完整范围仍为 53 项，全功能仍为 295 项；本单元没有关闭 N4-03 的会话、Agent 和专业入口，也没有关闭资源预算、统一停机或恢复阶段。

## 用户范围和执行顺序

全局能力检查没有工程对象。两种新增用途为 `runtime.python_probe/python_runtime_probe` 和 `runtime.wsl_probe/wsl_runtime_probe`，均绑定已认证用户，`case_id` 必须为 NULL，不创建算例、Job、attempt 或发布代次。报告、LaTeX 和 SyncTeX 请求继续要求有效的 case，不能因允许全局请求而失去工程归属。

认证后先释放数据库读事务，再并行启动 Python、WSL 和存储探测。每个原生探测采用既有的 preparing 提交、实际出生身份独立核验、running 提交、启动门放行、停止与实际退出核验、持久回执和请求终态协议。Windows 进程保留所属 Job 和实际成员退出证据；WSL 同时绑定配置发行版和真实原生出生身份。重型包只在所选 Python 子进程导入，API 事件循环继续处理健康及其他请求。

所有探测结束后重新读取用户状态。用户已被禁用时返回 401，不返回计算出的能力结果。CLI discovery 继续通过文件路径发现工具，不运行 CLI 或读取其凭据；实际 Agent／CLI 调用属于 N4-03.g。

## 固定用途和能力语义

Python 使用管理员 Setup 中的 Python 路径，以 `-I` 隔离执行固定 helper，保留该解释器的 site-packages。模块列表固定为 vtk、trame、wslink、cadquery、numpy、matplotlib、fastapi、sqlalchemy、httpx；私有通道不能指定额外模块或任意 argv。解释器和模块列表必须与本次登记的 profile 完全相同。模块导入失败用该模块的 NULL 表示，解释器／调用失败用 `reachable=false`、全部模块 NULL 和错误类别表示，不退回 API 自己的 Python 导入结果。

WSL 使用捕获的发行版和官方目录下的 OpenFOAM bashrc，通过固定 bash 内容加载配置，再检查 blockMesh、wmake、latexmk、gmsh 和 OpenFOAM 版本。配置路径通过独立位置参数传递，加载前清空该位置参数，避免 OpenFOAM 将自身路径再次作为附加配置加载。加载失败时，即使继承的 PATH 中有同名程序，openfoam 和 wmake 也不能被标记可用。正常返回的 `reachable=true` 只说明所属 WSL 调用成功；某个工具不存在仍如实返回 false。

保留原有 version／packages、wsl、storage、database、binaries、CLI、环境注册表及阶段检查字段，Python 和 WSL 分别增加自己的 request_id 以追踪所属记录。Pandoc 文件发现使用 Setup 选定路径。工具可用性不代表已经执行或验证全部专业功能。

## 取消、边界和故障

共用 ASGI receive 等待者处理真实 HTTP 断线。取消会通知两个所属范围，并等待实际进程停止、回执处理和同步线程结束；存储线程也等待其本次写读删操作收尾。取消线程等待本身不等于进程已退出。

Python 原生调用期限 45 秒，同步等待 65 秒；WSL 外层 30 秒、内部 bash 15 秒、同步等待 50 秒。原生调用日志上限 1 MiB，WSL 内部 stdout／stderr 各 64 KiB，固定配置最多 4 KiB。helper 只输出一个具有固定 schema 的结果帧；重复或形状错误的帧不能成为能力结果。整次磁盘／内存／并发配额仍由 N4-05/06 继续实现。

出生身份、发行版、登记提交、停止核验或持久回执失败均返回降级结果；错误只公开类别，不返回私有能力凭据。停止回执丢失时保留缺失证据；请求终态提交失败时保留 active，而不是补造终态。专项还注入了未抛出异常但 verified=false 的停止回执：请求完成校验会拒绝提交成功，保留 active、空结束时间和未核验回执，不能把工具返回值当作完成凭据。API 猝死后由 OS 所属范围结束真实工具，新 API 只从持久台账读取；过期活跃记录投影为 recovery_required，不自动重放或凭 PID 补造停止回执。

## 迁移和验证

迁移 `ad42f9b3e8c5` 前置 `9c31e8a2d7b4`：native_requests.case_id 可为空，新增两类 kind、用户／用途／创建时间索引，并以 CHECK 强制全局请求无 case、文档请求有 case。业务表仍为 23 张。有全局请求历史时，降级到强制 case 的前一版本会在 DDL 前拒绝；不静默删除记录。双库迁移验证还保留已有 SyncTeX 历史的降级保护和报告记录逐字段往返。

专项 [runtime_probe_smoke.py](../server/runtime_probe_smoke.py) 使用独立 API、文件存储、SQLite 及命名临时 PostgreSQL，调用真实配置 Python 和 WSL 工具。故障 app 只在测试临时目录存在，保持生产所属启动门和出生核验；WSL bash 的身份通过所属根之外的独立探针读取，不把 namespace 内的局部 PID 当成系统 PID。

验证覆盖正常能力、匿名不分配、错误 host／native 出生身份、错误 nonce／发行版、登记失败、profile 拒绝、两类超时、输出超限、重复结果帧、未核验停止、停止及终态提交失败、心跳失败、暂停时健康响应／私有通道错误凭据、HTTP 断线、Setup 解释器与 Pandoc 路径、所选解释器消失不回退、OpenFOAM 缺失不误报、用户范围、owner 猝死、新 API 持久读取和返回前禁用用户。外部 Windows／WSL 哨兵必须保持存活，全程不创建 case 或 Job，API 不新增重型模块导入。

最终通过结果及源码基线记录于 `tmp/mainline-N4-runtime-evidence/` 和 [基线台账](mainline-baseline.md)。原开发数据库没有迁移或重载。统一活动接口、全局请求的停机收尾、清理记录保留、缺回执恢复与其余入口继续 N4-07／N5。

实现：[路由](../server/app/routers/runtime.py)、[固定调用](../server/app/services/runtime_probe_execution.py)、[原生 helper](../server/app/services/runtime_native.py)、[请求范围](../server/app/services/native_request_execution.py)、[持久台账](../server/app/services/native_request_records.py)、[迁移](../server/migrations/versions/ad42f9b3e8c5_global_runtime_request_scopes.py)。
