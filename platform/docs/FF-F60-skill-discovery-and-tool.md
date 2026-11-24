# FF-F60 技能发现、补全和正文调用

2026-11-21，在真实 Edge 中用 `$` 搜索并选择全部 41 个内置技能和一个私有技能，核对名称、简介、写入草稿的规范名称或默认提示，以及未知名称提示。实际 API 验证未登录拒绝、个人定义损坏或删除时回退。浏览器测试只使用草稿，模型调用为零。[Edge 结果](../../tmp/skill-metadata-v86exerj/browser-result.json) · [目录与权限](../../tmp/skill-metadata-v86exerj/skill-metadata-result.json)

正式 Agent API 用真实 DeepSeek Flash 调用一次 `get_skill`，从内置 `vibeflow-openfoam` 读出《OpenFOAM 求解契约》和 `case-Re1000`。第一次 HTTP 请求无工具正文，第二次携带一次工具结果并包含两条验证内容。两个上游响应均为 HTTP 200，模型原样返回目录名。只记录匹配结果和请求状态，密钥与请求正文均留在隔离运行中。模拟调用为零。[模型结果](../../tmp/live-builtin-skill-k6ktfcv1/live-result.json) · [实际转发凭据](../../tmp/live-builtin-skill-k6ktfcv1/skill-forwarding.jsonl)

私有技能也通过真实模型调用。发布前 API 退出后，Agent 重新发现该技能，`get_skill` 读取恢复正文并返回唯一验证串。另有两次真实 HTTP 200。[私有技能冷恢复](../../tmp/live-skill-install-recovery-glonigi1/live-result.json)

复跑入口 [浏览器与目录验收](../server/skills_metadata_smoke.py) · [真实技能模型验收](../server/live_llm_skill_recovery_acceptance.py)
