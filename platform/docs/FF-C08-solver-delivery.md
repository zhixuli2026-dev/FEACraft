# FF-C08 开源后端子任务

2026-11-24，FF-C08 按原 T04 验收。当前 Windows API、非特权 WSL 与已有 Linux 部署组合覆盖五个具名后端。重新用 Edge 完成 9 次实际求解及续算，逐项核对输入、发布、清单和进程退出。浏览器验证了结果下载、原回执丢失后恢复、越权拒绝、范围读取和历史版本。覆盖同一批源码下的五个正式提交入口。模型调用为 0。 [当前 Edge 回执](../../tmp/ff-c08-browser-replay-ewo70avu/native-solver-browser.json) · [复验脚本](../tools/e2e-native-solvers.mjs) · [全功能计划](full-function-delivery-plan.md)

本轮浏览器发现同一算例的工作流与工程意图复用了组件键。两块使用独立键后，Edge 单独复核两块可见且控制台无重复键告警。 [修复](../web/src/panels/CasePanel.tsx) · [复核回执](../../tmp/case-controls-key-darso_ed/case-controls.json)

| 子任务 | 已核对的交付路径 | 适用运行环境和异常验收 |
|---|---|---|
| `FF-C08.01` CalculiX | C3D8 静力、模态、原生重启和中断恢复。Windows 与 Linux 实际静力及不可变发布。输入包含、目标冲突和停止证据见现有验收。[独立发布](calculix-publication.md) · [Linux 部署](linux-calculix-deployment.md) | Windows 与 Linux，含输入封存、取消、超时与真实原生续算。[退出与续算回执](../../tmp/mainline-N2-calculix-publication-evidence/evidence-origin.json) |
| `FF-C08.02` Code_Aster | `.export` 提交、隔离 `.comm`、双存储拉伸、运行取消和 MED 发布。原生库由 `FIN` 生成、`POURSUITE` 恢复，平台可从旧发布续算并发布新库。[运行手册](open-solver-native-runtime.md#code_aster-平台提交) · [历史库续算](open-solver-native-runtime.md#code_aster-计算库续算) | Windows API、非特权 WSL，双存储实算与旧发布续算。[实际回执](../../tmp/code-aster-restart-platform-validation-20261121.json) |
| `FF-C08.03` Elmer | Windows `.sif` 与网格封存，双存储传热和线弹性，VTU/.result，运行取消及 ASCII 和二进制续算。[运行与数值验收](elmer-runtime.md) | Windows 串行 ElmerSolver，取消及实际续算。[双存储续算](../../tmp/elmer-restart-validation-20261121.json) |
| `FF-C08.04` SU2 | Windows 与 Linux 的 NACA0012，涉及 5,233 点的四个标量场和密度残差。公开 `.cfg` 提交、ASCII/二进制续算、双区域、取消与故障恢复。[独立发布](su2-publication.md) · [Linux 部署](linux-su2-deployment.md) | Windows 与 Linux，实际迭代中的取消与基于历史的恢复。[退出与续算回执](../../tmp/mainline-N2-su2-publication-evidence/evidence-origin.json) |
| `FF-C08.05` MOOSE | Windows API 在非特权 WSL 执行，双存储稳态扩散、实际瞬态取消、Exodus/CSV 与不可变检查点。指定已被替代的历史发布恢复至 `t=0.4`，与连续解逐节点一致。[提交与续算](open-solver-native-runtime.md#moose-平台提交) | Windows API、非特权 WSL，双存储与历史检查点恢复。[实际回执](../../tmp/moose-restart-platform-validation-20261121.json) |

各工具手册给出本次验收的运行版本、物理场、平台和输入限制。物理与数值组合的交付范围以各相关专项记录为准。故障时可回退到前一可用 API/worker 构建，再读已发布的历史成果与重启输入。 [公开 API](api.md) · [本机进展](progress.md)

## 浏览器提交与成果

选定算例后，在「作业与日志 → 新建任务」将任务方式设为「开源求解」，选择求解器和对应输入。输入文件和依赖需先保存在算例里。核对并提交后，工作区打开该作业的状态和日志。回执丢失时可读取原提交结果或用同一键重试。

求解成功后，在「求解成果」核对发布 ID。CSV、日志和受支持的文本格式可预览，FRD、VTU、Exodus、MED 和检查点可下载原件。历史作业读取其发布档案。MOOSE 和 Code_Aster 的续算在新任务中填写原发布 ID、续算文件路径和本次输入文件。详情见 [MOOSE 和 Code_Aster](open-solver-native-runtime.md)。

2026-11-21，Edge 在 SQLite 文件系统完成 9 个真实作业，覆盖五种输入、MOOSE 与 Code_Aster 续算及 CalculiX 重算。MOOSE 稳态中点为 50，瞬态续算到 `t=0.4`。五种二进制下载与发布摘要一致，重算后仍可原样下载第一版 FRD。另验证了原回执丢失后找回同一任务、同键重放和参数变更拒绝、跨账户拒绝、分段读取、版本校验和 390 px 工作区布局。连续下载检出的下载槽未回收问题已修复。认证 SeaweedFS 搭配 SQLite 又完成两次真实 CalculiX 作业，旧发布与新发布摘要各自吻合，连续 6 次分段读取成功。本轮只调用原生求解器，实际模型请求及模拟均为 0。[Edge 验收回执](../../tmp/document-snapshot-browser-toksreac/native-solver-browser.json) · [认证 S3 验收回执](../../tmp/native-artifact-s3-nvmuvkri/native-artifact-s3.json) · [窄屏截图](../../tmp/document-snapshot-browser-toksreac/native-launch-compact.png)
