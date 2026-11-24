# MOOSE 与 Code_Aster 运行验收

2026-11-21 在本机 `vibeflow-linux` 的 D 盘 WSL 磁盘内完成安装和真实算例。两个求解器由无登录权限的 `vibeflow-solvers` 服务账户运行，实际记录 UID 为 999。环境位于 `/opt/vibeflow-ff-c08`，MOOSE 和 Code_Aster 各用独立的 Conda 前缀。安装日志、发布包摘要、实算输入和输出文件摘要均可复核。[验收记录](../../tmp/moose-code-aster-native-validation-20261121.json)

## MOOSE

使用 INL 官方预编译 `moose=2026.11.13=mpich`，运行时报告框架提交 `08315af82e`。输入为长度 1、10 个单元的一维稳态扩散，左端为 0，右端分别为 100、200。通过 `moose-opt -i line.i` 计算第一档，对第二档指定 `BCs/right_value/value=200 Outputs/file_base=line_200`。CSV 的中点值分别为 50 和 100。通过 `ncdump` 实际读回 Exodus 的 11 个节点，两档与解析解最大绝对差为 `1.42e-13`。[官方预编译安装说明](https://mooseframework.inl.gov/getting_started/installation/moose_conda_binary.html) · [扩散输入](../server/examples/moose/thermal/line.i) · [求解日志](../../tmp/moose-thermal-native-20261121/native.log)

## Code_Aster

使用 conda-forge 的 Linux 串行构建 `code-aster=18.1.11=py312_nompi_heb57605_2`。主机 Windows 10.0.19045 上的 Conda 24.1.2 将虚拟包 `__win` 报告为 0，无法解析同版本 Windows 构建所需的 `libboost`，因此使用已有 WSL。输入为长度 1 m、面积 0.01 m²、杨氏模量 210 GPa 的两节点杆，轴向荷载为 2.1 MN。用 `run_aster axial.export` 运行后，`.mess` 和 MED 位移场的读回值都为 `0.0009999999999999998 m`，解析值为 `0.001 m`，绝对误差为 `2.17e-19 m`。[conda-forge 构建](https://anaconda.org/channels/conda-forge/packages/code-aster/files) · [run_aster 文档](https://codeaster.readthedocs.io/en/latest/devguide/run_aster/run_aster.html) · [算例输入](../server/examples/code_aster/axial/axial.export) · [通过日志](../../tmp/code-aster-axial-native-20261121/native-ext4.log)

直接在 `/mnt/d` 求解时，Code_Aster 已生成原始数值，但 `shutil.copy2` 在保留输出时间戳时返回 `PermissionError`。把三个输入复制到 WSL 的 ext4 工作目录，在该目录运行，再按字节回传 `.mess` 和 `.rmed`，退出码为 0，`DIAGNOSTIC JOB : OK`。[首轮日志](../../tmp/code-aster-axial-native-20261121/native.log) · [复验命令](../../tmp/code-aster-axial-native-20261121/run-native-ext4.sh)

独立验收用 WSL UID 65534 启动单独的挂载、进程和网络命名空间，仅将所需系统目录及求解器运行库按只读方式挂入。启动 Code_Aster 前确认 `/mnt/d` 不可见、有效 capabilities 为 0、`NoNewPrivs` 为 1、IPv4 路由数为 0。实际 MED 端点位移仍为 1 mm。首轮尝试挂入整个 `/usr` 时被 WSL 拒绝，缩小到求解所需目录后通过。[独立隔离验收](../../tmp/code-aster-sandbox-native-20261121/validation.json)

## Code_Aster 平台提交

上传 `.export` 及其中列出的 `.comm`、`.mail` 或 MED 网格后，向 `POST /api/v1/cases/{id}/code-aster:solve` 提交 `{"input_path":"C-run/code-aster/axial.export"}`。可附 `Idempotency-Key`。当前支持串行作业，`.export` 需声明求解时限、内存限制、一个 `.comm` 和一个 `.mess`。平台封存列出的输入，按 `.export` 中的声明发布 `.mess`、MED 及支持的表格文件。取消和按当前输入重新计算走通用作业入口。`VIBEFLOW_CODE_ASTER` 可配置只读运行库中的 Linux `/opt/.../bin/run_aster`。[算例](../server/examples/code_aster/axial/axial.export) · [上游 .export 说明](https://codeaster.readthedocs.io/en/latest/devguide/run_aster/run_aster.html#export-export-file-object)

当前验收的 Windows API 通过 WSL UID 65534 启动受管进程。`.comm` 在仅有必要系统文件、运行库及作业副本的文件系统中执行，使用独立网络、进程和挂载命名空间。求解子进程禁用提权，退出后收取普通成果文件。首轮在清空继承环境后因缺少隔离账户的主目录记录无法启动，补齐隔离环境自己的账户定义后通过。[首轮和复验记录](../../tmp/code-aster-platform-validation-20261121.json)

SQLite 文件系统和 PostgreSQL 认证 SeaweedFS 共完成四个真实拉伸作业和两次运行中取消。两档 MED 端点位移为 1 mm 和 2 mm，与解析解最大差 `4.34e-19 m`。算例内核验主机盘不可见、有效 capabilities 为 0 和 `NoNewPrivs` 为 1。输入越界声明在提交时被拒绝。重新计算封存了新载荷的 `.comm`，取消核验父子进程停止并保留上一版 MED 原字节。真实 DeepSeek 4 次均为 HTTP 200，模拟 0。[真实作业与模型证据](../../tmp/code-aster-platform-validation-20261121.json)

## Code_Aster 计算库续算

独立 WSL 算例用 `FIN` 将原模型保存为计算库，使用 `POURSUITE` 从原库恢复网格、模型、材料和截面。在恢复的模型上施加 4.2 MN，MED 文件端点位移为 2 mm，原库四个文件的 SHA-256 在续算前后一致。[独立续算记录](../../tmp/code-aster-restart-native-20261121/validation.json) · [上游状态保存说明](https://codeaster.readthedocs.io/en/latest/devguide/code_aster/Commands/list.html)

平台源作业用 `R base state R 0` 发布 `state_base.zip`。续算 `.comm` 使用 `POURSUITE`。在 `.export` 用 `R base continued-state D 0` 读取原库，或用 `DR` 同时保存新库。提交 `/code-aster:solve` 时带上 `restart`，其中 `publication_id` 指定原发布，`checkpoint_path` 指定原 ZIP 在算例中的路径。平台从该发布封存原字节，支持选中已经被后续版本替代的发布。同一次提交生成的新库使用独立成果路径。当前支持同一 Conda 求解器构建的串行计算库，原生库和 ZIP 均以 128 MiB 为上限。[续算输入](../server/examples/code_aster/axial/continue.export) · [续算命令](../server/examples/code_aster/axial/continue.comm)

SQLite 文件系统和 PostgreSQL 认证 SeaweedFS 共完成 8 个真实作业。两套环境分别在 210 GPa 原库之后发布同名的 105 GPa 新库，再指定历史发布续算。续算从旧模型得到 2 mm 位移，与从头计算的 MED 位移场逐值差为 0。原输入库的封存摘要、新库的独立发布、原别名保留、缺少引用的拒绝、同键重放及父子停止回执均经过验证。原生库为 132,714,519 B，ZIP 约 3.5 MB。真实 DeepSeek 4 次 HTTP 200，模拟 0。[平台续算证据](../../tmp/code-aster-restart-platform-validation-20261121.json)

## MOOSE 平台提交

上传 `.i` 后调用 `POST /api/v1/cases/{id}/moose:solve`，请求体为 `{"input_path":"C-run/moose/thermal.i"}`，可附 `Idempotency-Key`。当前支持 `GeneratedMesh`、扩散和基础标量核、定值或通量边界，以及稳态和瞬态执行器。提供样例 `PointValue` 后处理。提交会封存 UTF-8 ASCII 范围内的单文件 HIT 配置，拒绝外部 `!include`、外部网格、动态模块和自选输出路径。结果在输入同级发布为 `thermal_out.csv` 与 `thermal_out.e`。重新计算可通过通用作业 `:repeat` 入口重新封存当前 `.i`。[算例](../server/examples/moose/thermal/line.i) · [API](api.md)

在 Windows API 中，MOOSE 由配置的 WSL 发行版以 UID 65534 执行并受现有 cgroup 管理。`VIBEFLOW_MOOSE` 使用发行版中的实际 ELF 路径。本机默认为 `/opt/vibeflow-ff-c08/moose/bin/combined-opt`，它是官方包中 `moose-opt` 链接的目标。原生 Linux 可以使用 `PATH` 中的 `moose-opt` 或指定 `VIBEFLOW_MOOSE`。平台在提交和执行前核对可执行文件摘要，验证签收文件、封存输入、CSV/Exodus 和进程停止回执，再提交不可变成果。[配置检查](../server/app/services/moose.py)

SQLite 文件系统与 PostgreSQL 认证 SeaweedFS 均完成两档真实扩散和一次真实瞬态取消。实际发布的 CSV 中点值分别为 50、100，Exodus 的 11 节点最大误差为 `1.42e-13`。重复幂等键只返回同一作业。从当前输入重跑时使用更新后的边界值。拒绝外部引用与取消都保留上一版成果。正常完成与运行取消都有父子进程的停止回执。最终矩阵 4 次真实 DeepSeek 均返回 HTTP 200。早一轮文件系统实算另有 2 次真实 DeepSeek HTTP 200。本轮模拟 0。首轮接口因 I/O 线程参数传递错误返回 500，修复后用新隔离环境重跑。[最终验收与失败留档](../../tmp/moose-platform-validation-20261121.json)

## MOOSE 检查点续算

在瞬态 `.i` 的 `Outputs` 设置 `checkpoint = true`。平台随 CSV 和 Exodus 发布 `<输入名>_checkpoint.zip`，包含最新串行检查点的网格、状态和元数据。要续算，上传一个瞬态 `.i`，保留原网格及变量声明，并设置新的结束时间。调用同一个 `/moose:solve`，在 `input_path` 之外传入 `restart`，包含原作业的 `publication_id` 与 `checkpoint_path`。例如，从 `C-run/moose/part_checkpoint.zip` 续到 `C-run/moose/continued.i`。可选旧发布。若两次都生成检查点，续算输入要使用另一个路径，以便原检查点与新检查点分别封存。[瞬态输入](../server/examples/moose/thermal/transient.i)

WSL 的 Windows 挂载目录拒绝 MOOSE 写检查点时保留文件时间戳。平台复用已有的非特权 cgroup 管理，在作业的私有挂载命名空间内生成或解包检查点，再将结果复制回封存作业目录。临时文件系统随该命名空间释放。重新计算发布到独立成果，使用 MOOSE 的 `Problem/restart_file_base` 恢复原状态。原生预检发现 `--recover` 面向追加原 Exodus 文件，在全新的结果目录会尝试打开缺失的旧文件。[MOOSE 重启说明](https://mooseframework.inl.gov/application_usage/restart_recover.html) · [首轮挂载失败](../../tmp/moose-restart-native-20261121/full.log) · [原生预检](../../tmp/moose-restart-native-20261121/ext4-restored.log)

SQLite 文件系统和 PostgreSQL 认证 SeaweedFS 的最终矩阵共 8 个实际作业。两套环境均以连续四步作对照，在发布 `t=0.3` 的新检查点后，仍从指定的历史 `t=0.2` 检查点恢复并运行到 `t=0.4`。恢复段的 CSV 中点值与对照相同，Exodus 11 个节点逐时刻比较的最大绝对差为 0。封存摘要、同键重放、同路径冲突与父子停止回执一起核验。首轮 CLI 使用了已安装版本不支持的选项，认证 S3 首轮也因没有物化发布文件而返回 500，两处修正后通过。最终矩阵真实 DeepSeek 4 次 HTTP 200，早一轮文件系统 2 次 HTTP 200，本轮模拟 0。[最终证据及失败留档](../../tmp/moose-restart-platform-validation-20261121.json)

FF-C08 继续按五个后端子任务验收目标部署、界面路径及原计划的剩余物理/并行组合。[开源后端子任务](FF-C08-solver-delivery.md)
