# FF-F63 原版技能目录

2026-11-21，从原安装包的桌面端和 Agent 目录分别盘点 `SKILL.md`。两个目录的 37 个技能名完全一致。隔离 API 返回了全部 37 项以及 HyperMesh、Jobs、OpenVSP 和 Viz 四项扩展。逐项读取 41 份详情，核对名称、简介与非空正文，并保存正文摘要。匿名列表和详情均返回 401，未登记的名称返回 404。[API 和正文摘要](../../tmp/skill-metadata-n7zcaeyr/skill-metadata-result.json)

Agent 草稿的联想按大小写无关的方式搜索。原版名称 `vibeflow-PPT` 可用大写和小写搜索，选择后仍写入原版名称。真实 Edge 逐项选择全部 41 个内置技能和一个私有元数据用例，核对草稿与无结果提示。用户覆盖的隔离与内置回退同时通过。真实和模拟模型请求均为 0，浏览器只操作草稿。[Edge 结果](../../tmp/skill-metadata-n7zcaeyr/browser-result.json) · [PPT 选择截图](../../tmp/skill-metadata-n7zcaeyr/skill-picker-draft.png)

使用 [技能验收入口](../server/skills_metadata_smoke.py) 复跑。实际求解工具链、PPT 高阶能力和各项支持声明分别在 FF-F64、FF-F65 和 FF-F66 验收。
