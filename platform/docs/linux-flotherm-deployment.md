# Linux FloTHERM 辅助库编译

2026-11-18：正式 `/api/v1/cases/{case_id}/flotherm:build-library` 已支持 Linux 容器，复用现有 OpenFOAM/wmake 环境和受控本机进程。Windows/WSL 继续使用配置的发行版及 nobody 用户。

## 配置与行为

在已有 [Linux 工程镜像](linux-cae-deployment.md) 上使用当前应用源码，无需另装 FloTHERM 商业软件。`VIBEFLOW_OPENFOAM_BASHRC` 或 Setup 中保存的 bashrc 指向容器内实际 OpenFOAM 安装，例如 `/usr/lib/openfoam/openfoam2406/etc/bashrc`。Linux 使用 `distro=native-linux`；Windows 使用配置的 WSL 发行版。API/worker 须使用一致镜像和配置。

提交参数仍为 `source_dir`、`destination_dir`。源目录必须包含 `Make/files`、`Make/options` 和 `.C` 文件；不同源/目标目录不能嵌套，目标不能已经存在。复制范围、文件大小限制、配额和冻结输入校验保持原规则。

真实 wmake 在作业私有副本内运行，移除本目标的旧对象缓存后重新编译。`FOAM_USER_LIBBIN` 指向本次私有目录，成功构建并实际加载的库才发布到算例目标目录。更新后的 `.so` 可被后续平台作业以发布编号绑定并执行。源码、编译/加载日志、工具摘要和私有目录清理记录进入成果凭据；原始不可变发布继续可核验。

Linux 原生子进程使用 `flotherm_library_linux` 标识，Windows 保留 `flotherm_library`。Linux 加载器使用实际运行的 Python 路径，兼容官方容器的 `/usr/local` 安装；Windows/WSL 保留 `/usr/bin/python3`。Linux 沿用既有容器用户，Windows/WSL 的普通执行仍为 UID 65534。

## 实际验收

验收使用专用 S3 存储、独立 SQLite/账户，真实执行 wmake、g++、动态库加载和平台 shell 作业。简单 C++ 测试库导出一个返回浮点值的函数，用于证明新库实际编译并被调用；它不代表完整 FloTHERM helper API。没有模拟 LLM 或伪造原生输出。

| 场景 | 验证内容 | 原始记录 |
| --- | --- | --- |
| Linux 完整编译流程 | 带空格/特殊字符的源目录复制；构建、加载、发布并执行函数得到 1.25；修改源文件原目录重编译，执行新函数得到 2.5；旧发布字节保持；私有安装并执行 | `../../tmp/linux-flotherm-20261118/linux-v1/result.json` |
| Windows/WSL 兼容 | 同一完整流程，实际非特权 wmake/CDLL 和函数调用；旧库保持 | `../../tmp/windows-flotherm-rip6lcbl/result.json` |
| Linux 编译中取消 | 真实观察到 namespace 内 cc1plus 正在运行后请求取消；原生范围和实际编译进程退出；旧活动库及不可变发布保持 | `../../tmp/linux-flotherm-20261118/linux-cancel-v3-retry/result.json` |

Linux 和 Windows 完整组还各执行两项负向算例：无效 C++ 导致真实编译失败，未定义符号导致真实加载失败。两种情况下都不提交新发布，并保留旧活动库和旧不可变发布。成功构建检查幂等重放、全部库哈希、真实加载日志和私有暂存目录移除。每个作业核验主进程及已有子进程停止结果；各测试 API 退出也单独核验。

Windows 首轮在成功发布后紧接着重放时遇到 `storage_quota_busy`，属于远端写入结算仍在进行；测试脚本加入仅针对该状态的 30 秒有界重试，原失败记录保留。Linux 取消首轮的专用存储启动参数被 PowerShell 拆开，卷上限未生效，新桶首次写入失败；改用参数数组启动新的专用存储并核对实际参数，等待接口就绪后才重试。首次过早连接的错误也保留在容器日志中。没有修改正式存储配额或降低产物核验。

## Linux 转换与辅助库隔离

