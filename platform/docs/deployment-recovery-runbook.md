# 联合部署备份与隔离恢复手册

适用于本项目单 Docker 主机的 PostgreSQL、Redis、FS 或 SeaweedFS、API、worker、Web 栈。数据库与文件/对象卷必须来自同一个停止写入的时间点，provider 原加密密钥也必须保留。这里只描述已实现部署形态；其他 S3 产品、物理多主机及商业外部后端继续按各自功能任务验收。

## 数据和程序的对应关系

| 内容 | 备份来源 | 恢复方式 |
|---|---|---|
| 数据库、账户、作业、会话、发布引用、加密模型配置 | `pg_dump --format=custom --no-owner --no-acl` | 新空 PostgreSQL 数据库用 `pg_restore --exit-on-error`；核对原 Alembic revision |
| S3 实际对象和版本清单 | 停止 SeaweedFS 后的整个数据卷 | 恢复到新卷；保持服务别名 `seaweedfs`，避免破坏其内部地址引用 |
| FS、原生私有状态、运行配置、索引、Redis AOF | 停止业务与存储写入后复制全部对应命名卷 | 每卷独立 tar，保留权限与符号链接；API/worker 的独立卷也分别恢复 |
| provider 加密密钥与部署配置 | 原 Docker secret 文件和 Compose 解析结果 | 私有副本；恢复服务使用同一原密钥，绝不重新生成 |
| 应用和工具链 | 原容器 image ID | 使用已验证的相同镜像；跨机器迁移前需将镜像另行保存/传输并校验 |
| 公共审查源码 | `tools/release_package.py` | 与敏感恢复备份分开；不包含账户、模型密钥、数据库或卷 |

`scripts/deployment_snapshot.py` 不导出活跃 PostgreSQL 的原始数据目录，不执行 `down -v`，不覆盖旧容器、卷或备份目录。它只处理显式 `container_name` 的已解析 Compose JSON；非 secret 的 bind mount 会被拒绝，避免漏备份宿主目录。恢复只接受新 `vf-restore-...` 项目名。

## 制作一致备份

先从应用停止提交新工作，结束已有任务和 CLI 会话。脚本检查无 queued/running 作业、CLI 会话仅为 closed/failed；还有待恢复会话时先按原会话恢复入口处理，不能跳过检查。

在 `platform/` 下生成私有 Compose JSON；此文件包含解析后的密码，不能在聊天或公共日志中输出。以下使用 Python 写 UTF-8 字节，兼容 Windows PowerShell：

```powershell
New-Item -ItemType Directory -Force private | Out-Null
server/.venv/Scripts/python.exe -c "import subprocess; from pathlib import Path; Path('private/deployment.json').write_bytes(subprocess.check_output(['docker','compose','--env-file','infra/.env','-f','infra/docker-compose.prod.yml','config','--format','json']))"
server/.venv/Scripts/python.exe scripts/deployment_snapshot.py snapshot --compose private/deployment.json --destination backups/deployment-YYYYMMDD
```

上例对应默认 FS 三服务部署。生成 JSON 时使用与运行实例一致的项目名、覆盖文件和 profile。启用独立 worker 时在 `config` 前加入 `--profile distributed`，使用栈内 SeaweedFS 时再加入 `--profile s3`。脚本在停止服务前按 Compose 项目标签核对所有运行实例，漏列 worker 会直接报错。外部对象存储需要由管理员协调该存储服务的快照。

默认配置实际演练从空 PostgreSQL 迁移开始，在独立项目中还原账户、算例及 FS 原文件。恢复后更新副本，源文件仍与备份前逐字节一致。三项检查与漏列 worker 的拒绝记录位于 `tmp/default-fs-recovery-a1z4z6yo/` 的 result.json 和 preflight.json。

脚本依次停止 worker/API/Web/Redis/SeaweedFS，检查写入已停止，导出数据库并复制命名卷。完成或中途失败都会尝试启动原服务。`manifest.json` 的 `complete` 与 `source_restarted` 必须都为 true；再检查原 URL 的 `/api/health` 和容器健康状态。任何失败都保留现场，不重放同名备份。

备份目录的 `private/` 包含原配置、原密钥、数据库与用户数据，按部署备份权限保管，不加入源码仓库或可审查发布包。摘要清单用于校验复制完整性，不代替对备份来源的信任。

## 在独立环境恢复并验收

先选择未使用的项目名与本机端口，保留原环境：

```powershell
server/.venv/Scripts/python.exe scripts/deployment_snapshot.py restore --snapshot backups/deployment-YYYYMMDD --project vf-restore-YYYYMMDD --port 57710
```

脚本先核对所有备份摘要，再创建独立网络、容器、命名卷与仅绑定 127.0.0.1 的 Web 端口。它恢复八类命名卷（以实际配置数量为准），在新空 PostgreSQL 中还原 dump，核对 Alembic revision，按存储→API→worker/Web 顺序启动。镜像使用备份记录的精确 ID；镜像不在目标主机时先导入原镜像，不临时换成 latest。

恢复后的验收至少包含：

1. 原账户能登录；原对话、作业/发布引用仍存在。
2. 原文件和不可变发布通过 API 下载，与备份前字节摘要一致。
3. 新文件能写入和读回；新对话与作业能持久化。
4. 使用备份前已有的加密模型配置调用真实授权服务；不要新导入密钥来掩盖原密钥恢复失败。
5. 模型工具确实读取新随机文件，回合与会话关闭后停止证明通过。
6. 原环境健康、原文件没有因恢复演练被改写；结束演练后停止新项目容器，保留卷和证据供审查。

