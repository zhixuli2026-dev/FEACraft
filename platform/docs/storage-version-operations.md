# 存储恢复、版本保留与停机回收

## 先识别需要处理的数据

| 类型 | 已有处理入口 | 保留规则 |
| --- | --- | --- |
| 文件提交前后现场、未知提交、隔离副本 | [文件版本与人工恢复](manual-file-recovery.md) | 先诊断；缺证明不删除，不凭文件年龄推断 SQL 是否提交 |
| 未完成上传、已提交后的残留块 | [未完成上传恢复](unfinished-upload-recovery.md) | 续传或明确取消；不因超时自动取消；完成回执匹配后清理残留 |
| 新建/导入/复制的准备目录与历史 | [启动维护](storage-maintenance.md) | 完整且有归属的暂存默认保留一天；历史默认永久保留；占用与提交证明不随历史过期删除 |
| 作业输入、输出、发布版本与人工隔离证据 | 对应作业恢复与人工处置入口 | 本工具不回收；不能把这些目录当成普通缓存 |
| S3 当前文件的旧不可变版本 | 本文停机工具 | 默认保留30天；只回收当前清单未引用、路径及版本可核验的 live-store 对象 |
| S3 发布的节点读缓存 | [发布缓存清理](s3-publication-cache-maintenance.md) | 默认保留 30 天。按算例和发布 ID 核对 SQL、远端对象与本地清单后清理节点副本 |
| 派生索引 | [索引重建](shared-index-deployment.md) | 可从当前文件重建，不代替业务数据备份 |

账户维护入口是 `GET/POST /api/v1/case-provisions:maintenance`；它处理已实现的恢复与准备残留，不自动执行本次 S3 版本回收。FS 后端继续使用文件事务与人工隔离，不存在需要本工具压缩的 S3 live-store。

## 停机窗口与备份

这是显式停机维护，不是后台 GC。先结束活动作业、会话、上传和恢复操作，停止所有连接该存储的 API、worker、CLI 写者与下载读者。已签发的下载链接和读取快照可能引用旧物理版本，维护时必须停止或使其过期。远端请求若结果未知，先完成原恢复/隔离，不以保留期代替退出证明。

联合保留同一停机点的数据库、完整对象存储、原 provider 密钥、运行配置及对应源码版本。数据库备份不能代替对象备份；工作区 ZIP 不能代替完整存储备份。先核对当前算例前缀，不能猜测算例 ID 的路径：新建算例可能带随机目录段。

工具只操作一个明确的算例前缀。当前文件的物理版本或大小异常、待恢复文件事务存在、清单变化时拒绝继续。未知目录保持原样。默认30天是普通旧版本的保留策略，不是操作仍活动时允许删除的期限。

## 本机操作

下列命令从项目根目录运行。存储配置复用 `platform/server/.env` 和进程环境；需明确配置 S3 后端。凭据不写在命令行、计划或输出中。

```powershell
# 仅预览；计划文件必须是尚不存在的新路径。
platform/server/.venv/Scripts/python.exe platform/scripts/maintain-s3-versions.py plan --case-prefix workspaces/1/cases/实际目录/1 --retention-days 30 --plan D:/backup/case-version-plan.json

# 核对计划中的 endpoint、bucket、prefix、候选对象/字节数、保留数及删除记录数。
# 完成停机、联合备份后，将预览输出的 plan_sha256 填入下面参数。
platform/server/.venv/Scripts/python.exe platform/scripts/maintain-s3-versions.py apply --plan D:/backup/case-version-plan.json --plan-sha256 预览摘要 --confirm-offline --result D:/backup/case-version-result.json
```

`--retention-days 0` 允许回收所有未引用的已存在旧版本，包括刚生成的版本；仅在明确需要、完成备份和停机的场景使用。默认不会如此运行。预览不迁移旧无清单命名空间、不写 HEAD、不删除数据。应用时重新计算候选、核对原摘要/ETag，并实际验证后端的条件删除能力；清单变化须重新预览。

容器镜像中同一工具位于 `/app/maintain-s3-versions.py`。停止业务服务后，可用共享相同存储配置的临时 API 镜像运行 `python maintain-s3-versions.py ...`，计划和结果目录需显式挂载。容器内停机回收仍待目标环境验收。

## 压缩、重试与混合版本

应用先通过 CAS 写入新的清单 generation，将当前文件引用原样保留，移除逐路径删除记录；随后仅对预览中未被引用的旧物理对象执行带版本条件的删除。HEAD 永久保留。正常写入仍为删除操作生成身份，下一次显式维护才压缩它们。

旧 v1 清单继续可读写；第一次压缩将该算例升级为 v2，旧版读取器会拒绝它。升级前的单文件或批量请求不能跨 generation 重试；迟到的旧 HEAD 请求也因 CAS 失败而不能使删除文件重新出现。单纯删除 tombstone 而不改变协议版本及 generation 不安全，因此工具不提供这种模式。

