# FF-C06A 手工 OpenFOAM 并行求解

算例所有者可在作业入口 `POST /api/v1/cases/{id}/jobs` 提交自己的 WSL 命令。网格和分解选择 `type=mesh`，并行求解和重构选择 `type=solve`。每次作业有自己的输入快照、真实执行日志、停止回执和成果发布。上一步成功后，由所有者显式提交下一条命令。手工命令发布为 `case-command`，其读回路径是算例工作副本和作业日志。

手工步骤使用算例自己的 `system/decomposeParDict`。例如两分区简单分解设 `numberOfSubdomains 2`，在 `coeffs` 指定 `n (2 1 1)`。在已生成网格的运行目录分别执行 `decomposePar`、`mpirun -np 2 icoFoam -parallel` 和 `reconstructPar -noZero`。`-noZero` 保留用户提供的初始场。当前 WSL 验收使用 Open MPI 4 专用的独立会话与共享内存目录，选择 PMIx `hash` 后端。[OpenFOAM MPI 运行时说明](mpi-publication.md)

自动生成分解配置和自动编排三阶段由 `mpi:run`、注册 MPI 工作流及其专项验收承担。[自动 MPI 的既有证据](mpi-repeat.md)

## 实际手工验收

- 在独立 SQLite／本机文件系统算例中，通过正式 WSL 作业手工提交 `blockMesh`、`checkMesh` 和单独的 `decomposePar`。分解生成 `processor0`、`processor1`，分别核对 `constant/polyMesh/points`、`0/U` 和 `0/p` 的 SHA-256。[网格日志](../../tmp/manual-mpi-c06a-7zbd8f0p/mesh.log) · [分解日志](../../tmp/manual-mpi-c06a-7zbd8f0p/decompose.log)
- 故意提交三个 rank 的手工命令。HTTP 接受作业，实际 `icoFoam` 报告两个分区与三个 rank 不符，触发 `MPI_ABORT` 并退出 1。作业的发布状态为 `failed`，没有回传部分场，原六份分区输入保持原摘要。用两个 rank 重新提交后，两个分区均写出 0.5 的速度场，等待下一条单独的重构命令。[错误 rank 日志](../../tmp/manual-mpi-c06a-7zbd8f0p/incorrect-ranks.log) · [正确 rank 日志](../../tmp/manual-mpi-c06a-7zbd8f0p/correct-ranks.log)
- `reconstructPar -noZero` 生成 0.1、0.2、0.3、0.4、0.5 五个时刻的 U 和 p。按原 400 单元、100 步串行结果比较，末时刻 U 归一化最大误差为 `1.00×10⁻⁶`，原限值为 `1×10⁻⁵`，p 的同口径误差为 `2.06248×10⁻⁶`。十份重构场、六份分区输入和五份作业日志的摘要均已复核。[重构日志](../../tmp/manual-mpi-c06a-7zbd8f0p/reconstruct.log) · [串行基准](../../tmp/icofoam-c05-ghkszg16/icofoam-contract.json)
- 他人提交命令、修改字典、取消作业及读取日志、分区和重构场均返回 404。5 项检查通过，5 个原生作业均以 UID 65534 执行且停止，5 个 Linux 作用域均有回收事件，隔离 API 已退出。真实模型调用 0，模拟模型调用 0。[正式回执](../../tmp/manual-mpi-c06a-7zbd8f0p/manual-mpi-contract.json) · [入口源码](../server/manual_mpi_contract_acceptance.py) · [最终审查清单](../../tmp/review-FF-C06A-manual-mpi-v3-20261119.json)

### 补验中保留的首轮证据

- 首轮 HTTP 夹具在 30 秒时中断等待。已持久保存作业和原生范围准备记录，隔离 API 随夹具退出。按原启动身份复查，记录中的三个 Windows 进程均已不存在，同启动代次和 inode 的分配目录仍在，Linux cgroup 为空。未获得该轮的正式作业终态。后续仅调整手工命令提交的客户端等待，任务墙钟预算保持 120 秒。[首轮结果](../../tmp/manual-mpi-c06a-ze3ri8ud/manual-mpi-contract.json) · [独立范围检查](../../tmp/manual-mpi-c06a-ze3ri8ud/first-scope-note.json)
- 第二轮实际完成网格和两分区分解。三 rank 作业触发 OpenFOAM 的分区数与 rank 数不符诊断和 Open MPI `MPI_ABORT`。夹具原先匹配了其它词形而提前退出。改用当前工具的确切诊断，保留原错误作业、已停止回执和未发布的现场。[第二轮结果](../../tmp/manual-mpi-c06a-nm9a16o2/manual-mpi-contract.json) · [原生错误日志](../../tmp/manual-mpi-c06a-nm9a16o2/incorrect-ranks.log)
