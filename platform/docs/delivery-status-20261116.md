# 当前交付与简化推进

主线 **48/53，剩 5 项**：N4-05 资源预算、N4-07 关闭恢复、N7-01 故障矩阵、N7-05 部署交付、N7-06 总体验收。295 项全功能目标继续；本次没有把未完整验收的功能改为完成。

按用户“怎么简单怎么来”的要求，暂不继续扩展启动失联的细分窗口，先修复实际启动和交付入口。原 N4-07 专业 child 更早准备等已知缺口仍保留，不能冒称已经解决。主线验收条件没有新增，也没有删减。

## 本次交付

- 一键启动脚本不再启动 mock LLM 或检查 8990，使用脚本自身路径定位项目；保留清理脚本自身登记的历史 mock 残留。
- Docker worker 改用现有 `app.services.distributed_worker`，接入已有停机收尾逻辑。
- Redis 健康检查使用实际 6380 端口和环境认证，启用 AOF。
- API 镜像移除 mock 文件，加入 Alembic 配置及迁移目录。
- [部署说明](deployment.md)和[开发说明](dev-setup.md)更新真实模型、现有 Redis、迁移、启动和恢复入口；不再建议直接执行未审查的历史回归。

## 验证与限制

Compose 实际渲染检查 9 组通过，PowerShell 语法解析通过。复用 WSL 的真实 Redis 验证带密码健康检查、无认证拒绝、AOF 重启后保留数据，并确认测试进程已关闭。本次真实模型调用 **0**、模拟模型调用 **0**；这些属于配置和实际基础设施验证，不冒充 LLM 验收。

Docker 引擎响应，但 `redis:7-alpine` 拉取失败（registry EOF），未启动 Docker 容器，不能把 WSL 结果当作 Docker 整栈通过。首次 Redis 验证因 Windows 文本管道转换为 CRLF，在启动前失败；改为字节传输后通过。初次 Compose 检查误将输出中的美元转义视为配置错误，修正检查后通过。

证据位于 `../../tmp/simple-delivery-20261116/`，汇总见 `validation.json`。没有重启开发 API、修改开发数据库或运行旧模拟集合。

## 后续顺序

先完成 N7-05 可用部署/恢复说明与可审查发布包，再按原条件处理 N4-05/N4-07 的明确功能缺口，将已有真实证据归入 N7-01，最后完成 N7-06 与全功能逐项验收。一次只推进一个可交付单元；阶段汇报写清新增完成、剩余数量、真实模型和实际基础设施结果。不再用不断增加细分故障场景替代功能交付。

## 可审查包已交付

新增 [打包与验收说明](release-review.md)和 `platform/tools/release_package.py`。本地包 `platform/backups/release-review-20261116.zip` 共 1,333 个文件、95,954,192 字节，SHA-256 为 `b601d453f2fce7374b7052ced416ea1dc9a9bbbf53d1112f2b63ac9876b39eeb`。原源码基线工具会遗漏的 PDF 字符映射、字体、WASM/ONNX 均完整保留，私有配置、密钥、用户数据、依赖目录及模拟模型执行器未入包。

从解包目录实际验证：新 SQLite 升级至 d31f98a254ca（52 张表），与模型结构差异为零；API 启动、266 条 OpenAPI 路径及正常退出通过。前端独立 `npm.cmd ci --offline` 安装 81 个锁定依赖后，完整类型检查和构建通过，213 个 public 资源与构建产物逐字节一致。使用解包源码再次打包，整个 ZIP 哈希相同。

Python 验证复用已有 Windows venv，不声称新机器全部依赖已安装；前端没有复用原 node_modules。首次 npm.ps1 被 PowerShell 策略拦截，改用已有 npm.cmd 后通过，系统策略未改。真实模型 0、模拟模型 0，本轮属于打包、构建、迁移和实际 API 验证。证据为 `tmp/release-runtime-20261116/package-validation.json`。

主线仍 **48/53，剩 5 项**，N7-05 未关闭。下一单元处理部署密钥持久化及目标环境配置/恢复缺口；Docker 整栈、外部工具链与原 N7-05 剩余条件继续保留。本地审查包没有对外发布。

## 部署密钥恢复已接通

后续已接通显式只读密钥文件、API 启动校验、API/worker 共用 Compose secret，以及拒绝覆盖的生成/原密钥恢复工具。真实 API 创建凭据后退出，将原密钥恢复到新目录，再启动 API 并使用同一 profile：DeepSeek deepseek-flash 两次 HTTP200、404 tokens，模拟0。缺失/损坏密钥和覆盖拒绝另列为3项配置/文件检查。详见 [实际验收](provider-vault-persistence.md)。