FF-J16 将 FloTHERM IR 转换和辅助库编译接入原生 Linux 的独立挂载、PID、网络及 IPC 命名空间。转换进程读取本次封存的输入并写入选定的产物目录。辅助库保持编译与加载子进程登记，子进程只访问本次算例副本及只读工具链。编译临时目录归本次执行使用。

2026-11-26，文件系统和认证 SeaweedFS 各导入并转换同一份 ECXML，生成 52 个工程文件及通过预检的双热阻模型。产物记录 `θJC=25 K/W`、`θJB=45 K/W`、芯片功率 `0.3 W`。两个实际 OpenFOAM 编译都产生 ELF，另起命令分别从冻结发布加载该库，并核对进程的 IPC 命名空间与普通 worker 不同。两次取消都观察到真实 `cc1plus` 运行，随后核验根和原生子范围停止。原库的活动文件和归档字节一致。S3 文件由起初无缓存的另一 API 读回，外账户查询返回 404。模型调用 0 次。 [文件系统回执](../../tmp/j16-flotherm-3kjsjigl/result.json) · [认证 S3 回执](../../tmp/j16-flotherm-rjf8nx6h/result.json)

Pack 的正式导出入口需要已配置的 Windows FloTHERM 启动器。当前 Windows 环境的入口检查返回 `flotherm_export_unavailable`，持证 Pack 导出继续由 FF-I01 和 FF-I03 跟踪。

共享启动器更新后，原生 OpenFOAM 串行、两进程 MPI、重建及运行中取消通过回归，400 单元速度场最大归一化差为 `1e-6`。PDF、PPT、旁白视频和两种草稿也通过实际回归。试验资源已清理，原 11 个 Docker 服务标识一致。构建缓存仍为 5.857 GB，Windows C 盘可用空间由 39,211,577,344 B 变为 39,234,068,480 B。 [MPI 回执](../../tmp/j16-openfoam-8lhh68q2/result.json) · [办公产物回执](../../tmp/j16-office-mu81etv0/result.json)

复验通过公开接口导入 ECXML 双热阻算例，先实际生成工程文件，再从生成的 C++ 源码编译及加载 `libvibeflowFlothermTwoR.so`。从项目根运行。

```powershell
& platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/linux_flotherm_isolation_acceptance.py --storage fs
& platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/linux_flotherm_isolation_acceptance.py --storage s3
```

## 更新、回退与范围

候选镜像 `vibeflow/api:linux-flotherm-v3-20261118` 在已验收的 Linux TeX/独立收尾镜像上更新六个应用模块。更新前确认没有排队或运行作业、保存 Compose 配置，只替换 API/worker。保留旧镜像、数据库、存储及历史发布；不要通过删除数据卷回退。部署结果记录在 `../../tmp/linux-flotherm-20261118/deployment-result.json`，包括实际容器源码核对和三份历史 TeX PDF 哈希读回。

v1、v2、v3 的应用源码相同；后两版仅调整验收中的结算重试并补实际取消场景。审查文件为 `../../tmp/review-N7-05-linux-flotherm-v1-20261118.json` 至 `../../tmp/review-N7-05-linux-flotherm-v3-20261118.json`，真实模型调用 0、模拟 LLM 0。

三组合计 19 个作业：14 个成功、4 个预期的真实编译/加载失败、1 个成功取消，核验 19 份主进程及 12 份子进程停止结果。正式 S3 部署更新时 Docker 返回一次 I/O timeout；随后实际检查发现 API 已运行且健康，worker 处于 created。单独启动该 worker 后完成两容器源码及历史 PDF 核验，原失败记录为 `../../tmp/linux-flotherm-20261118/deployment-result-initial-timeout.json`，没有重新创建数据服务。

FF-I09 的原要求仍包括完整辅助库 API/链接依赖、共享安装/升级/失败回滚和强杀残留恢复矩阵。本次交付的是既有算例私有编译/安装流程的 Linux 支持及实际验收，不能据此关闭整个 FF-I09。PostgreSQL 完整业务故障组合、物理多主机和其他外部工具仍由原主线继续承接。

主线 **49/53，剩 4 项**；资源 **11/17，剩 6 项**；全功能整项交付 **2/295，剩 293 项**。本阶段的功能进展计入 N7-05 和 FF-I09 的实现证据，完整范围保持不变。
