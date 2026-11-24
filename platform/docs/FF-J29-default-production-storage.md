# FF-J29 生产默认存储与本机作业

2026-11-20，FF-J29（WP01 / R0，T01）完成验收。正式 `docker-compose.prod.yml` 在未提供存储和分布式开关时渲染出 PostgreSQL、API 和 Web，共用生产配置中的 `casedata:/data/files`。隔离运行采用相同配置，给容器和卷使用独立项目名，并把 Web 映射到动态本机端口。[生产 Compose](../infra/docker-compose.prod.yml) · [配置验证](../infra/compose_smoke.py)

| 检查 | 结果 |
| --- | --- |
| 启动与写入 | 从空 PostgreSQL 库执行 Alembic。经过实际 Nginx 注册、创建工作区和算例，保存 2,097,186 字节。公开下载和容器内 `/data/files` 的 SHA-256 一致。 |
| 权限 | 独立注册的另一账户读取和覆写同一文件均返回 404。 |
| 本机执行 | Redis 和 SeaweedFS 均未启动。容器内的真实 Python 作业读到同一文件摘要，作业执行者为该 API 容器。第二个作业运行期间可保存同账户另一算例，随后取消作业并验证所属进程已停止。 |
| 错误配置 | 在独立启动中显式选择 `s3`，保持 `VIBEFLOW_ALLOW_PARTIAL_S3=false`。API 在创建用作探针的 SQLite 数据库前退出，默认 FS 服务仍健康。 |
| 重建恢复 | 实际替换 API 容器后重新登录。两个算例文件及 `succeeded`、`cancelled` 两条作业记录均保留。 |

默认栈专项调用真实模型 0 次，模拟 0 次。既有 PostgreSQL、Redis、默认 FS 的整栈证据另覆盖真实 DeepSeek 工具回读，显式开启 S3 的工程部署另有独立验收。[本轮结果](../../tmp/prod-default-storage-svx97yfd/result.json) · [分布式 FS 与真实模型](docker-deployment-acceptance.md) · [显式 S3 部署](live-linux-engineering-acceptance.md)

验收复用 `vibeflow/api:engineering-templates-v3-20261118` 的依赖层和 `vibeflow/web:reader-error-20261117` 的已构建 Web 镜像。将本工作区的后端源码、迁移和依赖声明装入隔离派生镜像并校验已安装版本，不联网重建依赖层。第一次重建测试因 Nginx 重启后获得新端口、测试客户端仍使用旧端口而超时。复验重新读取实际端口后通过。最终删除了隔离项目的容器、网络、卷、临时密钥文件和派生镜像标签。原有 11 个服务容器继续运行。[依赖版本记录](../../tmp/prod-default-storage-svx97yfd/image-build.log) · [端口超时记录](../../tmp/prod-default-storage-5yx1fe1n/result.json)

重跑使用服务器虚拟环境执行 `prod_default_storage_acceptance.py --api-image vibeflow/api:engineering-templates-v3-20261118 --web-image vibeflow/web:reader-error-20261117`。该功能沿用现有生产配置和存储格式，无迁移或配置变更。生产恢复与存储切换使用已有联合备份流程。[验收入口](../server/prod_default_storage_acceptance.py) · [部署与恢复](deployment-recovery-runbook.md)
