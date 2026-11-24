# WSL 专业子执行准备期间的失联恢复

2026-11-16。本项继续 N4-07 原“失联可恢复”条件，补齐 MPI 子执行已经完整准备、尚未登记 running 时的恢复。沿用现有 Windows 守护器、Linux 内核范围及 v4 排空凭据，不新增权限层或阶段退出条件。

## 实现

`NativeExecutor` 在业务程序放行前，已经按原顺序保存：子执行预留、内核范围分配、进程准备观察、非特权 gate 入组凭据和完整 prepared 记录。worker 若在下一步 running 登记前退出，原子执行 identity/stop 仍为空；此前恢复只读取 running 日志，遗漏这份已保存的完整身份。

现在 `child_launch_registration.prepared_identity` 同时读取 Windows 和 WSL 的完整原准备链。它只返回原身份，不回填 running 或 stop。`job_native_scopes.recover` 读取该身份后，仍要求：

1. 原 worker 出生身份确实退出，根和子 Windows Job 守护凭据完整。
2. 原 `native_scope_admitted` 身份与准备身份完全一致，原 gate 为 UID 65534，入组观察确实对应原分配范围。
3. 通过原 boot/device/父 inode/范围 inode 绑定撤销原 Linux 范围，并观察原 gate 出生身份退出。
4. 保存独立恢复凭据前，在原 case 锁内重新核对日志及准备事件。

`job_scope_recovery` 将准备完成但未登记的 WSL 子执行纳入必须有内核撤销证明的完整清单；缺少 admission、retirement 或范围绑定均不能生成有效恢复排空凭据。原输入、发布预约、child.identity/stop 及缺失根停止记录均保持原事实，原命令不重放。

MPI 处理器同时复用已有 `handler_environment` 声明机制。新提交且配置了主机映射的 `mpi.openfoam` 使用原登记 worker 散列及解释器角色进行跨主机契约核对，实际 MPI 仍路由到 Windows/WSL 执行。此变更不表示 Linux 主机可直接执行 WSL，也不重写历史请求。

## 真实验收

入口：`platform/server/live_llm_wsl_child_startup_loss_acceptance.py`。审查记录：`tmp/N4-07-wsl-child-startup-loss-review-v1-20261116.json`，包含新入口和全部继承辅助代码的 617 个源码散列。模型观察器转发真实 DeepSeek 请求；未使用模拟 provider、固定模型答复或模拟 CLI 推理。

- **实际基础设施和工程结果**：独立 PostgreSQL/S3/Redis，Windows API/两个 Windows worker、非特权 Linux worker；真实 blockMesh 与 icoFoam 生成 400 单元至 0.5 时刻的串行参考，再完成双进程 MPI 分解/求解/重构。速度场相对差 `1.0000000000287557e-6`，满足原 `1e-5` 阈值。
- **实际故障**：在原内核入组及 child prepared 提交后，实际 worker `os._exit(17)`。同机另一个 worker 恢复原任务为 interrupted，保存独立范围撤销凭据；原 child 仍 preparing/identity=None/stop=None，无新增 running 事件或新 attempt。原 0.5/U 字节不变。
- **完整使用流程**：显式提交新 MPI 请求，重新完成双进程求解并发布新版本，与前一 MPI 场比较；实际 Linux 命令写入下载场数据散列，真实模型经平台文件工具独立读回。原基线还验证用户取消、worker 关闭中断和重启后新任务。
- **纯凭据检查**：Windows 和 Linux 分别校验同一实际数据库内的恢复凭据，并各拒绝 18 类篡改，包括缺失原准备链、内核入组/撤销历史、改范围 inode、改 guardian 绑定及把活跃 gate 当退出。检查不修改数据库，不能计作模型调用。

最终证据：`tmp/remote-worker-6jraeak9/`。9 个实际任务均达到预期结果，包括故障中断、用户取消及关闭中断；3 个原生子执行和 27 个记录中的原进程身份经 `wsl-child-startup-exits.json` 核对均已退出。双主机各 18 项凭据拒绝，共 36 项纯检查。官方 DeepSeek `deepseek-flash` 实际调用 2 次 HTTP 200，模拟 0。

专属 Redis/S3 已停止；`tmp/remote-worker-postgres-503515b1ce714cf4b0fa0b518f044618/` 确认临时数据库已删除、访问配置已恢复。最终实现与审查源码逐文件一致，没有验收后的实现改动。源码/证据归档核验在 `tmp/mainline-N4-07-wsl-child-startup-loss-evidence/validation.json`。

