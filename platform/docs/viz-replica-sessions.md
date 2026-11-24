# 可视化会话跨副本与重开

## 部署

每个 API 实例使用固定且唯一的 `hostname` 和独立私有地址，每个地址运行一个 API 进程。各实例共用 PostgreSQL、算例存储、JWT 密钥和网站登录配置。将 `VIBEFLOW_VIZ_OWNER_ROUTES` 配置为所有 API 实例的主机名到私有 HTTP(S) 地址的 JSON 字典，例如 `{"api-a":"http://api-a:8000","api-b":"http://api-b:8000"}`。这些地址指向对应实例。对外网关可将可视化 HTTP、WebSocket 和关闭请求发往任一副本。

接收请求的副本校验用户和算例归属，从持久记录读取当前所有者。持有渲染进程的副本再次校验权限和原生进程身份。浏览器始终使用站点内的 `/viz-proxy/{sid}`。可视化日志使用端口和会话 ID，以区分不同副本恰好占用相同端口的情况。未配置路由表时沿用单实例路径和日志名。

## 重连和恢复

- 从可视化列表或会话历史打开已运行的会话。刷新页面后可以重新打开原会话。
- 正常停机或空闲回收完成后，历史记录显示已核验的停止结果。选择「按原数据源启动新会话」可用原文件、求解目录和引擎重新渲染。此操作会分配新会话，原有记录可继续查看。当前相机或场景状态可通过既有场景保存功能持久化。
- 强制中止后，未核验的会话保持恢复核查状态。来源重开入口要求原会话有完整停止证据。管理端可保留原记录进行核查。跨物理宿主与生产滚动部署按 N7-01 和 FF-J14 验收。

## T06 实测

本机两套 Linux API 进程使用共享 PostgreSQL 16 和算例存储。Edge 经另一副本打开原生 ParaView 两次，每条连接均收到 35,151 字节的图像帧。旁路账号访问返回 404。关闭、重复关闭、两端读回同一退出证据、无交互后约 13.4 秒回收通过。正常停机后，Edge 经历史记录在剩余实例重新打开原 VTP。实际强制停止时，对端阻止转入原生进程和未经核验的重开。实际模型与模拟模型均为 0。 [验收](../../tmp/FF-D26-replica-viewport-20261123/replica-acceptance.json) · [跨副本画面](../../tmp/FF-D26-replica-viewport-20261123/peer-viewport.png) · [历史重开](../../tmp/FF-D26-replica-viewport-20261123/reopened-from-history.png)

验收期间复用已有镜像。测试完成后移除本轮专用容器、网络和数据卷。Windows 实际空间采样与 Docker 占用分别记录。 [Windows 前](../../tmp/FF-D26-replica-viewport-20261123/before-c.json) · [Windows 后](../../tmp/FF-D26-replica-viewport-20261123/after-c.json) · [Docker 前](../../tmp/FF-D26-replica-viewport-20261123/before-docker.txt) · [Docker 后](../../tmp/FF-D26-replica-viewport-20261123/after-docker.txt)
