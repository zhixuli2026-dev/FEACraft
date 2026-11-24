# FloTHERM 转换的独立执行与结果发布

## FF-I08 原生网格门禁验收

2026-11-23。新生成的纯导热算例在 `Allrun.pre` 校验预检、实际执行 `blockMesh`、`topoSet`、`checkMesh` 并检查热源单元区。任一项失败会保持 `mesh_gate.json` 为失败状态并记录原因。`Allrun.solve` 和续跑入口要求已通过的网格门禁。空热源区和不具备纯导热条件的原始模型均由实际命令拒绝。旧生成算例仍可用 CLI `check` 读取。

转换作业从原始 ECXML 到文件系统、认证 SeaweedFS 实际发布各 24 个生成文件。原样下载后用非特权 WSL 的 OpenFOAM v2406 重算。两个下载版本和本地 CLI 版本均实测 1,000 个热源单元、1,200 W 输入，最终温度场 SHA-256 相同。多区域结果另核对带圆柱热源的密闭外壳，`checkMesh` 通过四个区域，实际网格接面与生成清单一致。真实模型调用 0，模拟调用 0。[本地和拦截](../../tmp/FF-I08-mesh-gate-20261123/native-first/gate-result.json) · [文件系统](../../tmp/FF-I08-mesh-gate-20261123/fs-raw/gate-result.json) · [认证 SeaweedFS](../../tmp/FF-I08-mesh-gate-20261123/s3-raw/gate-result.json) · [T09 核对](../../tmp/FF-I08-mesh-gate-20261123/t09-validation.json)

2026-11-05。本批完成本地文件存储下的 IR 转换链路；辅助库编译仍是下一项，不据此关闭整个 N2-03 或 FloTHERM 功能域。

`POST /api/v1/cases/{case_id}/flotherm:convert` 现在使用 `flotherm.convert@1` 处理器。提交时封存用户文件、IR、可选 settings 和生成器文件哈希；转换在本次 attempt 的独立 `run` 目录执行。原有路径参数、异步 Job 返回、日志、非空输出目录拒绝覆盖以及跨用户权限保持兼容。同一幂等请求在输出已存在后仍返回原 Job。

此前转换直接写入用户输出目录，取消或失败可能留下半成品。现在先验证完整生成物和所属 Job/attempt 回执，再使用已有不可变 bundle、发布代次、head 与有条件的兼容文件写入。转换失败、取消、源文件变更或用户在目标目录新增文件时，不发布到用户目录；私人运行目录中的诊断和半成品保留。完整取消产物的网页查看体验仍待后续验收。

验证保持原有 IR、settings、OpenFOAM 字典、覆盖报告、运行脚本和 required-overrides 逻辑。验证器在自己的临时目录重建确定性转换，与实际输出的完整文件清单、内容和可执行位比较，拒绝删改原始输入、跨目标目录写入、缺件、损坏字典或伪造回执。不支持的各向异性等物理模型继续给出明确的 preflight blockers；转换生成了报告不等于求解条件已满足。

同时修正最终生成标记：`.flotherm2foam-generated` 现在登记转换期间创建的全部文件，包括后续追加的 IR、覆盖报告和 Allrun 脚本，避免仅登记初始 case 生成物。CLI 原有 `--force`/未登记文件保护没有放宽。

验收证据：

| 范围 | 已核实行为 |
| --- | --- |
| SQLite、PostgreSQL 各 11 项 | 正常转换、settings 功率覆盖及含空格/元字符路径、不支持物理模型的阻塞报告、无效 IR/settings、损坏回执/实际产物、源/目标变更、取消、API 重启后的已验证结果恢复 |
| 真实 worker 的 3 项中断 | 在真实 generate_case 返回后设置明确测试暂停，取消/超时/API 退出均使所属进程退出；用户输入保留、半成品只在私人目录，外部哨兵进程不受影响 |
| 旧接口提交 16 项 | 包含双 API 幂等提交；FloTHERM 转换记录绑定新处理器和封存配置，不提前创建用户输出目录 |
| 原 I01 API | inspect、异步转换、日志与结果、跨用户拒绝、非空目录保护继续通过 |
| 实际 OpenFOAM 运行 | 从已发布转换结果启动所属 WSL 作业，实际执行 blockMesh、topoSet、laplacianFoam，生成时间步 100 的 T 文件；这不是完整热物理精度矩阵验收 |

规范证据位于 `tmp/mainline-N2-flotherm-conversion-evidence/`，原始矩阵在 `tmp/flotherm-publication-evidence-rnemz543/result.json`。没有迁移开发数据库或重载 8000；临时 PostgreSQL 数据库已清理。运行中中断夹具修改为观察私有目录中的真实生成文件，未用模拟转换代替实际工作。

当前共 19 个注册处理器。仍需完成 FloTHERM 辅助库编译隔离、旧显式 mesh/solve 命令、S3 默认隔离、完整工具链/外部依赖记录及专业工作流接线。全 case 来源快照是保守失效判断，不宣称已实现最小重算范围；独立目录也不等于操作系统沙箱。API 强杀后诊断目录的产品级回收、完整高级物理模型/所有输入组合/网页流程继续按原计划验收。

主线保持 20 完成、14 进行中、19 待开始，剩 33；295 项全功能完整验收标记仍为 0。本批提供子项实现和证据，不改变原始 53/295 范围。
