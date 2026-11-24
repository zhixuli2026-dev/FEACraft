# 旧工作流计划的显式迁移

2026-11-20，迁移先准备字典正文和历史，再原子提交回到 A、新流程和旧计划来源。认证 SeaweedFS 在 SQL 提交前后的真实退出均由冷 API 按文件见证恢复。提交前恢复旧字节后可以重新获取观察值并确认，提交后同键请求返回持久流程。真实旧命令及当前 CadQuery 导出在两种存储和 PostgreSQL 上复验通过。[迁移与退出验证](file-transaction-diagnostics.md#重算与旧计划迁移)

后续状态：2026-11-09，N5-06完整联动与审批/阶段策略已按原条件验收，主线36/53、剩17。见[最终组合验收](engineering-recovery-acceptance.md)。以下保留本子阶段当时的范围、原证据及失败记录。

本批是N5-06的迁移命名路径验收，N5-06仍进行中，主线35/53、剩18；全部295项交付继续。失败阶段回退、跨重启和真实Agent checkpoint/审批恢复联动尚未验收，不能据此关闭整项。

## 行为

GET /api/v1/workflow-runs/{run_id}/migration 给出可核验v1固定计划的摘要、原版本、当前caseDict修订和事件序号。POST /api/v1/workflow-runs/{run_id}:migrate 必须提供Idempotency-Key、这些观察值、confirmed_reset=true，以及明确的新目标、目录、注册参数和预算。注册新计划不需要迁移；损坏或未知旧图需要人工核对，不猜测或重放未知命令。

迁移前检查旧流程和步骤已经结清、来源归属、所有相关尝试终态及停止/写入排空证明、无其他活动流程/作业。新流程从A开始，使用当前输入重新准备，保留原流程/步骤/尝试及原始计划。阶段回退调用正式工程阶段入口，保留错误门控和文件事务；后续B/C/D/E逐级确认继续生效。v4计划持久记录旧流程版本及计划、原输入、步骤摘要；调度时继续核对来源绑定。同键重放返回原新流程，不再次回退或创建作业；同键不同请求拒绝。

## 实际验收

两种组合均使用独立真实PostgreSQL；FS直接使用隔离目录，S3使用自建真实SeaweedFS。旧v1流程从B开始，固定Python命令真实运行一次并exit23；在运行期间迁移及旧计划公开调度均拒绝。旧源几何体边长1，迁移前改成边长2并破坏当前旧argv脚本；新注册geometry.export实际运行CadQuery，STEP体积为7.999999999999998，允许1e-9浮点误差。新流程明确从B回到A，等待B确认，确认后成功。同键完成前后仍是同一流程，无额外作业/修订；旧SQL历史所有字段摘要保持一致。

拒绝检查覆盖未确认、旧版本/计划摘要/当前修订/事件变化、缺少幂等键、额外命令字段和其他用户。四类单独标注的协议故障是未知计划、损坏步骤依赖、错误活动尝试归属、缺失排空证明；不替换原生结果或模型结果。新流程调度拒绝随后改变的来源版本，故障恢复原字段及时间戳。

| 组合 | 原生/API证据 | 真实模型HTTP |
|---|---|---:|
| FS + PostgreSQL | tmp/live-cli-startup-13qg9er6/workflow-plan-migration.json | 2 |
| S3 + PostgreSQL | tmp/live-cli-startup-un269hwm/workflow-plan-migration.json | 2 |

真实服务api.deepseek.com / deepseek-flash，本批4次HTTP200，reported tokens 17248，模拟0。模型实际读文件并原样回读此原生验收JSON的摘要；这不能证明模型自主执行迁移、审批或恢复工程操作。临时PG已删除、API及S3服务已退出，开发数据库只读凭据来源、未修改。

首轮tmp/live-cli-startup-xfn06s4c在故障恢复后完整历史比对失败：测试夹具未恢复ORM自动变更的updated_at。修复夹具后两种组合通过；该首轮失败保留，模型0，不改为通过。

## 复现和审查

入口platform/server/live_llm_workflow_plan_migration_acceptance.py --source-profile-id 26 --postgres，可加--s3。从server目录使用.venv/Scripts/python.exe -X utf8 -B。已审查入口及导入辅助代码：live_llm_job_repeat_acceptance、live_llm_cli_startup_acceptance、live_llm_acceptance、testsupport、workflow_migration_smoke（只调用alembic）、s3testsupport、cli_startup_deadline_checks（默认场景未调用）；模型收发包装只记录真实调用，不生成推理结果。未执行历史模拟回归。

后续继续原N5-06失败回退/文件时间线与Agent checkpoint恢复去重，跨重启不绕开确认、错误门控或重复执行审批工具。未知旧计划的用户可见人工操作仍按原N5-07交付。
