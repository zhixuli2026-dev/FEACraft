# 旧排队任务的输入封存检查

2026-11-05，本批验收完成，规范证据在 `tmp/mainline-N2-attempt-isolation-closure-evidence/`。文件存储/S3专项各9项，其余5套回归全部通过；N2-03按原条件关闭，完整恢复/硬隔离范围继续。

入口核对发现：当前所有公开 Job 提交入口已经封存输入，但旧版本留下的排队记录可能只有投递意图，没有独立输入/运行目录。原投递恢复会继续认领该任务，执行器随后退回当前算例目录；旧 Redis `run_job` 消息还可绕过持久投递策略直接调用执行器。修复前的隔离复现确实返回 `started` 并保存了 running 状态；测试随即关闭 API，未观察到测试输出文件，不能把它写成已经复现覆盖用户文件。所属宿主进程事后核对为已退出。

现在创建投递意图时必须具备与 Job、attempt、case、runtime 关联的封存清单。真正认领持久请求时再次检查，防止仅在提交时验证。完整文件字节核验仍在原启动边界进行，数据库锁内不增加文件复制或远端读取。

恢复投递遇到缺失或串用的封存清单时，保存 `input_recovery_required` 和 `review_inputs_and_resubmit`，保留原 queued 记录，不取得 claim、不启动进程、不拿当前文件补造旧输入。说明和 outbox 一起提交，重复投递不反复创建相同说明。重启后可读，原任务仍可取消；对照当前输入发起新请求由原提交入口负责，完整人工恢复页面和新旧请求关系仍由 N5-07/N6 交付。

旧 Redis 消息只有在存在匹配的持久投递意图、原执行规格和 Redis 投递模式时，才能通过统一投递器执行。没有投递授权、载荷被替换或投递模式不匹配时不能执行。合法旧消息仍支持原尝试执行一次并等待收尾；重复消息不再启动进程。新 `run_delivery` 路径继续由 SQL 意图恢复，不依赖旧消息载荷重建请求。

源码位于 `attempt_workspace.py`、`job_delivery.py`、`distributed_queue.py`、`job_recovery.py` 和 `job_recovery_notice.py`。`legacy_job_admission.Admission.submit` 已没有产品调用者，保留的准备/重放/响应公共辅助函数仍被各正式路由使用；不可把这些 import 误判为仍在创建旧固定 argv 作业。

验收：

- `unsealed_delivery_smoke.py`：真实 API、SQLite 和所属进程，覆盖旧普通请求、固定 argv、旧 Redis、错误 case 绑定、新请求拒绝、重启诊断、取消、合法私有执行、无授权与载荷/模式拒绝及合法旧消息兼容。`--s3` 在独立 SeaweedFS 4.52 中执行相同断言。
- `job_delivery_recovery_smoke.py --wsl`：API 真正退出后的 Windows/WSL 恢复、批次、配额和重复消息。其测试种子现在和真实新提交一样封存输入；历史未封存情形由新专项直接覆盖。
- `job_reconciliation_notice_smoke.py`：旧“unclaimed”夹具本来没有封存输入，预期原因改为更明确的 `input_recovery_required`，其他所有者/指纹/取消/审计断言保持。
- `job_delivery_redis_smoke.py`：真实独立 Redis/arq，含队列丢失后按 SQL 恢复。
- `default_workspace_smoke.py` 和 `legacy_job_admission_smoke.py`：默认输入隔离、重试、批次与正式专业入口准入兼容。

没有迁移或写入开发数据库，没有重载端口 8000，没有更改项目 Redis 6380 或 Windows Redis 6379。本批不解决孤立准备目录、未知提交、原生缓存丢失和完整人工恢复。回退源码会重新开放旧投递风险；需要回退时应同时暂停旧 queued 投递，而非恢复后立即扫描执行。
