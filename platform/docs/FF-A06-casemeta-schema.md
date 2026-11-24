# FF-A06 caseMeta 字段校验与单位换算验收

2026-11-19 在真实 Edge 和独立 SQLite／文件存储通过。浏览器通过既有 caseDict 块表单修改学科、分析类型、维度、执行方式、精度层级、显示名称、标签与单位。结构化分析类型、modal 与 periodic 组合都显示字段错误并保留编辑草稿。五次合法保存生成连续修订，刷新后仍显示最终值，caseMeta 与 SQL 镜像一致，未编辑的 intent 块字节一致。[综合回执](../../tmp/case-meta-browser-ss3o5ml2/case-meta-result.json) · [浏览器回执](../../tmp/case-meta-browser-ss3o5ml2/case-meta-browser.json) · [截图](../../tmp/case-meta-browser-ss3o5ml2/case-meta-browser.png)

旧版写入带字典结构的 workflowState、analysisType 或 timeBehavior 会触发 HTTP 500。交叉校验现在只对类型正确的字段执行，错误通过原写入接口返回 HTTP 422 并指出字段。本轮实际请求核对十种无效输入以及跨账户写入和换算的 HTTP 404，拒绝的写入和单位换算均保留原修订与摘要。[旧版 HTTP 500](../../tmp/case-meta-before-kkrursct/case-meta-before.json) · [校验规则](../server/app/services/casedict_schema.py)

旧版单标签保存失败，多标签读取会把字典语法的括号作为标签内容，再次保存时继续累积。解析现在把 `caseMeta.tags` 的一层外部括号作为列表分隔符，标签文字中的括号仍按原文保存。浏览器验证空标签列表、单个中文标签、多标签及修改名称后的再次读取。[旧版标签反例](../../tmp/case-meta-tags-before-_k7oh4hy/case-meta-tags-before.json) · [字典解析](../server/app/services/casedict.py)

单位换算实测 length、mass、time、temperature、angle、force、pressure、energy 八种量纲，合计 13 个正常算例及 6 个非法请求。长度支持 m/mm/inch，温度支持 K/degC，角度支持 rad/deg，质量、时间、力、压力、能量分别使用 kg、s、N、Pa、J。现在相同单位保留浮点值，包括极小值。结果溢出、非有限输入和不支持的单位返回 HTTP 422。[换算实现](../server/app/services/units.py) · [实际数值](../../tmp/case-meta-browser-ss3o5ml2/case-meta-result.json)

本轮验证元数据的保存、拒绝与回读。`executionProfile` 的枚举验收使用配置值，实际远程求解有独立交付项。单位换算通过现有授权 API 实测，完整 schema 通过整文写入与块写入触发。模型调用为 0，模拟调用为 0。[现有近端校验](../server/casedict_schema_unit_smoke.py)

在项目根目录复跑，复用当天的前端构建目录和已安装 Playwright 的本机 Python。

```powershell
& 'platform/server/.venv/Scripts/python.exe' -B -X utf8 `
  'platform/server/case_meta_browser_acceptance.py' `
  --dist 'tmp/codex-wsl-profile-20261119/web-dist' `
  --browser-python 'C:/ProgramData/anaconda3/python.exe' --channel msedge
```
