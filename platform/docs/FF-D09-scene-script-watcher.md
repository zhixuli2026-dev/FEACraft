# FF-D09 场景脚本监听

打开可视化会话后，保存算例中的 `D-post/scenes.py`，现场对象随下一版脚本更新。监听等待文件版本稳定 500 毫秒，读取前后核对同一版本。脚本在现有会话的命名空间运行，同名对象继续沿用原场景对象。执行结果出现在可视化工具栏，失败原因写入 `D-post/logs/viz_executor.log`。[渲染器](../server/app/services/viz_kernel.py)

每次执行重新提供当前场景文件的 `__file__` 和 `VIBEFLOW_PARAVIEW_SCRIPT`。`ViewSize` 在执行任何脚本语句前拒绝。文件读取、编码、语法和普通脚本异常进入执行日志，用户可在同一会话下一次保存后继续工作。编辑器的每次稳定文件版本各执行一次。[渲染器](../server/app/services/viz_kernel.py)

本地文件系统直接监听正式算例文件。认证 S3 的浏览器保存由现有文件事务同时记录已提交的脚本摘要。活动可视化会话跟随持久版本，校验归属、会话和真实正文摘要，再原子更新自己暂存区里的 `D-post/scenes.py`。脚本打开 `__file__` 读到本次实际执行的文件。关闭时先排空投递，再关闭原生会话和发布产物。仍与最后投递一致的脚本原件由远端保存负责，其他产物沿原合并流程处理。[S3 投递](../server/app/services/viz_scene_relay.py) · [会话生命周期](../server/app/services/viz_sessions.py) · [暂存合并](../server/app/services/staging_sync.py)

## 实际验收

- 文件系统。真实 Edge 和原生 VTK 验证文件写入、无效 UTF-8 文件上传、后续命名空间恢复、235 毫秒内两次真实原子编辑、同名更新、截图、语法和 `ViewSize` 失败、浏览器接回及已核验的内核停止。外部账号写入和开启渲染均返回 404。[实际回执](../../tmp/viz-scene-watch-d09-1yqhz9ly/scene-watcher-result.json) · [验收入口](../server/viz_scene_watcher_acceptance.py)
- 认证 S3 旧行为。真实保存的远端 SHA-256 已更新，4.3 秒后原会话及暂存区仍停在旧版本。保留实际旧行为、关闭回执和临时服务的退出记录。[旧行为回执](../../tmp/viz-scene-s3-d09-7ck5rhpe/scene-s3-result.json)
- 认证 S3 修复后。实测对象存储已有新脚本、SQLite 事务尚未提交时，原渲染会话保持当前对象。正常提交后，同一个 Edge/VTK 会话执行新版本。随后完整拒绝 `ViewSize`，下一次合法保存仍沿用原命名空间。关闭后从实际 S3 读回最新脚本、1200×916 原生 PNG 和原执行日志。临时文件已清理，外部账号的改写与会话查询均为 404。渲染器、API 和隔离存储均已核验退出。本次模型调用为零。[浏览器回执](../../tmp/viz-scene-s3-d09-n9dy69bl/scene-s3-browser.json) · [完整回执](../../tmp/viz-scene-s3-d09-n9dy69bl/scene-s3-result.json) · [验收入口](../server/viz_scene_s3_browser_acceptance.py)