## 仍需完成

本轮只证明完整准备链和完整原生入组已保存的窗口；更早的不完整准备、其他取消组合和外部 owner 沿用原核对范围。Windows Pandoc 对应路径见 [报告子执行失联恢复](child-startup-loss-recovery.md)。N4-07 尚未整体关闭；N4-05、N7-01、N7-05、N7-06 及全部 295 项完整交付继续有效。

## 增量：入组已保存、child_prepared 尚未保存

后续继续同一原恢复条件。`native_scope_admitted` 提交与 `child_launch_prepared` 提交之间仍有一个实际退出窗口：原观察链已完整记录 gate 身份及非特权状态，内核入组也已记录，但旧恢复要求最后一条 prepared 存在。

现在只在该最后记录确实缺失时，使用原入组事件中的身份。它必须同时匹配原 child/parent/worker、launch intent 和完整准备观察链；恢复凭据内的这条原入组事件还必须与经完整校验的内核历史逐项相同。缺失 native_ready 观察、改 admission seed、改入组观察，均不能通过。原 child_prepared 和 child_running 事件仍保持缺失，不回填、不重放。已经有 prepared 的路径保持原凭据格式。

显式入口 `live_llm_wsl_admission_loss_acceptance.py` 在同一 PostgreSQL/S3/Redis 算例中顺序执行两种真实故障：入组提交后实际退出，以及 prepared 提交后实际退出。每种均先运行真实串行/双进程 MPI，恢复后明确提交新的400单元 MPI 求解，再由真实 Linux 任务发布场散列证明。入组窗口在 Windows/Linux 各检查21项篡改拒绝，prepared 兼容窗口各检查18项；最终实际模型读回只运行一次完整工具回合，另记录实际 HTTP 次数。

首轮新增入口及辅助代码审查：`tmp/N4-07-wsl-admission-loss-review-v1-20261116.json`，618 个源码散列。首轮双窗口证据 `tmp/remote-worker-jckwmkpw/` 的功能路径通过：14 个实际任务达到预期结果，6 个原生子执行，44 个记录中的原进程身份独立核验均已退出；两端凭据检查共78项。官方 DeepSeek `deepseek-flash` 2 次实际 HTTP 200，模拟0。入组窗口的原 child_prepared 数量一直为0，兼容窗口为1；两轮均未增加原 child_running 或新 attempt。**但归档前的非特权执行核验未通过，因此这一轮不是完整验收通过。**

该轮专属Redis/S3已停止；`tmp/remote-worker-postgres-3de40349783c40f7a16a8d1ac13cd703/` 确认临时数据库删除、访问规则恢复。上文原单窗口验收继续作为历史证据保留。

### 普通 WSL 的带类型命令遗漏

`tmp/N4-07-wsl-typed-command-root-before.json` 从首轮真实进程记录核对出：MPI child 的 UID 为65534，但两次串行参考均通过 `/jobs` 的 `command.convert` 执行，根 gate 的实际 UID 是0。原普通 WSL 非特权分支只接受无处理器的 shell，遗漏了已登记的普通带类型命令。第一轮检查脚本在任何台账或归档修改前拒绝继续，未降低断言。

修复只扩展现有普通 WSL gate 的适用判断：经过封存和完整处理器契约核验的 `CaseCommandHandler` 也使用原内核范围及UID65534。专业 argv 处理器和 sandbox 分支不在本次改动中。重新验收的串行 payload 先实际执行 `id -u` 检查65534，结束后再核对保存的 gate 身份；两种失联窗口、重新求解、后续发布、真实模型回读全部重跑。

最终重跑审查为 `tmp/N4-07-wsl-admission-loss-review-v2-20261116.json`，证据 `tmp/remote-worker-vw5zehdk/` 已通过：两次串行根及 MPI child 均核对 UID65534，14个实际任务、6个原生子执行、44个原进程身份退出，78项纯凭据拒绝。最终轮官方 DeepSeek `deepseek-flash` 2次HTTP200；加上首轮功能验证，本单元实际模型调用共4次，模拟0。首轮权限失败不被改写为通过。

最终专属Redis/S3已停止，`tmp/remote-worker-postgres-1214a9a5bfcd4943a5bfad5bf50c2ada/` 确认临时数据库删除及访问规则恢复。实现与最终真实运行的618个审查源码散列一致，归档核验为 `tmp/mainline-N4-07-wsl-admission-loss-evidence/validation.json`。
