# 本地项目 Redis

2026-11-06 更新：6380 曾在 Windows/WSL 同时拒绝连接，已用下述原配置恢复。Windows PING、唯一 TTL 临时键读写删除和 AOF 状态通过。用户授权自行选择 Docker 或恢复方案后，再次确认 Windows PING 为 true、aof_enabled 为 1、aof_last_write_status 为 ok，选择保留现有 WSL 实例。保留日志不能确认上次停止原因；仍未配置自动启动。恢复证据：`tmp/redis-restored-e566a59a10244e58b9f49afe216877a8.json`。下文任务数量为当时历史状态，实时进度以 `mainline-task-status.json` 为准。

2026-11-05 已为项目启动独立 WSL Redis，地址 `127.0.0.1:6380`。使用已经下载并通过真实 arq 验证的 Redis 7.0.15 包，在 `vibeflow-linux` 内以 `nobody`（UID 65534）运行。只绑定本机回环地址，开启 protected mode、AOF（everysec）和 RDB。此配置用于本机开发，不是对外托管部署配置。

原 Windows Redis `D:\redis\redis-server.exe` 在 `6379` 上运行，探测时响应正常；项目 `distributed_queue.py` 默认连接 `6380`，此前该端口没有服务。保留原实例，为项目使用独立端口和数据目录。没有修改开发数据库、重载端口 8000 或开启 `VIBEFLOW_DISTRIBUTED_JOBS`。

| 项目 | 位置 |
| --- | --- |
| 运行环境 | WSL `vibeflow-linux` |
| 程序与依赖 | `/opt/vibeflow-redis-6380/usr/` |
| 配置 | `/etc/vibeflow/redis-6380.conf` |
| 持久数据、PID、日志 | `/var/lib/vibeflow-redis-6380/` |
| Windows 连接地址 | `127.0.0.1:6380` |

在 PowerShell 中检查服务：

```powershell
wsl.exe -d vibeflow-linux -u nobody --exec env LD_LIBRARY_PATH=/opt/vibeflow-redis-6380/usr/lib/x86_64-linux-gnu /opt/vibeflow-redis-6380/usr/bin/redis-cli -h 127.0.0.1 -p 6380 PING
```

返回 `PONG` 表示服务可访问。WSL 或 Windows 重启后，可用以下命令重新启动；目前没有配置开机自动启动：

```powershell
wsl.exe -d vibeflow-linux -u nobody --exec env LD_LIBRARY_PATH=/opt/vibeflow-redis-6380/usr/lib/x86_64-linux-gnu /opt/vibeflow-redis-6380/usr/bin/redis-server /etc/vibeflow/redis-6380.conf
```

停止前用 `INFO server` 核对 `config_file` 是上述专用配置，再对该端口执行 `SHUTDOWN SAVE`；不要停止原 `6379` 实例。日志位于 `/var/lib/vibeflow-redis-6380/redis.log`。WSL 当前没有以 systemd 作为 PID 1，因此没有声称 systemd 服务或自动恢复已配置。

本次验证包括 Windows 客户端 PING、唯一带过期时间的键读写、AOF 开启且可写、受控重启后读取原键、原 Windows Redis 的实例身份保持不变。验证键已删除；首次启动命令因 WSL 的直接执行 PATH 找不到 `runuser` 失败，改为显式 `wsl -u nobody` 后重启验证通过。更早一次探测键由 600 秒 TTL 自动清理。

环境证据：`tmp/local-redis-6380-provision.json`、`tmp/local-redis-6380-verified.json`。安装脚本 `tmp/provision-local-redis-6380.py` 和已通过的验证脚本 `tmp/verify-local-redis-6380.py` 不应重复执行。日常使用上面的启动和状态命令即可。

此前 `tmp/mainline-N5-delivery-evidence/redis-result.json` 已记录 SQLite/PostgreSQL 各 5 项真实 Redis/arq 故障恢复验收；那些临时实例已清理，与本次常驻本地服务不同。此次没有重复完整故障矩阵，也不据此关闭跨主机、多副本、全局额度或任何未完成的主线任务。主线仍为 20/53 完成、33 项未完成。
