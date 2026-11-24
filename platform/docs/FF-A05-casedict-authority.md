# FF-A05 caseDict 文件、数据库镜像与阶段确认

2026-11-19 在真实 Edge 和独立 SQLite／文件存储通过。算例所有者明确确认 A→V，实际 API 重启后仍显示 V，再明确确认 V→A。三个观测点的阶段与修订分别为 A/1、V/2、A/3。V 保留 `caseMeta.status=draft`，返回 A 后更新为 `readyForGeometry`。字典原件、读取接口、SQL 镜像、事件时间线与修订哈希链相互吻合。API 重启前后的 V 文件和两份历史 SHA-256 相同。[验收回执](../../tmp/casedict-authority-browser-_cdhrlz9/casedict-authority-result.json) · [API 两次启动](../../tmp/casedict-authority-browser-_cdhrlz9/api.log) · [V 页面](../../tmp/casedict-authority-browser-_cdhrlz9/case-dict-stage-V.png) · [A 页面](../../tmp/casedict-authority-browser-_cdhrlz9/case-dict-stage-A.png)

修复前，隔离文件含 V、SQL 镜像为 A 时，重复确认 A 返回 HTTP 200。阶段入口现在先在算例协调锁下读取规范字典文件。文件缺失或读取版本冲突产生 HTTP 409 `file_conflict`。读取到的文件阶段与已提交镜像不一致时返回 HTTP 409 `case_dict_state_conflict`，字典、镜像和两份历史均保留。如果文件和镜像阶段相同，重复确认该阶段仍成功且不增加修订。有效的新阶段由现有事务写入器提交，提交前把当前文件摘要与已读取版本比较，并在同一写入流程更新字典、镜像、事件与修订链。故障场景在读取后实际追加了一行内容，提交返回 HTTP 409 并保留该行。既有字典语法和业务阶段校验继续由原入口执行。[修复前反例](../../tmp/casedict-authority-before-yu183jwd/casedict-drift-before.json) · [文件缺失与恢复实测](../../tmp/casedict-missing-file-gyrgcq9o/casedict-missing-file-result.json) · [阶段入口](../server/app/services/engineering_stage.py) · [提交与恢复](casedict-transactions.md)

未经确认、旧修订及从 V 直接跳到 B/D/E 均被拒绝。普通文件的写入、删除和上传都拒绝规范路径或等价路径。专用整文保存拒绝直接改变阶段。未共享的账号收到 HTTP 404，已共享的查看者能读取 V，界面禁用确认，直接 API 写入也收到 HTTP 404。[验收回执](../../tmp/casedict-authority-browser-_cdhrlz9/casedict-authority-result.json) · [整文编辑与冲突复跑](FF-A11-editor-conflict-acceptance.md)

本轮新增回执覆盖 Windows/Edge 与 SQLite/文件存储，实际模型调用和模拟调用均为 0。已有真实导热算例覆盖 B/C/D/E 浏览器确认，以及重算与原件保留。Agent 越权阶段提案继续引用真实 DeepSeek 的工程恢复验收，S3 事务恢复引用已有专项。这次复跑使用独立 V/A 流程。[B/C/D/E 实测](../../tmp/case-workflow-chain-jg4a48bl/chain-result.json) · [真实 Agent 回执与阶段拒绝](engineering-recovery-acceptance.md) · [S3 文件事务验收](s3-file-transactions.md)

在项目根目录复跑，复用既有前端构建目录和已安装 Playwright 的本机 Python。

```powershell
& 'platform/server/.venv/Scripts/python.exe' -B -X utf8 `
  'platform/server/casedict_authority_browser_acceptance.py' `
  --dist 'tmp/codex-wsl-profile-20261119/web-dist' `
  --browser-python 'C:/ProgramData/anaconda3/python.exe' --channel msedge
```
