# FF-CB15 算例错误编辑、证据与溯源

算例面板支持新增错误并编辑标题、阶段、分类、严重程度、阻断标记及详情。处理动作覆盖开始处理、提出修复、标记修复、验证结清、重开和不予修复。修复方案、处理结论及验证说明由现有 schema 按目标状态检查。相关证据使用算例相对路径，保存新引用前核对对应算例文件，点击引用通过现有算例阅读器打开。可引用同一算例的真实作业日志。

新增 `POST /cases/{id}/caseDict/errors`，使用 `id`、`title`、`stage`、`category`、`severity` 创建条目。现有 `PATCH /cases/{id}/caseDict/errors/{error_id}` 支持部分更新，状态、修复方案和证据字段沿用原契约。两个入口都核对版本条件，重算 `total/open/blocking/resolved/lastErrorId`，并通过原 caseDict 文件事务同时提交修订与溯源。旧算例只有汇总时，新增条目若会降低已有未结或阻断计数，返回 409 并提示先核对旧错误块。通过原字典接口明确核对后，可新增和处理详细条目。

错误变化随修订进入 `provenance.jsonl` 的哈希链。变化的修订记录完整错误块和 `errors_sha256`，其它修订保留该摘要。现有旧记录继续按其原始字段验证。浏览器实测也发现单条文件引用会把 OpenFOAM 列表的括号读成路径内容，随后增加第二条引用导致原日志打不开。现仅在 `errors.entries.<id>.relatedPaths` 路径用已有的列表解析，保留旧标量引用；其余同名字段和原始注释处理保持各自原有规则。[原证据链接失败回执](../../tmp/case-errors-browser-ckc_z587/case-errors-result.json)

## 2026-11-19 实际验收

- 真实 CadQuery 预览生成体积为 24 的实体。在真实 Edge 新增条目后编辑全部基础字段，关联并打开实际作业日志，再加入路径含中文和空格的核对记录。实际操作经过处理中、提出修复、已修复、已验证、重开和不予修复。重载后保留证据引用和验证说明。缺修复方案、缺验证说明各返回 422；并发修改返回 409，表单保留草稿并明确核对新版本后提交。[最终验收回执](../../tmp/case-errors-browser-8pep3s_q/case-errors-result.json)
- 校验 HTTP 溯源返回与磁盘 JSONL 末条一致，修订链接和 caseDict 摘要有效，重复写相同错误快照未增加错误内容副本。不存在的文件、越界路径和无效枚举均 422，重复编号 409，其它账号的新建和编辑请求均 404。以上拒绝前后 caseDict 和修订历史一致。[API 与溯源回执](../../tmp/case-errors-browser-8pep3s_q/case-errors-result.json)
- 旧汇总保留 2 个未结和 1 个阻断，新增详细条目和尝试阶段推进都收到 409。通过正式 caseDict 保存核对旧汇总后，新增记录和标记修复均成功。FF-A20 的实际 Edge 阶段门禁复跑通过。[旧算例回执](../../tmp/cb15-legacy-summary-cd249btk/legacy-errors.json) · [阶段门禁回归](../../tmp/case-error-gate-22_omi0a/case-error-gate-result.json)

本轮使用 D 盘隔离文件存储和 SQLite，模型调用为 0。TypeScript、Vite 构建与 Impeccable 检查通过。可用 [case_errors_browser_acceptance.py](../server/case_errors_browser_acceptance.py) 指定独立的 Vite 构建目录与本机 Edge 驱动复跑。
