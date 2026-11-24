# 尝试的工具链来源记录

2026-11-05。状态：本批验证通过，规范证据在 `tmp/mainline-N2-attempt-toolchain-evidence/`。封存23项、几何工作流17项、查询8项、S3 UDF正向1项；首轮S3断言失败及最终复验分别留存。本单元补齐提交宿主和正式处理器的来源记录，不关闭 N2-03，也不改变 53 项主线和 295 项全功能交付范围。

新尝试不再使用 `solver_versions: handler_not_registered`。输入封存中的 `workspace.toolchain` 使用 `vibeflow.attempt-host.v1`，记录提交宿主的平台、架构、Python 实现与版本，以及解释器路径、大小和 SHA-256。这里的作用域明确为 `submission_host`；执行尚未发生，专业工具版本明确标为 `not_observed_at_seal`。如果解释器文件无法读取，保存 `unavailable` 和异常类型，不虚构指纹，也不因这一诊断信息而阻止原本可执行的作业。

正式处理器绑定后，`handler.toolchain` 使用 `vibeflow.handler-toolchain.v1`，记录处理器名称/版本、执行规格和准备描述的 SHA-256，以及已有的 worker、原生脚本、编译器辅助代码、库、可执行程序和发行版等声明。MPI 的 PMIx 选择、UDF 的 OpenFOAM bashrc、FloTHERM 转换引擎指纹也从原准备配置中关联。记录只读取明确字段，不采集整个环境或凭据。准备描述摘要关联完整原配置，避免只凭一项版本名称混淆不同执行。

这份记录和发布预留在同一数据库事务内保存。claim 时重新按持久执行规格和发布预留计算声明，不一致时返回 `evidence_invalid`，不启动原生进程。输入封存文件保持原字节，绑定处理器不会重写已上传到 S3 的不可变清单。旧记录没有新字段时继续遵循原处理器验证，不为历史记录补造工具链信息。已有工作流详情会返回该处理器记录；本次也为经过 case 归属校验的 `GET /jobs/{id}` 补齐同一记录，普通专业作业可以读取自己的工具链声明。

运行后实际观测仍由各处理器的验证器负责：例如几何产物中的 CadQuery/OCP/Python 版本、网格和求解器的工具链文件、UDF 发布描述内的 OpenFOAM 版本、实际工具路径/指纹与调用 receipt。提交时的声明不能替代这些执行证明；任意 shell 的外部依赖全集也不能靠解析命令可靠推断。因此新声明明确保留 `external_dependency_closure: not_claimed`。

实现路径：`server/app/services/attempt_toolchain.py`、`attempt_workspace.py`、`step_handlers.py`。此次没有数据库迁移、没有变更开发数据库或重载端口 8000。回退可恢复上一份源码包；历史清单按原样读取，旧程序会忽略新增的工具链说明字段，但不再执行本次新增的一致性检查。

验收使用独立 SQLite 和所属文件/S3 存储：

- `attempt_workspace_smoke.py`：独立输入/运行目录、并发、原件保护、重试、清单破坏与权限回归，并检查宿主指纹确实对应当前解释器。
- `workflow_handler_smoke.py`：两个实际 API 和真实 CadQuery；新增声明损坏后 claim 拒绝，以及旧无索引记录仍可执行。重启后的处理器记录保持一致。
- `s3_professional_handlers_smoke.py --group openfoam --kinds udf-integrate --modes normal`：真实 WSL/UDF 数值结果、独立输入、工具链声明、不可变产物和空缓存当前/历史引用。

另对上一阶段一次未归因的 UDF 当前引用 409 做了有限复查：从原封存输入与哈希验证后的不可变产物重建独立 S3 前缀；12 次顺序读取和 6 次三线程读取均恢复出完全一致的原输入。证据位于 `tmp/s3-attempt-store-wx40xz0d/udf-source-probe.json`。这只检查直接来源读取边界，没有重现当时完整 API 时序，原 409 根因仍未解决；产品已保留底层异常日志供下一次故障定位。

剩余要求继续保留：实际外部依赖与编译缓存策略、跨系统权限和完整跨环境验收、旧绝对路径执行计划迁移、孤立准备目录与未知数据库提交恢复。它们按原 N2/N3/N4/N7 范围推进，本次来源记录不能代替环境封闭性或故障恢复验收。
