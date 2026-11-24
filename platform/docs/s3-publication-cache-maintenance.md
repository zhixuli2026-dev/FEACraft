# S3 发布读缓存维护

发布准备、作业收尾与兼容文件更新可直接校验 S3 的清单、对象版本及成员摘要。需要本机文件路径的读者仍可按发布 ID 从 S3 重建本节点缓存。数据库发布记录、远端发布原件和作业历史继续作为恢复依据。

## 预览与回收

使用当前节点的数据库、S3 和 `VIBEFLOW_STORAGE_FS_ROOT` 配置。命令第一个参数是算例 ID，第二个参数是已经提交或已被后续版本替代的发布 ID。默认保留写入时间不足 30 天的缓存。预览会返回 `retained`、`absent` 或候选的 `review_sha256` 与 `candidate_bytes`，并保持文件原样。

```powershell
platform/server/.venv/Scripts/python.exe platform/scripts/maintain-s3-publication-cache.py 12 7ea2921b-ffa5-4fae-8893-f32f2a5fce74
```

清理前结束使用该缓存根目录的请求与作业，停用节点上的 API、worker 及其他访问者。缓存根目录独立的其他节点可继续读取保存于 S3 的发布。核对候选摘要后应用同一组参数。容器构建将脚本安装到 `/app/maintain-s3-publication-cache.py`。

```powershell
platform/server/.venv/Scripts/python.exe platform/scripts/maintain-s3-publication-cache.py 12 7ea2921b-ffa5-4fae-8893-f32f2a5fce74 --confirm-node-offline --review-sha256 <预览摘要>
```

应用前重新检查归属、远端全量摘要与本地文件身份。遇到内容变化、未知文件、链接、历史未登记目录或归档异常时保留现场。确认后先将缓存目录原子移到 `.evicting-<发布 ID>`，再逐一移除核验过的文件和空目录。若中途退出，重新预览并应用，回收留在该目录中的匹配文件。其他内容需单独检查。`evicted_bytes` 为已移除的逻辑文件字节，Windows 实际可用空间应单独采样。

维护完成后，通过当前发布和旧发布 ID 各读取一次，校对数据。`--retention-days 0` 可显式选中近期缓存，重建仍计入节点空间准入和请求预算。远端历史及逻辑文件旧版本的处理见[版本保留手册](storage-version-operations.md)。

## 已失败的发布副本

`--failed-copy` 预览从未变成可见版本的失败副本。服务先核对数据库的完成失败状态、无发布指针、原生进程和复制者退出、已结算的复制凭据，以及远端复制意图。只有原作业输出完整且与重复对象相符才进入候选。原作业归档、已成功发布的所有历史版本、日志和复制回执继续保留。

这项操作占用共享对象存储维护窗口。先结束所有从该存储访问该算例的请求和作业，再停用其 API、worker 和其他访问者。使用相同选项重新校验 `review_sha256` 后，以 `--confirm-storage-offline` 执行。工具先通过独立探针核验实际条件操作，再用 ETag 条件逐对象删除。若存储返回不明确的结果，重新预览剩余对象，以新摘要继续。`evicted_bytes` 以本次对象长度合计，存储桶版本或后端实际释放空间由相应存储规则决定。

## 验收记录

2026-11-24，在两个独立 API、共享 PostgreSQL 与独立节点目录下，认证 SeaweedFS 4.52 和 RustFS 1.0.6 各完成两个原生 CadQuery 作业。第二个作业运行时，两节点仍能核对上一代成果。提交后新成果成为当前发布，原 ID 仍返回历史数据。基线包括逻辑兼容文件删除、缺远端成员拒绝回收、未知本地内容保留、节点冷启动恢复，以及从构造的中途清理状态续做。两个后端的节点缓存分别实清 6,831 B、6,829 B。隔离 PostgreSQL 库和对象服务均已退出。四次真实 DeepSeek HTTP 200 共 18,248 tokens，模拟 0 次。真实模型通过 API Agent 选择 `read_text_file` 读取维护后的算例文件。[SeaweedFS](../../tmp/live-j15-s3-primary-qu1i93t0/publication-maintenance.json) · [RustFS](../../tmp/live-j15-s3-primary-msswjk5b/publication-maintenance.json) · [RustFS 官方发布](https://github.com/rustfs/rustfs/releases/tag/1.0.6)

Garage 2.5.0 在独立认证环境对重复的条件创建和错误 ETag 更新返回 200，对错误 ETag 删除返回 204 且删除了探针对象。平台要求实际执行写入及删除条件，这一版 Garage 不纳入当前写入支持范围。兼容性需以条件探针结果判断。[Garage 实测](../../tmp/ff-j15-garage-2.5.0/capabilities.json) · [官方发布入口](https://garagehq.deuxfleurs.fr/download/) · [S3 条件操作](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-requests.html)

最终复验在两个真实存储各完成 11 项。第三个 CadQuery 作业在原生执行、输出归档和成果复制后触发提交故障，数据库记录失败。移除隔离桶的一个原归档对象时，工具拒绝回收并保留副本。还原原件后，实际条件删除失败副本共 6,799 B（SeaweedFS）、6,797 B（RustFS）。原作业输出摘要及两代已发布成果保持，冷重启后读回通过。最终 4 次真实 DeepSeek HTTP 200 共 18,256 tokens。两轮实际合计 8 次、36,504 tokens，模拟 0 次。内部发布包通过对工具、两种回收服务和 Docker 构建入口的逐文件校对。实际清理仅在隔离桶和节点目录执行。[SeaweedFS 最终复验](../../tmp/live-j15-s3-primary-3no5kd89/publication-maintenance.json) · [RustFS 最终复验](../../tmp/live-j15-s3-primary-n357dzb2/publication-maintenance.json) · [发布包核对](../../tmp/ff-j15-release-final-20261124/result.json)

FF-J15 的剩余范围为目标部署栈与跨物理宿主验收。已提交发布及原始作业归档继续保留，无法证明归属或尚有写入的未完成归档继续走原恢复路径。N7-01 的第二台测试机按用户决定暂缓。
