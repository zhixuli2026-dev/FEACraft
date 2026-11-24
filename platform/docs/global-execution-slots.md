# 工作区并发作业与可视化配额

2026-11-18，RB-16 已完成。共享算例的作业使用工作区所有者的配额。提交端可以接受排队；每个 worker 领取前都要在数据库的所有者锁内核对实际占用。取消后，运行数可以先降到零；只有原尝试的进程停止证据齐全，新的作业才能使用该名额。查询每批读取 32 行。

| 实测场景 | 观察结果 | 验收记录 |
|---|---|---|
| 两个独立 API，共享成员及所有者 | 成员自己的工作区满额仍能使用所有者的空位；所有者满额返回 429。取消后出现运行 0、占用 1。排空后继任作业只执行一次 | [SQLite / 文件](../../tmp/live-cli-startup-w_ayw74z/shared-job-slots.json)、[PostgreSQL / S3](../../tmp/live-cli-startup-j07z3z_6/shared-job-slots.json) |
| Windows 与 WSL Linux worker 同时领取，PostgreSQL、Redis、S3 | Linux 取得唯一名额，Windows 等候。原进程树停止后 Windows 开始执行。重启并重投两份原交付，重复领取为 0，最终占用为 0 | [首次](../../tmp/remote-worker-h9vr8euc/host-slots.json)、[最终复验](../../tmp/remote-worker-75tx7cqg/host-slots.json)、[临时数据库清理](../../tmp/remote-worker-postgres-23081562dbbf44bb96646f3a8d490087/result.json) |
| worker 租约过期、重复投递、进程退出与恢复 | 租约已过期但 worker 仍存活时保留名额，恢复使用原尝试并核对进程停止证明 | [PostgreSQL / 文件](../../tmp/live-cli-startup-2p4upomh/lease-registration.json)、[PostgreSQL / S3](../../tmp/live-cli-startup-8l7m1ljh/lease-registration.json) |
| 双 API 争抢一个可视化名额 | 只创建一个真实 VTK 内核，另一个请求返回 429。隔离数据库中损坏停止收据时继续占用；恢复原收据后第二个内核得以启动并正常关闭 | [SQLite / 文件](../../tmp/live-cli-startup-iq1646ll/viz-slots.json)、[PostgreSQL / S3](../../tmp/live-cli-startup-wcxqqcsk/viz-slots.json) |
| 设置面板 | Chromium 在渲染内核运行期间读取到可视化 1/1、作业 0/4；两个 API 均在内核关闭后报告零占用 | [浏览器复验](../../tmp/live-cli-startup-rm46kh54/viz-slots.json)、[画面](../../tmp/live-cli-startup-rm46kh54/renderer-quota.png) |

Windows 与 WSL Linux 使用同一台物理机、独立执行环境及 worker 身份。临时 PostgreSQL 访问规则限定在测试角色、数据库与 WSL 地址，结束时恢复。独立物理节点的部署验收由 N7-01 继续跟踪。[数据库访问恢复记录](../../tmp/remote-worker-postgres-23081562dbbf44bb96646f3a8d490087/postgres-access.json)

本轮 12 次通过的验收运行共记录 24 次真实 DeepSeek（deepseek-flash）HTTP 200，用于模型经工具读回结果。配额、进程退出及重投由数据库、worker、Redis 和 HTTP 实测判定。损坏停止收据属于隔离故障注入。12 次中含两种存储的 Redis 专业批次和一次 50+50+1 公平队列回归。[队列与批次](fair-queue-and-batch-reconciliation.md)

FF-J08 的四维额度、套餐到期及跨实例模型入场见最终交付记录。 [四维验收](FF-J08-effective-quotas.md) · [资源工作清单](resource-budget-worklist.md)
