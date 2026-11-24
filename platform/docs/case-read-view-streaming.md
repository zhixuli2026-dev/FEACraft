# N4-05 S3 算例读取视图

使用 S3 的算例读取视图现在按已选定的长度和 ETag 分块读取。现有四路并发和整视图磁盘预留继续适用。每个文件只下载一次，落到同卷的私有暂存文件，核对长度和 SHA-256，刷入拥有者清单，再以无覆盖移动安装到视图。视图交给读取方之前与读取结束后都会重新核对选择的版本。Windows 文件系统由读取方直接打开原目录。[视图实现](../server/app/services/case_read_view.py) · [S3 条件读取](../server/app/services/attempt_objects.py#L73) · [私有缓存](../server/app/services/private_cache.py#L154)

## 2026-11-19 实测

| 后端 | 大文件及结果 | Python 分配峰值 | 回执 |
| --- | --- | ---: | --- |
| Windows 文件系统 + SQLite | 40 MiB 文件和空文件的长度、SHA-256 一致 | 2,119,175 B | [文件系统](../../tmp/case-view-fs-0wq74pnf/case-view-streaming.json) |
| 带认证的 SeaweedFS 4.52 + SQLite | 40 MiB 文件只用一次条件 GET，单次正文读取最多 65,536 B，缓存与清单摘要一致 | 2,448,449 B | [S3](../../tmp/case-view-s3-oitentuj/case-view-streaming.json) |
| SeaweedFS 3.99 + SQLite 旧算例 | 空文件条件 GET 实际返回 412，兼容回读返回 200，返回 ETag 和空文件摘要核对通过 | — | [旧版空文件](../../tmp/case-view-legacy-ltlts9wj/case-view-streaming.json) |

在 SeaweedFS 4.52 的控制场景中，共享期限到达时已从实际流读出 131,072 B，响应正文随后关闭，消费方未进入视图。另一次在读取中替换源对象，原正文仍按固定版本完成，随后版本复核返回 `input_stale`。新请求读取到替换后的内容。每项完成后均观察到 0 个剩余缓存目录和 0 B 预留。这里测量的是视图创建、消费和退出中的 Python 分配峰值。

首轮验证期限到达、外部替换和正常退出。三个隔离 API 的原进程均确认退出，两个对象存储实例已停止。测试数据在 D 盘，C 盘可用空间为 22.21 GiB 至 22.20 GiB。真实和模拟模型调用均为 0。[专项复验](../server/case_read_view_streaming_acceptance.py)

## 下载进程骤停后的清理

同日补齐了已声明的下载暂存文件恢复。打开独占创建的 `.part` 后，先把唯一名称、准入长度和本地文件身份写入并刷盘到缓存清单，再读取 S3 正文。文件身份包含设备、文件编号和创建时间。清理仍要求原进程已退出，并通过实际删除句柄再次核对被清理文件的身份和大小。最终安装的文件继续使用完整 SHA-256。没有身份记录、身份不匹配、超出声明长度或无法获得可信创建时间的暂存文件保留供核对。[实现](../server/app/services/private_cache.py)

使用认证的实际 SeaweedFS 4.52，在三个并发下载均已写出 65,536 B 暂存内容后，终止真实 API 进程。进程仍存活时，维护扫描保护三个暂存目录，观测到 125,830,026 B 预留。新进程恢复时移除身份相符的原始暂存，保留字节相同但文件身份不同的替换文件，也保留缺少身份记录的旧格式暂存。再次运行恢复结果一致，预留归零。新视图完整读回原 40 MiB，保持单次条件 GET 和 64 KiB 分块，Python 峰值 2,310,870 B。[进程损失回执](../../tmp/case-view-process-loss-zhi50fqz/case-view-process-loss.json)

现有私有缓存的 24 项退出、脏文件、硬链接和身份隔离回归均通过。D 盘 WSL 映像内的 ext4 实测文件创建标记在写入前后稳定，也能识别同字节替换。WSL 通过 9p 挂载的 `/mnt/d` 未返回创建标记，采用保留路径。两个隔离 API 和对象存储均已退出。C 盘前后可用空间四舍五入均为 22.20 GiB，真实及模拟模型调用为 0。[既有回归](../../tmp/private-cache-recovery-ju6adm_4/result.json) · [本地文件身份验证](../../tmp/case-view-process-loss-zhi50fqz/linux-file-identity-check.json) · [复验入口](../server/case_view_crash_recovery_acceptance.py)
