# 远程集群作业

算例的「作业 → 远程集群」支持部署方配置的 SSH 调度入口。提交时选择已保存在算例内的 Bash 脚本、其他输入和预计取回的成果路径。输入路径相对算例根目录，每行一个。平台对输入记录长度和 SHA-256，以 SFTP 传入该作业专用目录。Slurm 使用 `sbatch` 取得调度 ID，OpenPBS 接口使用 `qsub`。墙钟限制由调度器执行。

平台作业 ID、调度 ID、输入摘要和续算来源保存在数据库。网络断开或页面刷新后，使用原提交键核对同一个作业。已受理的调度任务可在 API 退出后继续执行。重新打开任务时可刷新调度结果，并取回声明的成果和标准输出、错误日志。选择「取消作业」后，等待调度器确认停止。查看状态和下载文件都按算例与当前账户授权。

续算时选中同一集群上的历史作业，填写检查点的原成果路径。需要改名时写 `源路径 => 目标路径`。平台从已归档的成果复制并核对摘要，新的作业获得独立调度 ID。源作业和它已经保存的输出可继续读取。单文件限额 32 MiB，每个输入或输出组总限额 64 MiB。

## 部署

配置 `VIBEFLOW_REMOTE_HOSTS` 允许指定的 SSH 主机。可通过 `VIBEFLOW_REMOTE_HPC_CONFIG_PATH` 指向部署方私有 `profiles.json`，本地联调可用 `VIBEFLOW_REMOTE_HPC_PROFILES` 传递同一 JSON。凭据及 `known_hosts` 来自部署机文件，HTTP 请求只传配置名称。用户须是配置所绑定组织的管理员，并持有所选算例的写权限。

```json
{
  "engineering-slurm": {
    "org_id": 12,
    "scheduler": "slurm",
    "root": "/home/vibeflow/run",
    "queue": "engineering",
    "host": "scheduler.internal",
    "port": 22,
    "username": "vibeflow",
    "client_key": "/run/vibeflow/hpc/client-key",
    "known_hosts": "/run/vibeflow/hpc/known_hosts",
    "max_seconds": 3600
  }
}
```

准备非 root SSH 账户、其拥有的作业目录、可访问的队列、已核对身份的主机公钥和 SFTP。Slurm 依赖 `sbatch`、`scontrol`、`scancel`，调度历史需留够平台回收窗口。OpenPBS 依赖 `qsub`、`qstat -x -f -F json`、`qdel`，需启用作业历史并允许计算节点输出写回共享目录。保留 `known_hosts` 的绑定身份，替换主机身份后原作业进入待核对。 [Slurm 调度提交](https://slurm.schedmd.com/sbatch.html) · [OpenPBS 状态协议](https://github.com/openpbs/openpbs/blob/master/doc/man1/qstat.1B)

生产 Compose 可添加 `infra/docker-compose.hpc.yml`，通过 `VIBEFLOW_HPC_CONFIG_DIR` 指定包含 `profiles.json`、客户端密钥和已验证主机公钥的目录。叠加容器配置只读挂载目录。数据库需迁移至 `f71a46d08e9b`；降级前须处理所有远程作业记录。现有受限通用 SSH 入口继续按原契约提供。

## 已通过的验收

2026-11-24，实际 OpenSSH 公钥认证、SFTP 和 Slurm 接受同一幂等键并返回同一调度 ID。故意关闭首个 API 后，另一个 API 从 PostgreSQL 读取原 ID，通过调度器核验 `COMPLETED` 后取回 OpenFOAM 字段。认证 SeaweedFS 发布的 `0.02/U`、`0.02/p` 逐字节匹配独立运行。原检查点封存后由另一个调度 ID 续算到 0.03 s 和 0.04 s。运行中的作业收到取消后，读回实际 Slurm `CANCELLED`。其他组织账户无法读取该配置和作业。同宿主测试模型调用 0，模拟 0。 [实际 Slurm 结果](../../tmp/ff-c16-slurm-primary-x_l6y4e6/scheduler.json) · [续算日志](../../tmp/ff-c16-slurm-primary-x_l6y4e6/resume.log)

同一接口在 PostgreSQL 和文件系统上，用 OpenPBS `23.06.06` 完成相同提交、幂等、跨 API 读取、真实续算、原件回收和取消，结束状态由实际 `qstat` 核对。运行时从上游 `cd7ab5ed` 编译，实际 OpenFOAM 由 UID 1000 执行，字段摘要与 Slurm 回收值相同。两种调度器和临时数据库均已清理。 [OpenPBS 结果](../../tmp/ff-c16-pbs-primary-d_pfvh9u/scheduler.json) · [PBS 续算日志](../../tmp/ff-c16-pbs-primary-d_pfvh9u/resume.log) · [上游源码](https://github.com/openpbs/openpbs/tree/cd7ab5edaf03dcac2f1cb9e2f42d75eb117d468a)

最终复验在 SQLite 文件系统及 PostgreSQL 认证 SeaweedFS 核对取消后并发槽位归零，组织审计链包含同一任务的提交、取消与终结回执。Edge 实际丢弃提交响应，重载后以同一幂等键找回原调度 ID，完成文件字节核对及 390 px 读回。SQLite 在空隔离数据库通过迁移、回退和再迁移。内部发布包包含操作入口、迁移与 Compose 配置。所有验收在现有单物理宿主完成，模型调用与模拟均为 0。 `f71a46d08e9b` 已应用于隔离库，现行服务部署仍使用原版本。 [SQLite 与 Edge](../../tmp/ff-c16-slurm-primary-nn8i1nxn/scheduler.json) · [390 px 回执](../../tmp/ff-c16-slurm-primary-nn8i1nxn/hpc-browser.json) · [认证 S3 最终复验](../../tmp/ff-c16-slurm-primary-tsy3rthx/scheduler.json) · [数据库迁移](../../tmp/c16-sqlite-migration-p7juv8g7/migration.json) · [发布包核对](../../tmp/ff-c16-release-20261124/result.json)
