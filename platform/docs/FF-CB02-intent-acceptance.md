# FF-CB02 工程意图与求解验收

在算例的 caseDict 块级编辑器中，为目标输出指定后处理指标名和单位，再用具名验收标准引用目标。上下限分别可省略，至少填写一项，边界值视为符合。指标名使用后处理结果中的完整名称，包含结果所对应的时刻。匹配要求单位一致，量纲为一的指标可显式填写 `1`。

```text
intent
{
    targetOutputs
    {
        meanTemperature { metric "mean.T@1"; unit K; }
    }
    acceptanceCriteria
    {
        temperatureRange { target meanTemperature; min 349; max 351; }
    }
    openQuestions
    {
        wallCondition { text "确认壁面条件"; status open; }
    }
    assumptions
    {
        material { text "均匀材料"; status proposed; }
    }
    decisionLog
    {
        reference { text "使用解析参考解"; status accepted; }
    }
}
```

正式组稿将每个目标关联到已封存的实际指标，并显示验收上下限、测量值、单位和判定。两种组稿输出的 `GET /api/v1/cases/{id}/report/compose-result` 在 `evidence.intent_acceptance` 记录机器可读的判定、指标文件路径、SHA-256、来源作业和修订。检查来源算例、作业、工程输入指纹、报告就绪标记与产物清单后才进行数值比较。找不到目标或指标、指标重名、单位不一致、非法数字和无效区间均显示为待核验，并记录原因。旧列表和自由文本字段继续显示在报告里。数值判定沿用原报告中的后处理证据门禁，实验验证仍由原验证章节的独立证据支撑。

算例面板的「工程意图修订」使用现有 `GET /api/v1/cases/{id}/provenance` 分页显示具名问题、假设和决策的最新状态，以及每次修订的新增、更新和移除。结果与同页的修订事件 ID 关联，完整原文可从该事件的 `intent` 字段回读。状态由编辑者填写，面板按字典已提交值显示。修订链完整性校验失败时，界面显示读取错误，停止显示推导的状态变化。受邀请的只读成员可查询，修改继续受原权限及修订检查约束。

## 实际验收

- 修复前的 SQLite 文件系统算例运行 80 单元 OpenFOAM 热传导，VTK 产出约 350 K。两个正式组稿入口展示原配置和实际指标，结果缺少单项判定。[基线](../../tmp/live-cli-startup-_hzq8m6a/intent-metrics-baseline.json)
- Windows 文件系统配 SQLite、认证 SeaweedFS 配 PostgreSQL 均重新运行 80 单元 OpenFOAM 热传导和 VTK 提取，核对实际结果、两种报告及相同的指标摘要。区间 349–351 K 符合，只设上限 340 K 超出范围。错误单位、无指标、未知目标、反向阈值及 `nan` 返回待核验。实际 API 验证状态变化、分页、删除、过期修订拒绝、保留旧报告、只读授权及撤销后的拒绝。[SQLite](../../tmp/live-cli-startup-moh4slkm/intent-metrics-result.json) · [PostgreSQL 与 S3](../../tmp/live-cli-startup-2hokezcn/intent-metrics-result.json)
- 真实 Edge 在 1500 × 950 和 1040 × 780 下验证原列表编辑回读、键盘展开修订、分页、写入后刷新及切换算例。实际生成的报告以 Markdown 预览核对温度、范围与判定。展示层以约等号标记四舍五入的读数，判定使用原始数值。首次报告截图中验收表七列过于挤压，最终改为四列。[工作台回执](../../tmp/case-intent-browser-w4levz1w/intent-roundtrip-result.json) · [窄界面](../../tmp/case-intent-browser-w4levz1w/intent-lifecycle-narrow.png) · [实际报告预览](../../tmp/live-cli-startup-moh4slkm/intent-metric-report.png)
- SQLite 和 PostgreSQL 的最终两轮各调用两次真实 DeepSeek `deepseek-flash` 读回验收证据，均为 HTTP 200，模拟调用 0 次。首次 PostgreSQL 夹具沿用了 SQLite 现场查询，已改用该次验收的临时 PostgreSQL 数据库。[SQLite 模型回执](../../tmp/live-cli-startup-moh4slkm/live-result.json) · [PostgreSQL 模型回执](../../tmp/live-cli-startup-2hokezcn/live-result.json) · [首轮夹具失败](../../tmp/live-cli-startup-n474n_20/live-result.json)

## 复跑

```powershell
$python = "platform/server/.venv/Scripts/python.exe"
& $python -B -X utf8 platform/server/live_llm_case_intent_metrics_acceptance.py --source-profile-id 26
& $python -B -X utf8 platform/server/live_llm_case_intent_metrics_acceptance.py --source-profile-id 26 --postgres --s3
& $python -B -X utf8 platform/server/intent_roundtrip_browser_acceptance.py --dist tmp/intent-metrics-web-20261121 --browser-python C:/ProgramData/anaconda3/python.exe --channel msedge --lifecycle
& $python -B -X utf8 platform/server/live_llm_case_intent_metrics_acceptance.py --source-profile-id 26 --intent-browser-dist tmp/intent-metrics-web-20261121
```

回退可撤回报告生成器和修订面板代码。已经发布的报告仍按其不可变版本读回。此变更沿用现有数据结构和修订链，无数据库迁移。