现有本机默认路径兼容，开发密钥和开发数据库未改。新审查包以 `release-review-vault-20261116.zip` 为准，旧包继续保留。主线仍48/53、剩5，下一步核对目标工具链配置与恢复操作；Docker整栈和原有多实例索引/外部依赖条件仍待验收。

## 工具路径与 Setup 恢复已验证

后续统一了环境变量、Setup 和实际处理器的路径选择，修复阶段检查忽略 Pandoc 配置及 CalculiX 缺省路径误报，并给 Compose 增加独立运行配置卷。自定义安装目录、环境覆盖和恢复备份三阶段的真实报告及最终真实模型回读已验证。两轮共6个报告作业，4次真实DeepSeek、19,902tokens，模拟0；最终模型回读通过。两次套件的预览/清理断言错误保留，独立核验确认62个子执行有停止证明、72个进程身份退出，见 [完整结果](toolchain-deployment.md)。

最新审查包为 `release-review-toolchain-20261116.zip`。主线仍48/53、剩5；接下来按原N7-05条件核对多实例共享索引与存储恢复操作，不继续增加启动失联细分。gmsh缺项、Docker整栈及原有跨主机条件保持未验收。

## 同主机共享索引子项已完成

API/worker 的 Compose 配置已共用 `indexdata:/data/index`。两个实际隔离 API 验证跨进程事件消费、共享搜索、重启继续更新、索引目录丢失后从原文件重建；最终整轮通过，四次实际服务进程均确认退出。真实 DeepSeek 工具回读最终2次HTTP200、8,493tokens；计入前一轮已发生的真实调用，本阶段合计4次、17,052tokens，模拟0。配置检查12组通过；纯文件/进程故障检查与模型调用分开记录。

首轮目录占用、第二轮即时退出断言失败保留。修正测试连接关闭与实际进程退出等待后，第三轮完整通过，没有把先前失败改成成功。证据为 `tmp/live-shared-index-s_98aggj/shared-index-result.json`，汇总为 `tmp/N7-05-shared-index-evidence/validation.json`，说明见[共享索引部署与重建](shared-index-deployment.md)。

N7-05 原待办从5组降为4组：第一组共享目录分支完成。主线仍 **48/53，剩5项**，295项全功能目标继续。实际拓扑为同主机共享SQLite与文件库；Docker整栈、目标PostgreSQL/worker部署及跨物理主机不能从这次结果推定通过。下一单元是原存储恢复、版本保留/回收和混合版本部署操作，不继续增加启动失联细分。最新审查包为 `release-review-shared-index-20261116.zip`。

## 存储版本运维组已完成

新增明确停机使用的 S3 版本预览/回收工具。默认保留30天，原样保留当前文件引用和未知对象，通过 v2 清单及 generation 防止压缩删除记录后的旧请求复活；旧 v1 数据继续可读写，旧程序拒绝已压缩 v2。正式 CLI 已从真实 API 产生的数据中回收旧版本，重启后读写正常；真实 DeepSeek 两次 HTTP200、8,548tokens，模拟0。另用真实 S3 删除加明确响应丢失故障验证中断续做，原文件不变。

证据见 `tmp/N7-05-storage-versions-evidence/validation.json`，操作与混合版本/回退约束见[存储版本手册](storage-version-operations.md)。N7-05 待办从4组降到 **3组**，主线仍 **48/53，剩5项**。FF-J15 已补齐 live-store 回收/压缩，但私有归档淘汰、全部后端和物理跨主机等原范围继续，整项不标完成。

最新审查包为 `release-review-storage-versions-20261116.zip`。下一步处理 N7-05 剩余目标工具链、外部依赖/缓存和正式部署条件，优先补齐实际 gmsh 缺项与环境清单，复用现有 Windows/WSL，不扩展新的权限故障分支。

## Gmsh 工具链已实际打通

现有WSL已安装Ubuntu系统包gmsh4.12.1，新增29包、未升级/移除已有包。平台增加meshio实际能力探测，并交付可复用Gmsh示例。实际非特权作业完成生成、OpenFOAM转换、checkMesh、VTU预览和质量报告：144节点、391四面体，Mesh OK。真实DeepSeek2次HTTP200、8,584tokens，模拟0；作业停止回执/API退出已核验。

首轮缺少fvSchemes的失败保留，补齐示例字典后完整通过。证据见 `tmp/N7-05-gmsh-deployment-evidence/validation.json`，详情见[工具链部署](toolchain-deployment.md)。最新审查包为 `release-review-gmsh-20261116.zip`。

主线 **48/53，剩5项**；N7-05仍有 **3组**待办，本次解决其中明确的gmsh缺项。当前最低阶段检查全部可用，Kimi CLI仍未安装，完整外部工具/缓存和正式部署继续；FF-B17的snappy/cfMesh等原范围未关闭。下一步按实际环境清单处理剩余依赖和目标部署，继续复用已有环境。
