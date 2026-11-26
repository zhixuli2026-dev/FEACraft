# Elmer Windows 运行环境验收

2026-11-21，FF-C08 的 Elmer 使用官方镜像提供的 Windows x64 无 GUI、无 MPI 便携包。压缩包为 128,365,334 B，SHA-256 为 `e9072fe8db3c15ae3bef36e3f6d5ac59241376002b1d78eb171c9242756b1fc8`，解压在 D 盘 `third_party/elmer-csc-nompi-20260805`。运行日志报告 26.2，编译日期 2026-08-05。[官方二进制目录](https://www.nic.funet.fi/pub/sci/physics/elmer/bin/windows/)

从如下证据中的算例目录启动便携包的 `bin/ElmerSolver.exe heat.sif`。进程环境指向包根目录的 `ELMER_HOME`，`PATH` 在前面增加包的 `bin`，`OMP_NUM_THREADS` 和 `OPENBLAS_NUM_THREADS` 均为 1。这些设置只用于本次进程。算例有 3 个节点和 2 个线性单元，边界温度分别为 0 和 100，形式为稳态一维传热。

原生进程退出码为 0，写出了结果和 VTU。读取 VTU 的实际坐标和温度，`x=0`、`0.5`、`1` 分别为 `0`、`50.00000000000001`、`100.00000000000001`，与解析解的最大绝对误差为 `1.42e-14`。独立求解器验收的模型调用为 0 次。[输入](../../tmp/elmer-heat-line-lg27zb2h/heat.sif) · [原生日志](../../tmp/elmer-heat-line-lg27zb2h/solver.log) · [验证结果](../../tmp/elmer-heat-line-lg27zb2h/run.json)

## Linux 求解子进程隔离

Linux Elmer 子进程使用私有 PID、挂载和网络命名空间。工作目录为本次尝试，系统运行时只读。部署仍用 `VIBEFLOW_ELMER` 选定可执行文件。有 `share/elmersolver/lib` 布局的发行包，经可执行文件摘要校验后，将入口和模块目录分别只读挂载在 `/run/elmer` 下。运行环境按该目录设置 `ELMER_HOME` 和 `ELMER_LIB`，保留相对库搜索路径。 [Linux 启动器](../server/app/services/agent_shell_linux.py) · [实测入口](../server/linux_solver_isolation_acceptance.py)

2026-11-26，在 UID 65534 的 Docker worker 上，沿用源码构建的串行 Elmer 发行包和实际 PostgreSQL、Redis。本地文件系统和认证 SeaweedFS 各运行两账户并行算例、一次直接启动发行包入口及一次故意越界的续算。两个原生子进程的登记区间分别重叠 3,215 ms 与 3,185 ms，分别使用独立网络。核验了只读模块目录、测试连接被拒、宿主对照文件不可见和跨账户详情 404。 [本地文件系统回执](../../tmp/j16-elmer-9hpcqx3a/result.json) · [认证 S3 回执](../../tmp/j16-elmer-fk35x4q5/result.json)

三档轴向应力分别与 210、420 和 315 MPa 的解析解一致。跨两种存储的最大应力误差为 6.56×10⁻⁷ Pa，最大位移误差为 6.51×10⁻¹⁹ m。检查同时核对实际 VTU 中两个六面体与两个边界四边形、四个网格文件的封存摘要，以及 ASCII 检查点摘要。位置 99 的续算失败后，原结果字节保持一致，S3 另经空缓存 API 独立读回。八个作业的父子进程停止凭据均已核验，模型调用 0 次。 [源码发行包验收](solver-source-integration.md#elmer)

共用启动器的 CalculiX、普通 Linux 终端和 Agent 均通过认证 S3 回归。Agent 使用真实 `deepseek-flash` 收到 2 次 HTTP 200，模拟调用 0 次。原 11 个 Docker 服务已核对，构建缓存 5.857 GB，回收 0 B。Windows C 盘可用空间由 39,770,931,200 B 变为 39,765,893,120 B。 [CalculiX 回执](../../tmp/j16-calculix-9fo3ex1s/result.json) · [PTY 回执](../../tmp/j16-pty-compute-p4uastlc/result.json) · [Agent 回执](../../tmp/j16-agent-linux-o46x020k/agent-compute.json)

## 平台提交与结果

将 `.sif`、`mesh.header`、`mesh.nodes`、`mesh.elements` 和 `mesh.boundary` 放入同一算例。在 SIF 的 `Mesh DB` 中使用算例下的相对路径。通过 `POST /api/v1/cases/{id}/elmer:solve` 提交 `input_path`，响应包含 `job_id`。可用 `Idempotency-Key` 查询同一次提交，用 `GET /api/v1/jobs/{job_id}` 查看作业和产物发布状态。Windows 求解器使用 `VIBEFLOW_ELMER` 配置或本地便携包，运行时能力清单显示发现结果。

当前入口承接串行声明式 SIF，内置过程包括 `HeatSolve/HeatSolver`、`StressSolve/StressSolver` 和 `FlowSolve/FlowSolver`，本轮用热传导做数值验收。SIF 声明 `Output File` 的 `.result`、`Post File` 的 `.vtu` 或两者。原生状态文件位于网格末级名称对应的目录，VTU 可用 `Results Directory` 指定算例相对目录。用户 DLL、MATC/Lua 脚本及外部包含文件当前由入口拒绝。[Elmer 原生状态文件实现](https://raw.githubusercontent.com/ElmerCSC/elmerfem/release-26.2.1/fem/src/ModelDescription.F90)

封存的配置、输入摘要、求解结果、工作进程和求解子进程凭据关联同一次作业。子进程由现有进程监督通道启动。只有成功且通过输出核验的作业才发布不可变成果。运行途中可经 `POST /api/v1/jobs/{job_id}:cancel` 取消，原已发布结果保留。作业操作页面的按当前输入重跑使用同一服务端求解器配置。

## 平台实际验收

SQLite 加文件系统验证原生默认输出目录，PostgreSQL 加认证 SeaweedFS 验证指定嵌套 VTU 目录。两组各通过真实初次求解、已提交幂等键重放、修改边界后重跑和取消中的父子停止凭据。两次重跑的右端温度由 100 改为 50，VTU 中点为 `25.000000000000004`。取消时 Elmer 子进程已进入实际运行，旧 VTU 摘要保留。两组均拒绝外部过程 DLL。

初次 API 测试发现 Elmer 原生状态文件与 VTU 的目录规则独立，已按实际写入行为补齐。取消测试首轮过早读取已受理状态，修正为等待两个停止凭据。首轮中断的子进程单独确认已退出。SQLite 上的 CalculiX/SU2 实际求解回归通过。此批真实 DeepSeek 回读 8 次，HTTP 200，模拟 0 次，临时 API、数据库和 SeaweedFS 均已清理。[实测索引](../../tmp/elmer-platform-validation-20261121.json)

## 从状态文件续算

在 `Simulation` 中指定 `Restart File`，然后通过同一个 Elmer 提交接口启动。Elmer 按网格末级目录查找状态文件。例如，网格目录为 `C-run/elmer/line` 时，`Restart File = File "first.result"` 读取算例中的 `line/first.result`。`Restart Position = Integer 0` 选择最后一个已保存状态。为续算产物指定另一个 `Output File`，例如 `continued.result`。检查点和 SIF 一起封存，原检查点在续算发布后仍可读取。[Elmer 续算文件查找规则](https://raw.githubusercontent.com/ElmerCSC/elmerfem/release-26.2.1/fem/src/ElmerSolver.F90)

`Binary Output = Logical True` 的求解会发布原生 `.result` 和 `.result.pos`。续算使用原生顺序读取二进制 `.result`，并保留原发布的索引。实测本机 Elmer 26.2 通过索引会读错时间，即使其温度场和退出码看似正常。顺序读取恢复了正确时间。越界位置的原生回退会被识别为失败，产物不会发布。[Elmer 状态读取实现](https://raw.githubusercontent.com/ElmerCSC/elmerfem/release-26.2.1/fem/src/ModelDescription.F90)

SQLite 文件系统和 PostgreSQL 认证 SeaweedFS 分别通过 ASCII 和二进制续算。两组都是实际前两步加后两步，与实际连续四步对照。后续时间为 `0.3` 和 `0.4`，最终三个节点温度的最大绝对差值均为 0，中点为 `47.86558295198415`。旧检查点和已发布的二进制索引字节保持一致。等长改写已封存检查点返回 409，恢复原字节后可用同一个请求键重试。第 99 个状态的失败作业取得停止凭据，归档后同案恢复写入。

S3 首轮数值与停止凭据通过后，模型回读夹具撞到了失败归档的存储预约。最终复验先取得同案正常写入，再调用真实模型。加上本地复验和既有 Elmer 初次求解、重跑、取消回归，真实 DeepSeek 共 8 次 HTTP 200，模拟 0 次。已回收临时数据库、对象存储和 API。[实测及源码索引](../../tmp/elmer-restart-validation-20261121.json)

## 独立线弹性核对

2026-11-21，原生 `StressSolve/StressSolver` 计算两个八节点六面体的轴向变形。模型长 1 m，杨氏模量 210 GPa，泊松比为 0，两端指定轴向位移 0 和 1 mm。VTU 的节点坐标包含变形，比较前用位移还原参考位置。独立运行的四个中截面节点位移均为 0.5 mm，最大位移误差为 `2.17e-19 m`，轴向应力与 210 MPa 解析解的最大差值为 `2.09e-7 Pa`。[算例](../server/examples/elmer/axial/axial.sif) · [VTU 数值核对](../../tmp/elmer-axial-elasticity-native-13rhwvb3/result.json)

同一个算例随后用于平台验收。分别将 `axial.sif` 与 `brick` 网格目录上传到算例根目录，提交 `input_path` 为 `axial.sif`。SQLite 文件系统和 PostgreSQL 认证 SeaweedFS 各施加 1 mm 与 2 mm 两档位移。四个真实作业都发布了 `.result` 和 VTU，父进程和求解子进程停止凭据通过。两档轴向应力均与 210 MPa 和 420 MPa 解析解一致，最大应力误差为 `4.17e-7 Pa`。同一个幂等键返回原作业，实际输入的 SHA-256 与封存记录一致。真实 DeepSeek 回读 4 次，均 HTTP 200，模拟 0 次。[平台验收索引](../../tmp/elmer-elasticity-validation-20261121.json)
