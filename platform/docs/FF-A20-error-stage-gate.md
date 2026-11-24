# FF-A20 未结与阻断错误的阶段门禁

2026-11-19 在真实 Edge 和独立 SQLite／文件存储验证。在算例面板核对 V 阶段，再点击明确确认。`(open, blocking)` 为 `(1,1)`、`(1,0)`、`(0,1)` 时，服务端各返回 HTTP 409 并指出计数，界面留在 A，caseDict、修订号与 provenance 保持一致。通过错误表单处理与重开记录，计数为 `(0,0)` 后，同一目标的明确确认返回 HTTP 200，阶段更新为 V，修订增加一次。[当前源码回执](../../tmp/case-error-gate-kyyeowoe/case-error-gate-result.json) · [仅阻断错误的页面](../../tmp/case-error-gate-kyyeowoe/error-gate-blocking-only.png)

此前类型错误的 `errors.summary.open`、`errors.summary.blocking` 或非字典条目会让汇总复核返回 HTTP 500。现在汇总比较复用已通过类型校验的整数，并在条目都为字典时重算汇总。非法结构和与有效条目冲突的手工零值通过原写入接口返回 HTTP 422，当前字典和修订历史保持一致。[修复前反例](../../tmp/error-gate-before-unwpnyf0/error-gate-before.json) · [校验实现](../server/app/services/casedict_schema.py)

仅保存 `errors.summary` 的旧算例同样实测了未结单独阻断、阻断单独阻断及清零后确认。新浏览器回执使用 A→V 隔离验证错误门禁。B 阶段真实工程报错、结清及明确确认继续引用既有 CadQuery 工作流测试。字段齐备、网格质量和所有工程阶段转换由 FF-A14 等功能分别验收。模型调用为 0，模拟调用为 0。[真实 B 阶段门禁](engineering-recovery-acceptance.md) · [A/V 阶段与权限](FF-A05-casedict-authority.md)

隔离候选通过后，已合入相同 SHA-256 的源码并复跑。首次合入后运行时，证据刷新使浏览器确认草稿过期，未发出阶段请求。最终复跑先核对页面显示的当前证据，再准备草稿，且记录准备动作只在页面内发生。[候选回执](../../tmp/case-error-gate-kgysbfxk/case-error-gate-result.json) · [合入后首次回执](../../tmp/case-error-gate-4v4cb_tz/case-error-gate-result.json)

在项目根目录复跑，复用当天的前端构建目录和已安装 Playwright 的本机 Python。

```powershell
& 'platform/server/.venv/Scripts/python.exe' -B -X utf8 `
  'platform/server/case_error_gate_browser_acceptance.py' `
  --dist 'tmp/codex-wsl-profile-20261119/web-dist' `
  --browser-python 'C:/ProgramData/anaconda3/python.exe' --channel msedge
```
