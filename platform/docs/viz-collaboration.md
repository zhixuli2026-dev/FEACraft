# 可视化协作查看

ParaView 5.11.2 的原生协作由启用多客户端的 `pvserver` 提供，主客户端可改动管线，其他客户端可观看并跟随指定相机。项目随附的 ParaView `collaboration.py` 处理多客户端 Server Manager 通知。平台 Web 工作台沿用单发起者和只读观看者的交互，在算例权限内传送共享的实际渲染帧。 [ParaView 多客户端文档](https://docs.paraview.org/en/v5.11.2/ReferenceManual/parallelDataVisualization.html) · [ParaView Collaboration Manager API](https://www.paraview.org/paraview-docs/v5.11.0/cxx/classvtkSMCollaborationManager.html)

## 使用流程

在有写权限的算例中打开自己的可视化会话，展开「协作查看」，选择「共享当前画面」。有读取权限的成员在各自的工作台打开同一算例，再选择「跟随画面」。发起者看到当前观看成员。停止共享会清空可读画面。重新打开自己的会话后，可在原分享仍有效时继续共享。

只读观看使用会话版本和单调递增的帧号。平台保存最新一帧，成员重连时读取当前帧。更换发起会话后使用新版本。过时的发布请求返回 409。授权对每次状态和图像读取重新检查。失去算例读取权限后，工作台隐藏已显示的共享画面。空闲过期后服务端清理画面和观看状态。

新部署通过 Alembic 迁移 `f73518fa653d` 创建共享状态和观看者表。多 API 实例共用数据库。Web 工作台的画面共享通过平台会话和 HTTP 传输。

## T06 实测

本机 Edge 使用三套独立账号，覆盖发起者、已授权只读成员和其他组织账号。实际 ParaView 渲染尺寸为 620×260，相机转动同步到观看者。SQLite 和双 API 副本共享 PostgreSQL 16 均通过。验证了发起者重连后继续发布、观看者重连接到最新图像、旧帧冲突、只读成员写入被拒、跨组织返回 404、撤销权限后隐藏图像及停止共享。实际模型和模拟模型均为 0。 [SQLite 回执](../../tmp/FF-D21-viz-collaboration-20261123/collaboration-edge.json) · [双副本回执](../../tmp/FF-D21-viz-collaboration-20261123/collaboration-postgres-edge.json) · [观看者实际画面](../../tmp/FF-D21-viz-collaboration-20261123/viewer-after.png)
