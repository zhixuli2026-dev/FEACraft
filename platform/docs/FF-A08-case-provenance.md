# FF-A08 caseDict 修订、决策与错误记录验收

2026-11-19 使用真实 Edge 和独立 SQLite／文件存储通过。浏览器保存 `intent.decisionLog`、`provenance.assumptions` 和 `provenance.decisions`。每次保存后，通过正式 provenance API 核对新记录的修订号、字典摘要和字段内容。前序事件及摘要能够通过分页读回。[通过回执](../../tmp/case-provenance-browser-b3rcxyia/case-provenance-result.json) · [浏览器回执](../../tmp/case-provenance-browser-b3rcxyia/provenance-browser.json)

实际表单完成开始处理、提出修复、标记修复、验证结清、刷新回读和重开。缺少修复依据时返回 HTTP 422，原修订和哈希链保持一致。结清时 blocking 为 0，重开后恢复为 1。扩展通过正式 API 注册和删除，各新增一条来源为 `extensions` 的修订。独立组织访问修订链或尝试修改错误和扩展都收到 HTTP 404。[浏览器截图](../../tmp/case-provenance-browser-b3rcxyia/provenance-browser-verified.png) · [通过回执](../../tmp/case-provenance-browser-b3rcxyia/case-provenance-result.json)

修复前，去掉首条、末条或整条修订链，或更改实际字典字节后，读取仍可能返回 `chain_valid: true`。损坏 JSONL 会导致 HTTP 500。现在读取检查首条和前序连接、各条记录的摘要，以及末条记录与已提交修订和真实字典摘要的一致性。损坏记录通过 `issues` 返回原因和行号，其他可读记录仍可分页。实际注入七种文件故障后均标记无效，恢复原字节后重新通过。历史中存在非对象记录时，保存返回 422 并保留原文件和 SQL 版本。[修复前实际反例](../../tmp/provenance-integrity-before-r9caj_ip/provenance-integrity-before.json) · [检查实现](../server/app/services/provenance.py)

实际算例复制保留原记录并追加新记录，导入带历史的 StandardCase ZIP 按本地修订 1 开始记录，工作区备份恢复保留完整旧链。三条路径均通过末条修订与字典摘要核对。新验收覆盖 Windows/Edge 与 SQLite/文件存储。产物自动关联继续由 FF-CB14 跟踪，扩展完整 schema 见 FF-CB16。本轮模型调用为 0，模拟调用为 0。[实际复制、导入与恢复](../../tmp/case-provenance-browser-b3rcxyia/case-provenance-result.json)

在项目根目录复跑，复用当天的前端构建目录和已安装 Playwright 的本机 Python。

```powershell
& 'platform/server/.venv/Scripts/python.exe' -B -X utf8 `
  'platform/server/case_provenance_browser_acceptance.py' `
  --dist 'tmp/codex-wsl-profile-20261119/web-dist' `
  --browser-python 'C:/ProgramData/anaconda3/python.exe' --channel msedge
```
