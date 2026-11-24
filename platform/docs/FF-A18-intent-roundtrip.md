# FF-A18 intent 字段、修订回读与报告

2026-11-19 在真实 Edge、独立 SQLite 和文件存储通过。`targetOutputs`、`acceptanceCriteria`、`assumptions`、`limitations`、`openQuestions`、`decisionLog` 经实际表单连续保存并刷新后保持原值。覆盖空列表、单项和多项列表、带括号的正文、中文和老算例常见的字典或纯文本字段。两次保存分别形成独立修订，旧内容可通过 provenance 分页回读，哈希与各次字典内容一致。[验收回执](../../tmp/case-intent-browser-3c5vyhx3/intent-roundtrip-result.json) · [浏览器截图](../../tmp/case-intent-browser-3c5vyhx3/intent-editor.png)

修复前，基础列表的单项和空值会读成带括号的字符串，多项列表会把外部括号带入字段值。再次应用块表单还会累积这些括号。现在识别这些 intent 字段的 OpenFOAM 列表边界，provenance 保存相同的语义值，包括作为条目正文输入的括号。普通整文写入中非法的 intent 块返回 HTTP 422，原字典、SQL 镜像和历史保持一致。独立账号的读取历史及修改字段均返回 HTTP 404。[旧版反例](../../tmp/case-intent-before-5sk_j376/intent-before.json) · [解析实现](../server/app/services/casedict.py) · [修订实现](../server/app/services/provenance.py)

报告组稿通过两次实际 `report:compose` 作业验证。主算例的正式 Markdown 草稿包含全部六个字段，旧算例的 `primaryAssumptions` 和 `knownLimitations` 也进入相应章节。两次作业均核验进程停止，组稿保留算例修订历史。本轮验收覆盖字段编辑、校验、历史和草稿，目标与实际工程指标的自动关联及生命周期追踪由 FF-CB02 继续验收。模型调用为 0，模拟调用为 0。[主算例草稿](../../tmp/case-intent-browser-3c5vyhx3/intent-generated-report.md) · [兼容草稿](../../tmp/case-intent-browser-3c5vyhx3/intent-generated-legacy-report.md) · [组稿实现](../server/app/services/report_builder.py)

FF-A24 的列表保真改动合入后，使用同一真实入口再次通过两次表单保存、现有和旧字段组稿、读回历史及退出核验。[当前源码复验](../../tmp/case-intent-browser-qvavo35m/intent-roundtrip-result.json)

2026-11-21 开始核对 FF-CB02。通过公开 API 保存具名输出与 349–351 K 验收区间，随后运行 80 单元 OpenFOAM 算例、VTK 提取和两条正式报告组稿入口。报告载有区间描述和约 350 K 的实际结果，公开结果缺少该验收条目的判定。原始六字段往返及修订保真已有上述证据，FF-CB02 继续实现指标判定与具名条目的生命周期。真实 DeepSeek 读回 2 次 HTTP 200，模拟 0 次。[复现回执](../../tmp/live-cli-startup-_hzq8m6a/intent-metrics-baseline.json) · [实际工程流程](../../tmp/live-cli-startup-_hzq8m6a/local-post-report.json) · [模型回读](../../tmp/live-cli-startup-_hzq8m6a/live-result.json)

在项目根目录复跑，复用当天的前端构建目录和安装了 Playwright 的本机 Python。

```powershell
& 'platform/server/.venv/Scripts/python.exe' -B -X utf8 `
  'platform/server/intent_roundtrip_browser_acceptance.py' `
  --dist 'tmp/codex-wsl-profile-20261119/web-dist' `
  --browser-python 'C:/ProgramData/anaconda3/python.exe' --channel msedge
```
