# FF-CB14 算例修订与产物追溯

新建 StandardCase 在原创建事务内写入初始来源、创建账户、时间和 `artifactTrace.registry casePublications`，以实际文件摘要建立修订 0 的哈希链记录。具名来源、假设、决策和手填的产物说明随每次 caseDict 修订共同记录。由用户触发的保存、Agent 已审批保存、阶段确认、回退、复制和导入记录实际操作者。受管理的 `provenance` 块级保存保留原创建信息并更新时间。整文导入和复制保留原字典字节及已有来源，新操作另写修订事件。

产物发布事务把封存工作区摘要、读取的 caseDict 修订及摘要、上游发布版本和结果清单摘要一并写入发布回执。原始 caseDict 以其已保存字节为准，产物的新增、换版和历史回读由发布表与回执记录。这样，新产物生成时无需更新工程输入或增加字典修订。可从发布回执和 SQL 封存记录重建追溯关系，已有旧回执按其保存内容读回，缺少封存字典摘要时显示已有修订信息。 [发布事务](../server/app/services/artifact_publication.py) · [追溯服务](../server/app/services/case_artifact_trace.py) · [修订哈希链](../server/app/services/provenance.py)

## 操作与接口

在算例面板展开「产物追溯」，选择发布版本。可查看发布摘要、封存 caseDict、回执、登记文件和上游发布。点击「核对算例修订」后按修订号和摘要核对整条字典哈希链，读回实际来源、假设、决策和操作者。

- `GET /api/v1/cases/{id}/provenance/artifacts` 支持 `limit` 与 `before_id` 分页。列表仅显示成功作业的已核验发布与保留版本，`active` 对应当前发布头。
- `GET /api/v1/cases/{id}/provenance/artifacts/{publication_id}` 返回输出清单和发布来源。清单身份及摘要不一致返回 409。实际读取产物正文时沿用原文件读取与摘要核验。
- `GET /api/v1/cases/{id}/provenance?revision=…&sha256=…` 按两项共同匹配已验证修订。哈希链损坏或查不到对应历史时，`matched_revision` 为空。可继续使用原分页字段读取修订。

读权限沿用同算例访问控制，支持授权只读成员，拒绝外部账户及其他算例的发布 ID 或游标。复制和工作区备份沿用现行归档范围。复制或恢复可读原字典修订，原算例发布仍由各自原存储范围和 SQL 发布记录证明。

## 证据

- 修复前已实际执行 80 单元 OpenFOAM、VTK 后处理和两路组稿。报告已有独立发布和封存摘要，修订记录缺少完整的来源块，发布回执缺少工作区关联，追溯路由返回 404。 [旧码基线](../../tmp/live-cli-startup-xr0ogk07/case-artifact-trace.json)
- 文件系统配 SQLite、认证 SeaweedFS 配 PostgreSQL 均执行注册的 CadQuery 几何导出、blockMesh/checkMesh、laplacianFoam、VTK 后处理和正式报告组稿。四次阶段确认完成，五类产物均能按摘要查回各自 caseDict 修订，四组连接指向原上游发布摘要。 [SQLite 注册工程链](../../tmp/live-cli-startup-o767be42/registered-artifact-trace.json) · [PostgreSQL/S3 注册工程链](../../tmp/live-cli-startup-zaj164d2/registered-artifact-trace.json)
- 原 caseDict 修订链已通过真实 Edge 兼容复验。七种物理文件故障保留诊断，原字节恢复后正常。复制、StandardCase 导入和工作区备份恢复保留历史。 [旧入口兼容](../../tmp/case-provenance-browser-d993diiv/case-provenance-result.json)
- Edge 使用鼠标和键盘打开历史报告，查看原决策和上游后处理版本。重新组稿后保留原发布，重启隔离 API 后仍能从同一数据库及文件存储查回原版本和摘要。另一轮在 PostgreSQL 与认证 SeaweedFS 上检查相同的发布和权限行为。两种部署均验证清单损坏时拒绝读取，恢复原字节后可再次核对。 [Edge 与 API 重启](../../tmp/live-cli-startup-lqnygzof/case-artifact-trace.json) · [工作台截图](../../tmp/live-cli-startup-lqnygzof/case-artifact-trace-report.png) · [认证 S3 复验](../../tmp/live-cli-startup-rm9fp9d5/case-artifact-trace.json)
- 将修复前数据库和实际归档复制到隔离环境，查询旧报告时从已封存记录读回精确修订，回执明确显示该旧事件未保存结构化来源字段。 [旧版发布回读](../../tmp/case-artifact-legacy-esg5vatu/legacy-artifact-trace.json)
- 九轮实际 Agent API 文件回读合计 18 次 `deepseek-flash` HTTP 200，模拟 0 次。四套临时 SeaweedFS 和 PostgreSQL 均完成清理。前端构建和追溯组件 lint 通过。Windows C 盘在阶段前后分别可用 23,651,569,664 和 23,640,662,016 字节，Docker 本轮缓存回收为 0。 [联调与清理回执](../../tmp/case-artifact-trace-validation-20261121.json)

## 复验路径

```powershell
$python = "platform/server/.venv/Scripts/python.exe"
& $python -B -X utf8 platform/server/live_llm_case_artifact_trace_acceptance.py --source-profile-id 26 --trace-browser-dist tmp/case-artifact-trace-web-final-20261121
& $python -B -X utf8 platform/server/live_llm_case_artifact_trace_acceptance.py --source-profile-id 26 --postgres --s3
& $python -B -X utf8 platform/server/live_llm_case_artifact_trace_acceptance.py --source-profile-id 26 --trace-registered --postgres --s3
& $python -B -X utf8 platform/server/live_llm_case_artifact_trace_acceptance.py --trace-legacy tmp/live-cli-startup-xr0ogk07
```

如果回退本轮代码，原有发布路由、原字典及既有哈希链仍保存在原位置。旧读写回路可继续读取新增可选字段，同一数据库无需迁移。
