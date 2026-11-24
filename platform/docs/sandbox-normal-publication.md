# 沙箱正常关闭发布

2026-11-20，文件系统正常关闭的目录树准备已移到算例锁外，原配置修订、空目录和导出回执继续共用一次提交。实际 WSL 正常退出核对同一 40 MiB 摘要、空目录、caseDict 新修订及停止回执。认证 SeaweedFS 的正常导出和失效拒绝兼容通过。[本地目录树实测](local-file-storage-admission.md#2026-11-20-本地目录树暂存与提交)

2026-11-16。承接 N4-05 / RB-12、FF-F58、FF-J16。复用目录树恢复事务，将正常关闭的导出接入同一条发布路径，不新增阶段退出条件。主线仍为 48/53，剩 5 项；资源清单 11/17，剩 6 块；295 项完整交付目标保持。

## 行为

正常退出后，对初始沙箱、最终快照和当前项目进行三方比较，完整保存空目录、删除、文件与目录互换、内容及 Linux 模式位。项目在沙箱运行期间发生冲突修改时，拒绝整批发布并保留导出副本；用户可以继续使用已有恢复入口处理。

caseDict 走已有语法、结构及阶段校验，生成新的配置版本、事件历史和来源历史，与目录变化及发布回执共享 SQL 提交。普通命令不能通过修改配置绕过阶段确认。沙箱自行修改两份版本历史仍被拒绝。仅阅读后退出不会产生新的配置版本。

发布回执沿用原格式，按会话和导出请求生成稳定事件 ID。提交后的重复发布返回同一回执，不重写内容或重复增加配置版本。FS 使用已有子树交换事务；S3 使用已有条件写入日志，不再把导出目录移入另一份暂存并进行第二轮独立提交。

终端启动及启动前重新授权复用 CLI 已有的有界额度等待。只等待写入前的 `storage_quota_busy`，不重复执行命令或提交未知结果。测试的文件准备步骤同样只对这种明确的写入前拒绝有界重试，并记录次数。

## 验证

显式入口 `platform/server/live_llm_sandbox_publication_acceptance.py`。审查继承已读的传递辅助代码并逐个核对摘要；真实模型路径仅使用本地已授权 DeepSeek 配置，观察真实上游请求，不替换回复、工具选择或 CLI 推理。

验证包括实际 WSL 创建目录及类型互换后正常退出、API 读取成果和三份配置文件、重复发布保持单一版本、第二个实际 Linux 沙箱核对四组精确模式位、真实模型读取随机证明、实际并行 API 编辑阻止整批导出，以及阶段变更被拒绝时其他文件也不发布。

最终 FS / SQLite 结果为 `tmp/sandbox-recovery-ui-publication-gfkgm4er/publication-result.json`，实际 SeaweedFS / SQLite 为 `tmp/sandbox-recovery-ui-publication-4z6nud9h/publication-result.json`，各 5 组完整流程通过。每轮 2 次真实 DeepSeek HTTP 200，服务模型 `deepseek-flash`；模型实际选择 `read_text_file` 并读回提示词未包含的随机内容，工具和回复均已持久化。两轮各 20 个原进程身份独立核验不存在，测试 API 和自建对象存储已退出。

最终 FS 使用 v4 审查，S3 使用 v5；两者生产源码相同，v5 只为测试的明确写入前额度拒绝增加有界重试。早一轮 FS `tmp/sandbox-recovery-ui-publication-ozswuhev` 也有 2 次真实请求，合计 6 次，最终生产代码验收计 4 次；模拟 0。

本轮实际结果和源码摘要由 `tmp/mainline-N4-05-sandbox-publication-evidence/validation.json` 统一登记。此前失败也保留在 `55svn874`、`nsqmfeye`、`92be70sd`、`8m9m3dkn` 结尾的同类隔离目录中：首次新回执字段不兼容旧校验；随后测试助手的空参数在发出模型请求前失败；S3 先后遇到终端启动及文件准备的额度暂忙。修复后的证据与早期失败分别记录，不把未发出的请求计入真实模型调用。

## 剩余范围

Windows / S3 保存的是下一次 Linux 执行使用的模式位，不是 Windows ACL 或 S3 原生 POSIX 权限。普通编辑后的权限延续已有后续实现和真实进程退出验证，见 [文件编辑权限延续](file-mode-continuity.md)；其他执行副本和封存输入的模式传递继续推进。Linux API 宿主、其他数据库及原故障组合仍按原计划验收。本页正常导出阶段未新增 API 进程退出测试，不把其他阶段的实际崩溃回滚证据称为本阶段重新执行。

原副本维护、空间峰值、统一期限、跨 worker 并发及资源边界继续；不据此关闭 N4-05、RB-12 至 RB-17 或全功能 F58/J16。

## 2026-11-19 正常关闭时的远端原件复核

RB-14 的固定对照通过正式 WebSocket 进入实际 WSL 沙箱。沙箱只改动一个小文件，然后执行 `exit`。隔离 SQLite 的算例包含一个实际认证 SeaweedFS 40 MiB 对象。在 WSL 里核对该文件的 SHA-256，关闭后再从正式文件 API 读回小文件，并核对大对象的原 SHA-256 和逻辑 ETag。[专项夹具](../server/sandbox_normal_close_resource_smoke.py)

原版关闭正常完成，登记一条成功发布和一条真实原生导出停止证据。发布在同一个 SQLite 写事务里两次条件读取未改对象，各 41,943,040 B，最大块 1,048,576 B。第一轮读到真实第一块后暂停，独立 `BEGIN IMMEDIATE` 确认写锁不可用。原 API 和隔离 SeaweedFS 已独立确认退出。原版执行器 1/1 通过。[旧版实测](../../tmp/sandbox-normal-close-resource-uvrrj47t/sandbox-normal-close-resource.json) · [执行器摘要](../../tmp/vibeflow-regression-all-20261119-164119-5abb17/summary.json) · [独立进程复核](../../tmp/sandbox-normal-close-resource-uvrrj47t/process-exit-independent.json)

实现复用原所有者的额度准入、沙箱发布和回滚协议。在第一次短事务核对终端权限和已有回执并协调待恢复事务，退出数据库事务后再校验普通树正文。提交前按原权限和算例协调器复核 S3 的可见文件版本、树元数据以及绑定的实际物理对象。继续沿用 `sandbox_tree` 日志、条件写、原发布回执、模式位和 caseDict 的同一提交。[正常关闭发布](../server/app/services/sandbox_export_publication.py) · [S3 版本复核及写入](../server/app/services/tree_transactions_s3.py)

首次固定复验已观察到独立写锁可用和一次完整 40 MiB 读取，同时发现新分支在构造条件写入计划时引用了未赋值的旧局部变量。终端保存 `failed/UnboundLocalError` 及经核验的停止证据，发布未成功。固定源码改为传递已经校验的原树元数据。API 与真实隔离 SeaweedFS 的退出另有独立回执。[首次固定失败](../../tmp/sandbox-normal-close-resource-ekfgwqlc/sandbox-normal-close-resource.json) · [独立退出复核](../../tmp/sandbox-normal-close-resource-ekfgwqlc/process-exit-independent.json)

最终两次实际关闭在核验时都允许独立 SQLite 写者取锁。每次对未改对象仅条件读取一次，读取 41,943,040 B，最大块 1,048,576 B。正常场景发布小文件并保持大对象原 SHA-256 与 ETag。负对照在正文读取完成与提交版本复核之间，实际用条件 PUT 把原件的树元数据模式从 `0777` 改为 `0677`。终端保存 `failed/CaseWriteConflict`，外部元数据版本保持，项目内仍可读取前一次正常发布的小文件。新的小文件保存在私有导出目录，独立复核其 46 B 文件与当次导出清单的 SHA-256 完全一致。[固定实测回执](../../tmp/sandbox-normal-close-resource-4xh72aku/sandbox-normal-close-resource.json) · [私有副本与数据库独立复核](../../tmp/sandbox-normal-close-resource-4xh72aku/retained-export-independent.json)

两次终端 `exit` 均返回 0 并持久保存已核验的停止证据，两条原生导出请求均为 `succeeded`，数据库只有一条正式发布事件。两个场景的节点预留和活动所有者额度均归零，同所有者的后续正式保存成功。最终精确回归 1/1，耗时 96.953 秒，模型调用 0 次。隔离 API 由验收句柄终止，实际退出码 1，原 PID 18596 和端口 64717 已消失。认证 SeaweedFS 的原进程 407185 经独立 WSL 检查也已消失。[执行器摘要](../../tmp/vibeflow-regression-all-20261119-165452-f5265d/summary.json) · [独立进程和端口检查](../../tmp/sandbox-normal-close-resource-4xh72aku/process-exit-independent.json)

验收文件和原生临时目录位于 D 盘。固定轮前后，C 盘可用空间 22.10 GiB 和 21.98 GiB，D 盘 385.34 GiB 和 384.46 GiB。11 个项目 Docker 容器保持运行，两组既有 API 容器健康。本轮记录对应 S3 正常关闭发布的完整树读取与版本复核，后续条件写入沿用原事务。
