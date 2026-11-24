# Linux CalculiX 部署与实际求解

2026-11-18：CalculiX 正式求解入口已支持 Linux 容器内 `ccx` 自动发现，并提供可选源码构建镜像。Windows 仍使用原配置或本机既有 2.23 可执行文件。本阶段没有模型回复、模型工具选择或 Agent 推理测试；真实模型调用 0、模拟 LLM 0。

## 构建与配置

先使用当前源码构建 API 基础镜像，再在其上增加 CalculiX：

```sh
docker build -f platform/infra/Dockerfile.api -t vibeflow/api:current platform
docker build -f platform/infra/Dockerfile.calculix --build-arg API_IMAGE=vibeflow/api:current -t vibeflow/api:calculix platform
```

也可选择已带 OpenFOAM/TeX 的当前 API 镜像作为 `API_IMAGE`。Compose 使用 `docker-compose.prod.yml` 加 `docker-compose.calculix.yml`；`VIBEFLOW_API_IMAGE` 指定基础镜像，`VIBEFLOW_CALCULIX_IMAGE` 指定最终镜像，API/worker 必须使用同一构建结果。不要把多个镜像构建覆盖文件叠加当成自动串行构建。默认与自定义镜像两组 Compose 渲染已核验。

Linux 默认从进程 PATH 查找 `ccx` 并解析实际文件路径，能力清单与求解入口共享该逻辑。显式 `VIBEFLOW_CALCULIX` 仍优先，错误的显式路径不会回退到其他程序。Windows 的原默认安装路径和显式配置保持兼容。

镜像使用[官方 CalculiX 2.23 源码](https://www.dhondt.de/ccx_2.23.src.tar.bz2)和[Netlib SPOOLES 2.2](https://www.netlib.org/linalg/spooles/spooles.2.2.html)，在当前 Debian 工具链下编译，链接发行版 ARPACK/BLAS/LAPACK。固定下载摘要为：

| 源码 | SHA-256 |
| --- | --- |
| `ccx_2.23.src.tar.bz2` | `9c88385c10fb04f5dc6c4e98027a51bebdd8aee3920e05190d6c1dd08357d6e7` |
| `spooles.2.2.tgz` | `a84559a0e987a1e423055ef4fdf3035d55b65bbe4bf915efaa1a35bef7f8c5dd` |

构建阶段与运行阶段分开，最终镜像安装运行库并复制 `/usr/local/bin/ccx`。官方预编译 Linux 文件在本次环境缺少 `libgfortran.so.4`，因此没有将其直接放入部署。该探测、下载摘要和失败日志保留于 `../../tmp/linux-calculix-20261118`。

适配当前编译器时，Fortran 使用 `-fallow-argument-mismatch`，SPOOLES 使用 GNU89 C 模式。固定 2.23 源码的 `readnewmesh.c` 第 468 行原为 void 函数中的 `return NULL`，GCC 报错；构建脚本先核验原行，再改为 `return`，没有改动有限元计算公式。上游 `ccx -v` 打印版本后返回 201，镜像版本检查同时核验该返回码和精确版本文本，不能用通常的零返回码判断这条查询成功与否。正式求解仍要求原有成功返回及完整产物证明。

## 实际验收

Linux/S3 和 Windows/S3 使用独立 SQLite、账户、算例和存储桶，经过真实 API 提交、受控原生子进程、冻结输入和成果发布流程。没有将模拟求解器或历史固定输出用于验收。

每端真实运行两个载荷的 C3D8 单元算例：单位立方体，弹性模量 210000、泊松比 0.3，底面固定，顶面四个节点各施加 -1000 或 -2000 的 z 向力。参考结果独立组装八点积分的 24×24 弹性刚度矩阵并求解约束后系统；参考计算只用于比较真实 CCX 输出。

两端两档载荷的最大位移绝对误差均约 `3.33e-8`，满足 `rtol=5e-5, atol=1e-8`；载荷翻倍时位移也翻倍。验收解析 FRD 的实际 DISP 字段，不能以文件存在、坐标非零或进程返回成功代替数值验证。

同时验证含空格/特殊字符的输入路径、`*INCLUDE` 材料冻结、全部发布文件哈希与活动文件一致、重复提交幂等、重算后旧 FRD 不变。第三个作业引用不存在的单元节点，实际求解失败；新发布未提交，上一版活动结果及旧不可变成果保持。

| 验收组 | 原始证据 |
| --- | --- |
| Linux，2 成功/1 预期失败 | `../../tmp/linux-calculix-20261118/linux-v1/result.json` |
| Windows，2 成功/1 预期失败 | `../../tmp/windows-calculix-97md8w7a/result.json` |
| 版本实际运行 | `../../tmp/linux-calculix-20261118/toolchain-result.json` |
| 默认/自定义 Compose | `../../tmp/linux-calculix-20261118/compose-check.json` |

六个作业的主进程及 CalculiX 子进程停止结果逐项核验，能力探测产生的原生请求也核验成功和停止；测试 API 退出单独记录。源码构建前的旧运行库缺失、GCC void 返回错误，以及误判版本返回码的首轮失败日志均保留。

## 部署、历史成果与剩余范围

部署候选镜像为 `vibeflow/api:linux-calculix-v1-20261118`。更新前确认没有排队/运行作业并保存私有 Compose，只更新 API/worker，保留数据库、存储和其他服务。部署结果以 `../../tmp/linux-calculix-20261118/deployment-result.json` 为准，包括两容器源码一致性和三份历史 TeX PDF 哈希保持。回退应恢复对应镜像及配置，保留历史任务使用的工具版本和全部数据卷。

本阶段证明所述静力单元、输入/发布及失败流程。非线性、接触、热/流体、特征值、复杂重启动和完整大模型矩阵仍需按原功能要求逐项验收。FF-C08 同时承诺 SU2、Code_Aster、Elmer、MOOSE 等后端，不能因本次 CalculiX 验收而整项关闭。

主线 **49/53，剩 4 项**；资源 **11/17，剩 6 项**；全功能整项交付 **2/295，剩 293 项**。此次证据计入 N7-05 和 FF-C08，原范围保持有效。
