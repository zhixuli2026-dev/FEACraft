# 注册原生执行的原镜像清理

2026-11-07。新增原 ext4 镜像清理后端，已在实际 FS、SeaweedFS、WSL 环境删除隔离沙箱镜像并验证保留快照可继续使用。随后完成已登记清理者的独立退出对账和界面入口，见[中断恢复与界面验收](sandbox-image-reconciliation.md)。未登记身份窗口及完整故障矩阵仍待完成，不能据此称全部卷维护已经交付。

最新进展：[启动证据与双库取消验收](sandbox-image-startup.md)已补部分未登记身份恢复和 PG 双 API 取消重试；完全缺失观察及未知 WSL 身份等窗口继续。

## 实现

新增 `GET/POST /api/v1/terminal/sessions/{sid}/image-cleanup`。POST 必须明确 `confirm:true`，并指定旧记录收尾所保留的 `recovery_id`。调用者权限、完整收尾组、恢复请求成功/实际停止证明和完整 ZIP 哈希都必须通过。持久清理意图绑定原镜像路径、磁盘回执、快照中的完整镜像哈希和分发环境。

执行使用新的 `NativeRequest(kind='terminal.cleanup')`、固定 `sandbox_image_cleanup` purpose 和现有原生进程登记、释放、心跳、停止回执流程；未通过这些登记与授权不能释放命令。每个请求最多执行一次。清理意图提交后，新镜像恢复的创建与最终释放都会被拒绝；活动或停止未验证的旧恢复/清理请求会阻止新的删除。

固定 helper 在已登记并获准释放的 **外层 Linux gate** 核验原进程出生身份及旧读取进程确实退出，不用子 PID 命名空间中的“查不到进程”作为证明。随后取得镜像独占 flock，核对磁盘 device/inode/128 MiB 固定大小、ext4 UUID、与快照绑定的完整 SHA-256，并在删除前后检查相关状态。任何现存 loop 关联均拒绝；不会替用户卸载其他设备。

唯一删除目标是已核对原目录里的固定 basename `volume.ext4`，通过目录描述符执行 unlink；原目录、`disk.json`、初始基线等元数据和恢复快照全部保留。原生回执绑定实际外层 gate 身份，只有已验证停止的成功请求才能提交完成事件。返回值区分 `image_absent` 与 `deleted_this_request`，不会把原来就缺失的镜像冒充本次删除。

没有完成回执的意图继续阻止管理操作。已完成操作重复 POST 返回同一回执；旧路径重新出现文件时拒绝，不删除新内容。节点准备文件经过容量预留，无法确认原生子执行停止时保留预留。

迁移新增 revision **c6a1de7402ef**，前版 **b5f0cd6391de**；仅扩展请求 kind 约束。存在清理请求或清理意图/完成事件时拒绝降级，以免丢失执行证据。开发数据库尚未迁移，本轮只操作隔离库。

## 实际验收

- [S3 清理](../../tmp/sandbox-recovery-ui-retirement-sns_f1g8/image-cleanup-checks.json)：真实镜像字节变更后拒绝删除，恢复原字节后由登记 gate 删除；实际子进程回执、快照下载、元数据不变、远端文件未发布、重复确认和重新出现文件保护通过。2 项实际基础设施、3 项协议/故障检查。
- [FS 清理与实际占用](../../tmp/sandbox-recovery-ui-retirement-5e2yif6o/image-cleanup-checks.json)：增加实际 Linux 进程持有 flock、实际只读 loop 设备两种拒绝场景。独立 helper 在整个拒绝期间保持活动；退出时释放自己创建的锁/设备，原进程身份确认已退出，再执行实际清理。共 4 项实际基础设施、3 项协议/故障检查。
- [真实 DeepSeek](../../tmp/sandbox-recovery-ui-retirement-sns_f1g8/live-result.json)：实际删除镜像后，从保留快照确认写回原 shell 随机内容，模型选择读文件工具并续答，**2 次真实调用、8513 tokens、模拟 0**。服务 `api.deepseek.com`，模型 `deepseek-flash`。严格“只回复随机字符串”仍有额外说明，未算作通过。
- [双库迁移](../../tmp/image-cleanup-migration-4y9r0_hk/checks.json)：SQLite/PostgreSQL 各 6 项纯 schema/协议检查，涵盖已有请求无损升级、新 kind 接受、无 case/未知 kind 拒绝、含请求或事件时降级拒绝、无清理历史时往返及 metadata 一致。临时 PostgreSQL 库已删除。
- 两次功能运行分别确认 11/15 个登记进程身份退出；FS 两个占用 helper 另有[文件锁退出](../../tmp/sandbox-recovery-ui-retirement-5e2yif6o/image-flock-guard-exit.json)及[loop helper 退出](../../tmp/sandbox-recovery-ui-retirement-5e2yif6o/image-loop-guard-exit.json)记录。共用旧收尾检查重复执行，不重复计入新增覆盖。本轮未改前端。

## 下一步与限制

已补已登记清理者的 API 猝死独立对账和七处真实退出验收。尚无完整登记身份的 preparing 子执行仍保守阻止新删除；不能伪造正常退出码/stop journal 或仅按租约超时放行。原生 unlink/回执写入定点故障、两个执行者竞争、取消/权限变化、PostgreSQL 原生路径及 WSL/主机重启仍需专项覆盖。

界面确认、结果不确定时查询及显式重试，以及原始暂存与镜像的联动浏览器验收已完成。其他导出发布暂存、失败/未完成恢复副本、长期快照策略、整套额度结算、目录/权限/删除/caseDict/大批量恢复等保持原计划。

主线保持 33/53 完成、剩余 20；资源 11/17 完成、剩余 6 块；295 项全功能最终验收继续。
