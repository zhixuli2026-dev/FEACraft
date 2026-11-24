# 任务提交与批次取消前的读取额度

2026-11-06：后处理提交前的幂等记录/成果分流读取，以及批次取消前的清单/作业读取，接入 `case_read_capacity.reading_before_action`。这两个入口的协调器可能恢复 S3 文件事务、写入栅栏，因此需要先取得工作区所有者的存储额度。

新范围要求调用方尚未产生业务修改。读取成功后先提交数据库事务，再退出所有者额度范围；后续任务准入或取消使用独立的资源生命周期。项目会话配置 `expire_on_commit=False`，因此提交后仍可使用已加载的 case、user 和 Job 对象。异常则结束事务并结算额度。原有纯读取接口继续使用 `reading`，不改变其回滚语义。

后处理分流读取与实际准备之间会重新取得算例锁。现有 `LegacyPostprocessHandler.prepare` 会拒绝已经采用注册求解成果的算例；注册处理器也会重新核验当前求解发布。因此本次没有把先前读取视为后续源状态不变的证明。

## 实际验收

显式入口 `platform/server/mixed_read_capacity_checks.py` 只运行实际基础设施和原生计算，不导入或调用模拟模型。测试辅助模块已审阅：隔离 API、真实 SeaweedFS、作业等待、临时 PostgreSQL 迁移及确定性的 OpenFOAM 输入生成。观察包装器只核对实际读取范围退出时的 SQL、额度和 ORM 状态，不替换业务结果。

| 场景 | SQLite | PostgreSQL |
| --- | --- | --- |
| 额度为零：拒绝请求，无新增作业和远端字节 | 通过 | 通过 |
| 不存在的批次返回 404，异常路径额度已结算 | 通过 | 通过 |
| 实际进程运行中的批次取消：运行项停止、排队项取消 | 通过 | 通过 |
| 重复取消不再变更状态 | 通过 | 通过 |
| 实际 blockMesh、laplacianFoam 和后处理提取，均温 350 K | 通过 | 通过 |
| 相同幂等键重复后处理提交，返回原作业、不新增作业 | 通过 | 通过 |
| 后续动作前 SQL 事务与额度释放，ORM 对象可读 | 通过 | 通过 |

证据分别为 [SQLite 批次](../../tmp/mixed-read-capacity-helrc0nk/checks.json)、[PostgreSQL 批次](../../tmp/mixed-read-capacity-vnwsgyp2/checks.json)、[SQLite 后处理](../../tmp/mixed-read-capacity-epilrtfd/checks.json)、[PostgreSQL 后处理](../../tmp/mixed-read-capacity-1i3kg72c/checks.json)。总计 18 条检查记录，不代表新增 18 项任务。

[批次临时库清理](../../tmp/mixedread-postgres-vvafeph9/cleanup.json)和[后处理临时库清理](../../tmp/mixedread-postgres-1s_b_qs6/cleanup.json)确认临时库删除；没有迁移开发数据库或重启开发服务。两个数据库各启动了一个实际批次进程、一个求解作业和一个后处理作业。排队项取消由其终态和完成状态验证，运行项还核对进程停止凭据。

## 剩余范围

本批覆盖 S3 后端的两个混合入口；没有重跑 FS 全矩阵，也没有重跑完整注册几何—网格—求解发布链、跨进程分流竞态或慢存储取消故障。后续写入阶段的全部空间峰值不因预读取修复而视为完成。

下一步继续上传状态读取、显式准备恢复、其他写入入口与未来上传额度交互，然后 ZIP/备份/导出峰值。RB-12 仍未完成，RB-13 至 RB-17 保持原范围。主线 33/53 完成、剩余 20 项；资源预算 11/17 完成、剩余 6 块；295 项全功能交付范围不变。本批没有 LLM 调用，历史真实模型验收次数不因这些基础设施检查增加。
