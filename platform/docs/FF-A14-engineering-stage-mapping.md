# FF-A14 阶段映射与工程证据门禁

2026-11-19 用独立 SQLite／文件存储运行真实导热算例，依次通过 CadQuery、`blockMesh`／`checkMesh`、`laplacianFoam`、Python／VTK 后处理和 Pandoc 报告。五项正式作业均核验退出并发布。每次阶段确认后，规范 caseDict、API、SQL 镜像和修订摘要一致。实际 Edge 显示 E 报告阶段及 `readyForReport`。[全程回执](../../tmp/stage-mapping-pnukmam5/stage-mapping-result.json) · [E 阶段截图](../../tmp/stage-mapping-pnukmam5/stage-mapping-E.png)

| 阶段 | 实测 `caseMeta.status` | 当前工程前置 |
| --- | --- | --- |
| A | `draft`、显式回到 A 后为 `readyForGeometry` | 重新检查几何输入 |
| B | `readyForMesh` | 当前有效的几何验证 |
| C | `readyForRun` | 当前网格与 `checkMesh` 质量证据 |
| D | `readyForPost` | 与当前输入和求解场一致的成功求解 |
| E | `readyForReport`、同阶段允许 `completed` | 本算例成功后处理与 `report_ready` |
| V | 保留此前状态字段 | 显式切换开发阶段 |

在生成每一项工程前置前，独立阶段请求 B/C/D/E 均收到 HTTP 409。真实作业成功后，绑定工作流步骤的未确认请求仍被拒绝，携带当前修订及步骤版本的明确确认才推进。过期修订、跳级和普通整文保存中不一致的 `caseMeta.status` 均被拒绝。V 的状态字段保留和阶段权限引用已有浏览器验收，错误计数门禁引用 FF-A20。[A/V 状态与权限](FF-A05-casedict-authority.md) · [错误计数门禁](FF-A20-error-stage-gate.md)

旧算例的后处理指针为数组时，原确认 E 返回 HTTP 500。旧指针现用现有版本校验读取器读取，格式损坏、内容缺失、无效标识或指向其它类型作业时返回常规证据不足 HTTP 409。正常的整数标识及旧整数文本沿用既有成功后处理与报告就绪检查。实际导入 D 算例前后对照验证了修复，主链也验证不可用指针后可继续正常后处理和报告。[修复前回执](../../tmp/stage-map-legacy-before-woi0h82w/stage-map-legacy-before.json) · [实际导入复验](../../tmp/stage-map-legacy-after-astzt7qp/stage-map-legacy-after.json) · [阶段入口](../server/app/services/engineering_stage.py)

本轮新增证据覆盖 Windows/WSL、Edge、SQLite 和文件存储。PostgreSQL／SeaweedFS 的 C 阶段事务、兼容文件缺失和冷 API 重放引用当前联合验收。模型调用为 0，模拟调用为 0。[当前 PostgreSQL／SeaweedFS C 阶段](N7-01-recompute-postgres-s3.md) · [既有 B/C/D/E 浏览器确认](N6-05-original-closure-audit.md)

在项目根目录复跑，复用当天的前端构建目录及已安装 Playwright 的本机 Python。

```powershell
& 'platform/server/.venv/Scripts/python.exe' -B -X utf8 `
  'platform/server/engineering_stage_mapping_acceptance.py' `
  --dist 'tmp/codex-wsl-profile-20261119/web-dist' `
  --browser-python 'C:/ProgramData/anaconda3/python.exe' --channel msedge
```
