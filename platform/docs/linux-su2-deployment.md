# Linux SU2 部署与实际求解

2026-11-18：SU2 求解入口与能力清单共用可执行文件发现逻辑。Linux 默认从 PATH 查找 `SU2_CFD`；显式 `VIBEFLOW_SU2` 优先，错误的显式路径会报错。Windows 保留现有安装路径及显式配置。两端真实求解、续算、成果发布和部署验收已通过。

## 构建

先构建当前源码的 API 镜像，再增加 SU2 层：

```sh
docker build -f platform/infra/Dockerfile.api -t vibeflow/api:current platform
docker build -f platform/infra/Dockerfile.su2 --build-arg API_IMAGE=vibeflow/api:current -t vibeflow/api:su2 platform
```

基础镜像也可以选用已经包含 OpenFOAM、TeX、CalculiX 的当前镜像。`docker-compose.prod.yml` 与 `docker-compose.su2.yml` 配合使用；`VIBEFLOW_API_IMAGE` 指定基础镜像，`VIBEFLOW_SU2_IMAGE` 指定最终镜像，API 和 worker 使用同一结果。多个可选工具镜像层需要依次构建，不能通过叠加 Compose 构建覆盖文件来代替。

Dockerfile 下载 [SU2 官方 8.5.0 OpenMP Linux 发布包](https://github.com/su2code/SU2/releases/tag/v8.5.0)，校验外层 ZIP 的 SHA-256，再解开其中的 `linux64-omp.zip`，复制 `bin/SU2_CFD` 到 `/usr/local/bin/SU2_CFD`。固定摘要为：

```text
aadc800cd9df34deff99d4725f5897f620c9f2979f62ab235313311bf501f09b
```

该程序是静态 Linux ELF，实际执行 `--help` 返回 0 并显示 `SU2 v8.5.0 "Harrier"`。对它执行 `ldd` 会返回 `not a dynamic executable`，不能把这当作缺少运行库。版本探测与首次 ldd 日志均保留。

## 验收范围

独立账户、SQLite、S3 存储桶通过正式 API 提交仓库 NACA0012 算例，真实运行 SU2，检查冻结网格和重启文件、进程停止证明、全部成果摘要、当前结果与历史不可变发布、幂等提交，以及真实错误配置失败后保留既有成果。

算例为 Mach 0.8、攻角 1.25 度的二维无粘 Euler 流动，网格 5233 点、10216 单元。首次最多 250 步，以配置的密度残差阈值停止；第二次从实际输出的 `restart_flow.dat` 续算。解析真实 VTK 的密度、压力、温度、Mach 和 CSV 残差，不能只依据求解进程退出码或文件存在判定通过。

这轮属于实际求解器和基础设施验收，没有模型回复、模型工具选择或 Agent 推理：真实模型调用 0、模拟 LLM 0。完整多物理场、其他后端、部署矩阵等原承诺继续有效。

## 结果与证据

两端首次计算都在第 158 次迭代（索引 157）达到密度残差阈值，CSV 中 `rms[Rho]` 从约 -1.02686 降至 -8.01370。续算使用冻结的真实重启文件，11 次迭代后降至约 -8.27911。这里是配置的密度残差停止条件，不能扩展为所有守恒方程均满足同一残差阈值。

两次计算的 Linux/Windows VTK 坐标、单元连接一致，四个标量场的 5233 个点逐点比较，输出浮点值最大差为 0；CSV 残差差异小于 `1e-5`。这种比较证明平台一致性，不等同于独立实验气动力验证。

补充状态方程检查首次误用了配置中的 `GAS_CONSTANT=287.87`。实际日志显示模型为 `STANDARD_AIR`，其气体常数由[上游 8.5.0 源码](https://github.com/su2code/SU2/blob/v8.5.0/SU2_CFD/src/solvers/CEulerSolver.cpp)固定为 287.058。保留首次参考值错误证据，核验实际模型后，以 287.058 验证 `p=rho*R*T`，两端最大相对误差约 `1.34e-7`，满足 `2e-7` 容差，没有修改真实求解输出或放宽容差。

共 6 个真实作业：4 成功、2 次故意使用无效 SOLVER 的预期失败；6 个根进程、6 个求解子进程及 4 个能力探测请求的停止已核验。Linux 计算较慢但在原验收时限内完成；没有因一次 Docker 查询超时重启作业。

- Linux 证据：`../../tmp/linux-su2-20261118/linux-v1`。
- Windows 证据：`../../tmp/windows-su2-qovd3s4w`。
- 逐点和状态方程比较：`../../tmp/linux-su2-20261118/comparison.json`。
- 部署与 Compose：`../../tmp/linux-su2-20261118/deployment-result.json`、`deployed-toolchain.json`、`compose-check.json`。
- 汇总：`../../tmp/mainline-linux-su2-evidence/validation.json`。
- 源码交付包：`../backups/release-review-linux-su2-20261118.zip`。

S3 环境的 API/worker 已更新为 `vibeflow/api:linux-su2-v1-20261118`，467 个应用模块与工作区摘要一致，历史三份 TeX PDF 的摘要未变。默认和自定义镜像的 Compose 配置均确认 API/worker 选用同一镜像。文件存储环境保持既有部署。

主线 **49/53，剩 4 项**；资源 **11/17，剩 6 项**；全功能整项交付 **2/295，剩 293 项**。本阶段计入 N7-05 和 FF-C08；其余后端、复杂物理、完整并行及生产故障矩阵仍须按原计划交付。下一项已知 Linux 工具缺口为 ParaView 的 `pvpython`/`pvbatch`。
