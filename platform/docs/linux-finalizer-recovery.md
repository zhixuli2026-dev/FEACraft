# Linux 独立收尾恢复

2026-11-18：Linux 的 `job.finalize` 已接入现有受控 PID namespace，可在原作业 API 退出、独立收尾 API 再次退出后继续同一次 attempt。Windows/WSL 的既有收尾路径保留并实际复验。本阶段不新增模型功能，真实模型调用 0、模拟 LLM 0。

## 行为与恢复入口

作业已经完成实际编译、记录冻结输入并归档输出，但提交最终状态前 API 退出时，先读取 `GET /api/v1/jobs/{job_id}/output-recovery`，使用返回的 `attempt_id` 和 `inventory_sha256` 调用 `POST /api/v1/jobs/{job_id}:recover-outputs`，再调用 `POST /api/v1/jobs/{job_id}:finalize-outputs`。独立验证器再次退出后，显式重试最后一个入口会检查旧请求所有者退出，清理原执行范围、结算输出预算，再创建关联的新验证请求。

这条路径要求完整原输出清单和归档准备证据，不能对缺少证据的历史作业强行标记成功。本次恢复使用实际 S3 存储；本地 FS 的不完整清单仍会被该入口拒绝。

Linux 实现复用 `OwnedProcess` 的启动门和 PID namespace，在业务放行前记录原始宿主、namespace init 出生信息及实现哈希。正常退出使用实际停止结果；独立恢复对记录的具体进程使用 pidfd，并核验 init/宿主退出。原请求缺失的停止记录继续保留为空，另写独立恢复凭据。不能根据 PID 存在与否替换出生身份，也不能将未观察到的启动进程记作已退出。

原 attempt 编号、输入清单、原作业主进程及子进程日志不变。重新执行的是独立校验流程；原求解任务不会自动重跑。已经完成的恢复可重复调用，不增加新的验证请求。

## 实际验收

每组使用独立 SQLite、账户和 S3 bucket，存储由专用 SeaweedFS 容器提供。故障夹具仅暂停真实执行边界，然后实际终止 API；没有生成替代模型回复、替代编译结果或伪造进程停止。每组均生成、下载并核验 40 页 PDF，核验文本标记、发布哈希、原 attempt/日志保持和重复恢复幂等性。

| 场景 | 实际故障与断言 | 证据 |
| --- | --- | --- |
| Linux 运行中断 | 原 API 退出；第二个 API 的真实 pdfLaTeX 进程运行时再次退出；第三个 API 清理原 init/宿主/编译进程并完成发布 | `../../tmp/linux-finalizer-20261118/linux-v3/finalizer-result.json` |
| Linux 启动中断 | 原 API 退出；第二个 API 已持久化 namespace 出生、尚未登记放行业务时退出；第三个 API 验证未放行、关闭已观察范围并完成发布 | `../../tmp/linux-finalizer-20261118/linux-namespace-v4/finalizer-result.json` |
| Windows/WSL 兼容 | 实际原文档归档后终止 API，重启后通过原 Windows guardian/WSL 路径独立收尾 | `../../tmp/windows-finalizer-vlopp1tr/finalizer-result.json` |

Linux 两组的首次验证请求均保留 `OwnerExited` 失败历史和原缺失停止记录。启动中断证据明确记录 `business_not_released=true`、`unknown_bootstrap_exit_inferred=false`。验收结束时各测试 API 已退出，证据中的 PDF、SQLite、逐次 API 日志与原始失败均保留。

首次验收使用 FS，未形成完整输出准备记录，在恢复入口被拒绝；改用实际 S3。第二轮 Windows 的系统代理转发本地对象存储请求产生 502，使用测试进程本地地址直连后恢复。第二轮 Linux 的验收轮询未兼容刚创建的空子进程日志，修正检查脚本后通过。没有降低产品恢复条件。

## 部署及范围

候选镜像为 `vibeflow/api:linux-finalizer-v4-20261118`，由已验收的 Linux TeX 镜像追加当前应用源码。部署脚本先确认没有排队/运行作业，保存私有 Compose 配置，再只替换 API/worker，保留数据库、存储和其他容器。部署结果以 `../../tmp/linux-finalizer-20261118/deployment-result.json` 为准：核验两容器源码哈希与工作区一致，并下载三种引擎的历史 PDF 比对更新前哈希。

v3 与 v4 的全部应用模块哈希相同；v4 增加了启动边界的验收夹具。源码审查与归档分别为 `../../tmp/review-N7-05-linux-finalizer-v3-20261118.json`、`../../tmp/review-N7-05-linux-finalizer-v4-20261118.json`。部署脚本为 `../../tmp/linux-finalizer-20261118/deploy.py`。

本阶段证明上述三条实际路径。PostgreSQL 上的完整恢复组合、多副本/物理跨主机、其他原生工具及其故障组合仍按 N7-01 原矩阵验收，不能由 SQLite 测试或一次 PostgreSQL 部署读回代替。没有将历史缺失范围证据升级为新保证，也没有承诺跨宿主重启后凭旧 PID 恢复。

主线仍为 **49/53，剩 4 项**（N4-05、N7-01、N7-05、N7-06）；资源 **11/17，剩 6 项**；全功能整项交付 **2/295，剩 293 项**。本阶段补齐 N7-05 的 Linux 独立收尾适配，并提供 N7-01 的部分实际故障证据；这些大项仍未整体关闭。