实际模型入口为 `server/live_llm_deployment_restore_acceptance.py`，必须先生成并核验真实模型源码审查清单。它使用前一次隔离验收账户，不读取模型 API key；旧加密 profile 必须能在恢复栈中解密并成功调用。该入口不是适合任意用户数据的自动验收器；外部部署应选其专用验收账户和已授权模型配置。

## 失败与回退

### 从作业输入恢复版本回退到前一版

本次验证的旧服务端归档为 `mainline-N5-07-native-lineage-real-ui-20261110.zip`，其 Alembic head 是 `c20e87f143b9`。新版本的 head 是 `d31f98a254ca`，新增 `job_input_recoveries` 表，记录原作业与替代作业之间的关系。

1. 停止 API、worker 和其他写入，按上文备份 PostgreSQL、文件或对象卷、JWT 与 provider 密钥，并保留旧镜像或旧版服务端及校验值。记录数据库当前 revision、备份时间和预期回退时允许舍弃的升级后写入。
2. 在隔离副本上用新版执行 `alembic upgrade d31f98a254ca`。启动新版后，从原组织账户验证成员、共享工作区、文件和原 provider 配置。准备回切前再次停止写入，单独保存升级后状态。
3. 需要保留升级后写入，且存储格式仍与旧版兼容时，用**包含本次保护的新版本**在已备份的目标数据库执行 `alembic downgrade c20e87f143b9`。恢复关系表为空时允许降级。只要已有一条恢复历史，迁移即终止，保留 revision 与记录。可继续启动新版，通过原作业或替代作业的详情查询恢复关系。
4. 需要退回升级前时间点时，将升级前联合备份恢复到独立数据库和存储，以对应旧版本启动。校验所有表与序列、原文件摘要、账户和组织访问权限，以及凭据解密和真实模型文件读取。升级后独有的数据保存在另外保留的升级后副本中。完成业务核对后再切换访问入口。

文件存储和 SeaweedFS 均在 PostgreSQL 上实测了以上两条回退路径。SeaweedFS 使用同一停机备份分别启动升级副本和回滚副本。SQLite 另用已有两条恢复关系的数据库副本检查了拒绝降级时的全库完整性。[升级、降级与冷备恢复证据](FF-J12-completion-review.md)

快照失败：保留 manifest、已导出的私有文件及原日志，确认原服务已启动且健康。未完成快照不得恢复。若 Docker 不可用导致自动启动失败，先恢复引擎连接，再逐个启动原 SeaweedFS、Redis、API、worker、Web；不删除命名卷。

还原失败：保留新项目及恢复记录，先查 `docker compose ... ps` 与目标服务日志。脚本拒绝复用已有目标项目，不能把同一命令当作从零重试；应在检查明确失败位置后继续该现场，或者另选一个全新恢复项目。原环境仍保留。观察超时不等于原操作结束，须查询原进程/容器状态再决定下一步。

升级前先完成上述一致备份。升级数据库使用经过审查的 `alembic upgrade head`；同版本恢复只验证 revision，不修改 schema。版本回退需要匹配的旧镜像、数据库、存储及密钥，不能只退代码、只改 S3 HEAD 或盲目 stamp。跨版本升级/回退验收按相应迁移与最终生产任务继续执行。

本工具的范围是单机 Compose 联合冷备份与独立恢复，未声称提供在线增量备份、自动高可用切换、远程备份仓库、任意对象存储导出或物理多机一致快照。

## 2026-11-18 实际联合恢复

从运行中的 `vf-report-s3-20261117` 制作一致备份，检查无运行作业与活动 CLI 后停止写入，导出 PostgreSQL 并复制 8 个命名卷。原服务启动后，57700 与 62883 的 `/api/health` 均为 200。备份总计 1,014,763,498 字节；manifest 为 `tmp/deployment-snapshot-20261118/manifest.json`，原数据库 revision 为 `d31f98a254ca`。

随后用全新 `vf-restore-20261118` 项目、端口 57710、独立网络与命名卷完成恢复；数据库 revision 保持，6 个服务启动。结果为同目录 `restore-vf-restore-20261118-result.json`。备份的 `private/` 包含实际密钥和数据，不能公开或放入源码包。

最终实际模型验收 `tmp/live-deployment-restore-1f693cfb2ec0/result.json` 的 5 组检查全部通过：原账户/对话/S3 文件、三份不可变 PDF 哈希、旧加密 DeepSeek profile 的真实调用、新 PG/S3 写入与历史、CLI 会话关闭停止证明。使用备份前既有 profile 10，没有重新导入模型 key；服务为 `api.deepseek.com / deepseek-flash`，恢复后的 case 31、conversation 12、session `8b1b4ea6-a70a-4ec5-9200-68c160053b85`、turn `37ad6717-2c68-4f57-ab36-6f7853400d54`。真实模型执行 `cat Ref/proof.txt`，返回新生成且未放入提示的随机内容；CLI 报告总输入 17580、输出 218、总 tokens 17798，缓存输入 17024。一个真实模型回合，模拟 0；上游 HTTP 请求次数未独立计量。

两次验收脚本错误保留：`4300225a61e9` 的错误字段 `client_request_id` 导致 HTTP 422（实际字段是 `client_message_id`）；`50fea38558ab` 复用了先前算例名称而返回 409。两者都发生在模型回合准入之前，不计为模型成功；原 JSON 与对应审查哈希一致的失败源码保留在证据目录。修正为实际请求字段和独立随机算例名后通过，没有修改产品接口来迎合测试。

这次演练补齐联合恢复证据，不自动关闭 N7-05 的其他剩余条件或全功能灾备条目。为 C 盘空间维护而停止恢复栈时保留其卷、私有备份和验收材料；原运行栈由维护流程恢复。
