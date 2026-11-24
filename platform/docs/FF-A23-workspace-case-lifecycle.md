# FF-A23 工作区与算例管理

2026-11-19。原要求覆盖工作区和算例重命名、复制、算例跨工作区移动，以及文件系统回收站的删除与恢复。新增验收使用生产前端、实际 Edge 和隔离 SQLite/文件系统 API。永久清除、S3 回收站和多副本回收站沿用原审计单列的范围。[原审计](feature-gap-audit.md) · [回收站协调与恢复证据](case-write-coordination.md)

- 工作区重命名原先重新选择当前工作区，导致活动算例与打开的编辑器被清空。现在根据 PATCH 回执更新工作区名称，保留原选择和草稿。Edge 实测 Monaco 草稿完整保留，服务器文件在主动保存前维持原内容。算例重命名保留原 ID 和文件入口。[项目面板](../web/src/panels/TreePanel.tsx) · [浏览器回执](../../tmp/a23-project-admin-uza63xbq/workspace-lifecycle-browser.json)
- 移动按目标工作区 ID 提交。选择列表提供其他自有工作区，重名时显示编号。实测两个同名自有工作区与一个同名共享工作区，取消丢弃未保存草稿时没有发出移动请求。目标有同名算例时，真实 API 返回 409，原算例、文件和表单目标均保留。解除冲突后同一表单重试成功并打开目标工作区中的原算例。只读共享账户的相关操作在界面禁用，API 拒绝越权移动及删除、恢复、改名。[移动冲突界面](../../tmp/a23-project-admin-uza63xbq/move-conflict.png) · [实际 API 与 Edge 回执](../../tmp/a23-project-admin-uza63xbq/workspace-lifecycle-result.json)
- 验收先运行一个真实 Windows `convert`，生成已提交的原生作业发布。算例复制和整区复制分别生成新 ID 和独立存储，携带普通文件、caseDict 和已发布的实时结果文件。修改副本时原件保留，已封存的发布历史留在原算例。分别将原算例与其工作区移入回收站后，普通读取返回 404。恢复后原算例 ID、路径、caseDict SHA-256、作业尝试身份、发布记录及不可变结果字节全部核验相同。API 已退出，模型调用为 0。[回收站界面](../../tmp/a23-project-admin-uza63xbq/workspace-recycle-bin.png) · [验收回执](../../tmp/a23-project-admin-uza63xbq/workspace-lifecycle-result.json)

复验使用服务端虚拟环境、生产前端构建和装有 Playwright 的 Python。入口通过 `no_llm` 审查后启动隔离 API。[复验脚本](../server/workspace_lifecycle_browser_acceptance.py) · [前端构建](../../tmp/a23-admin-build-20261119.log) · [界面检测](../../tmp/a23-admin-impeccable-20261119.json)

```powershell
& 'platform/server/.venv/Scripts/python.exe' -B -X utf8 `
  'platform/server/workspace_lifecycle_browser_acceptance.py' `
  --dist 'tmp/a23-admin-dist-20261119' `
  --browser-python 'C:/ProgramData/anaconda3/python.exe' --channel msedge
```