部署前必须让所有读写实例支持 v2，再开始压缩。不能在仍运行旧实例时滚动压缩，也不能仅回退代码或把 HEAD 改回 v1。回退需恢复停机点一致的原应用、数据库和完整存储备份。已回收版本不能靠恢复旧 HEAD 找回来。

执行结果 `complete` 表示本次计划删除请求均成功；`partial` 保留逐对象错误。进程中断可能留下 `started`，异常留下 `incomplete`，都不能当作完成。保留原计划/结果，检查当前 HEAD 和对象并重新生成计划；已完成的删除自然不再进入候选，新的 generation 继续阻止旧请求。不得盲目重放旧计划、恢复旧 HEAD 或删除未知文件。迟到的旧不可变对象上传至多留下新孤立对象，需后续新计划处理。

恢复业务后，从两个实际使用入口检查当前文件、保存新编辑、搜索及相关作业恢复；模型功能只使用真实服务验收。该工具完成 live-store 版本回收和删除清单压缩，不宣称已覆盖全部 FF-J15 私有缓存/归档淘汰或物理多主机故障矩阵。

## 2026-11-16 实际验收

`tmp/live-s3-version-maintenance-psbt3e_s/maintenance-result.json` 整轮通过：实际 SeaweedFS 4.52、旧版本覆盖与删除、30天默认保留、未知对象保留、零天显式回收、v1/v2 旧请求拒绝、上一审查包的真实旧读取器拒绝 v2、过期计划及待恢复事务拒绝。实际 API 生成的旧版本通过正式 CLI 预览并回收；两个 API 服务进程均实际退出，重启后可继续读写 v2。

真实模型验收使用 `api.deepseek.com` / `deepseek-flash`：两次 HTTP200、8,548 tokens，真实工具读取维护后保留的随机文件内容，并形成持久回复；模拟调用0。合计实际回收1,671字节是隔离小样本结果，不是容量或性能上限验收。

`tmp/s3-attempt-store-mbm5j_rm/version-resume-result.json` 另有5项检查：真实条件删除后注入响应丢失、旧计划拒绝重放、新预览只选剩余对象、续做后当前字节一致、无剩余孤立版本。这是明确标识的通信故障注入，未声称真实进程崩溃；模型调用0。两套自建对象服务均已退出。源码与辅助代码分别由640/641项哈希审查覆盖，旧模型模拟集合未执行。

以上完成 N7-05 原“存储恢复/版本保留回收/删除清单压缩手册与混合版本边界”交付组，并补齐对应 live-store 实现；FF-J15 整项不关闭。Docker 整栈、其他对象存储后端、跨物理主机以及全部私有版本淘汰仍按原任务保留。

## 认证 S3 部署验收

2026-11-26，生产 Compose 的 API、对端副本和 worker 统一读取部署选择的 endpoint、bucket、签名 region 与凭据。认证覆盖配方为 SeaweedFS 挂载私有身份文件，缺少凭据时 Compose 拒绝启动。本机隔离栈使用 SeaweedFS 4.52、共享 PostgreSQL、Redis、两套 Linux API 和 Nginx。管理员预建隔离桶，业务身份只获准访问这一个桶。其他桶和匿名访问均被拒绝。默认 FS 配方及镜像内 FS 初始化另行通过。 [配置步骤](deployment.md#认证-s3) · [认证覆盖配方](../infra/docker-compose.s3-auth.yml)

API 启动直接检查配置指定的桶。分开的实际 SeaweedFS 探针分别以 `us-east-1` 和 `us-west-2` 自动创建缺失桶，第二次启动只执行 `HeadBucket`。错误身份返回 403，未发出创建请求。探针模型调用 0 次。 [桶初始化回执](../../tmp/ff-j15-auth-compose-20261126/bucket-bootstrap.json)

隔离整栈通过跨副本原件读取及 Nginx HTTP 206。S3 不可变成员的 SHA-256 与 API 原件一致。两个真实 CadQuery 作业经 Redis worker 发布两代几何。对端本地缓存剩余空间不足时，几何接口返回 `node_storage_full`。扩充试验缓存后重建对端容器，当前文件和两代几何成果均从认证存储读回。真实 DeepSeek 在重建的对端用 `read_text_file` 读回算例标识并完成回答，2 次 `deepseek-flash` HTTP 200，模拟 0 次。 [整栈回执](../../tmp/j15-auth-compose-jr4g6_0u/result.json) · [验收脚本](../server/live_llm_authenticated_s3_compose_acceptance.py)

本机重建复用了已安装的运行层。隔离容器、网络、卷、临时凭据和两个候选镜像均已清理，原 11 个 Docker 容器 ID 一致。构建缓存由 5.819 GB 变为 5.85 GB，C 盘可用由 39,844,429,824 B 变为 39,835,971,584 B。FF-J15 仍待在目标环境复核其实际 S3 服务，并完成跨物理宿主验收。 [磁盘读数](../../tmp/ff-j15-auth-compose-20261126/disk-after.json) · [Docker 占用](../../tmp/ff-j15-auth-compose-20261126/docker-after.txt)
