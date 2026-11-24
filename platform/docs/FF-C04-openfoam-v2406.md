# FF-C04 OpenFOAM v2406 开发环境

现有 `vibeflow-linux` 安装了 OpenFOAM v2406 及开发包。管理员通过运行环境 Setup 选择发行版和 bashrc。受控平台探测在 Windows/WSL 使用非特权 `nobody` 执行。`GET /api/v1/runtime/capabilities` 在该 bashrc 实际加载后核对所选安装下的 `blockMesh` 和 `wmake`，并实际用 `foamEtcFile` 查找该安装的 `etc/controlDict`。所选 bashrc 缺失时，网格和求解阶段显示缺少能力，OpenFOAM 工具和版本字段为空。[配置说明](toolchain-deployment.md) · [原生探测](../server/app/services/runtime_native.py)

同一发行版的 Debian 1912 和 OpenFOAM v2406 可以并存。实际 `/usr/bin/blockMesh` 仍由 1912 提供。正式 `golden_cavity.py` 和 `golden_cavity3d.py` 均通过当前注册工作流使用所选工具链，入口和共用基准实现已无 1912 的固定路径。[二维入口](../server/golden_cavity.py) · [三维入口](../server/golden_cavity3d.py) · [共用工作流](../server/registered_cae_benchmarks.py)

真实工程沿用原回执。二维 400 单元 `icoFoam` 求解经坏边界拒绝、修复和重新发布，五个时刻 U/p 仍与原基准一致。三维 27,000 单元由原正式基准验证场和残差。最小 functionObject 使用 Windows/WSL UID 65534 实际由 `wmake` 编译成新 ELF、加载并参与 `icoFoam` 计算，在真实日志中写出本次唯一标识。非特权手工 MPI 的网格、求解和重构日志也分别记录了 OpenFOAM 2406。[串行实测](FF-C05-icofoam-serial-cavity.md) · [WSL UDF 报告](../../tmp/linux-mpi-udf-20261118/windows-2e9e44c618/result.json) · [UDF 编译和加载](linux-mpi-udf-deployment.md) · [手工 MPI](FF-C06A-manual-openfoam-mpi.md)

## 当前实际验证

- `dpkg-query` 确认真实安装 `openfoam=1912.200626-2build3` 和 `openfoam2406-dev=2406.260127-2`。以 UID 65534 加载 v2406 后，`wmake -show-cxx` 返回 `g++`，`foamEtcFile -show-api` 返回 2406，实际读取 `/usr/lib/openfoam/openfoam2406/etc/controlDict`。`icoFoam` 与 `blockMesh` 从同一实际 v2406 安装发现。[正式回执](../../tmp/openfoam-v2406-c04-zpdfwbkn/openfoam-v2406-contract.json)
- 匿名发起能力探测返回 401，普通成员修改 Setup 返回 403，管理员提交非法格式返回 400。管理员设置格式合法但实际不存在的 bashrc 后，WSL 仍可达，网格与求解能力均不可用。尽管旧 `/usr/bin/blockMesh` 仍在原 PATH，OpenFOAM 版本与路径字段为空。恢复 v2406 配置后，能力和实际文件查找成功恢复。[验收入口](../server/openfoam_v2406_contract_acceptance.py)
- 4 项检查通过，三次 WSL 和三次 Python 能力执行均取得停止回执，隔离 API 已退出。本次模型调用 0，模拟模型调用 0。第一轮包核对的 Bash 夹具启用了全局 `errexit`，厂商初始化脚本提前退出。保留该回执，第二轮单独核验包查询和 bashrc 的实际退出状态。[首轮回执](../../tmp/openfoam-v2406-c04-j9lenazz/openfoam-v2406-contract.json) · [最终源清单](../../tmp/review-FF-C04-openfoam-v2406-v2-20261119.json)
