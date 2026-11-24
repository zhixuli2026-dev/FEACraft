# FF-B05 保存几何脚本后的自动预览

本轮真实条件保存覆盖写入前及提交后的短时 SQLite 写锁、预览等待用尽及交接中换版。短锁各创建一个真实预览，超期时文件摘要仍正确，较旧请求跳过预览。Edge 单文件和全部保存均显示已保存、清除未保存状态，下一次条件写入使用新摘要。之后手动预览已生成私有场景并取得进程停止回执，作业数据库仍停在 `running`，公开场景保留上一版。整项继续验证原生退出后的状态提交。[四种公开 API 验收](../../tmp/geometry-preview-admission-zzk1vbzl/preview-admission.json) · [手动预览观察](../../tmp/geometry-autopreview-handoff-browser-1r80__g9/manual-preview-publication-observation.json)

2026-11-19 追加验收发现，编辑器收到 `409 storage_quota_busy` 时，目标脚本实际上已更换。文件事务已标记提交，并有持久 `files.changed` 事件。此时作业、尝试和发布均未创建。[故障回执](../../tmp/geometry-tolerance-browser-4bc631xc/geometry-tolerance-result.json) · [响应转录及隔离状态](../../tmp/geometry-tolerance-browser-4bc631xc/save-admission-observation.json)

路由在文件结算后启动预览。故障版本的 SQLite 配额快速准入最多等待 0.1 秒。另一次浏览器验收在批次查询时记录了 SQLite 写锁异常，两次故障分别保留，当时的持锁方仍待定位。[批次查询锁日志](../../tmp/geometry-tolerance-browser-8nxgi12j/api.log)

普通几何脚本条件保存、手动和自动预览使用有界配额等待。文件提交后遇到 0.45 秒真实 SQLite 写锁时，保存返回已提交摘要，预览只创建一次并以默认容差 0.1 正常退出。等待耗尽时，保存仍返回 HTTP 200 与新摘要，自动预览未启动，旧场景保留。在等待期间有更新版本写入时，只有新版本触发 CadQuery，旧保存回执附带 `stale_case_files`。另以独立 SQLite 写者先持锁，再发正式条件保存，确认等待结束后写入和预览均成功。[四种准入情形](../../tmp/geometry-preview-admission-zzk1vbzl/preview-admission.json)

编辑器收到已提交的几何脚本保存回执时，按算例记录返回的预览作业。单文件保存和全部保存共用这个入口。几何面板切换算例后先重置当前画面，再跟踪所选算例的预览作业。自动预览轮询只在同一已选算例内更新状态，保留上一次成功的场景。当场景读取失败，面板保留读取错误，成功作业状态仍可正常结束。

保存回执包含 `auto_preview_error` 时，编辑器仍接收已提交内容的摘要，用于下一次条件写入。提交后新输入的内容继续显示为未保存。两种保存操作都在编辑器和几何面板提示「几何脚本已保存，自动预览未启动」，并显示服务端原因。几何面板保留已加载场景，可用「重建预览」手动启动。接受新预览作业后清除本算例的交接提示。

## 2026-11-19 实测

- 使用同一当前后端和旧版前端复现。首次单文件保存刷新出 `first_solid`。随后全部保存提交了两个文件，真实 CadQuery 作业以退出码 1 失败，面板仍显示上一个作业号，失败提示为空。[旧版复现回执](../../tmp/geometry-autopreview-browser-r0a1eh29/geometry-autopreview-result.json)
- 当前版在真实 Edge 对几何脚本和另一文本同时执行全部保存，读回两份文件的保存结果。切到另一算例时，该算例的面板独立显示。原作业实际退出后，切回原算例看到对应失败作业和原发布，保留同一批对象。编辑器保存修复脚本后自动出现 `repaired_solid`，正式验证与新发布一致，期间没有手动提交预览。另一账号读取作业和几何场景均返回 404。[最终回执](../../tmp/geometry-autopreview-browser-2evtx1j1/geometry-autopreview-result.json) · [失败与旧场景](../../tmp/geometry-autopreview-browser-2evtx1j1/autopreview-failure.png) · [修复后的场景](../../tmp/geometry-autopreview-browser-2evtx1j1/autopreview-repaired.png)
- 当前版第一轮已产生修复后的新几何，但测试用浮点精确相等检查体积 24。原生 OCCT 报告值为 `23.999999999999996`，夹具改为 `1e-12` 容差并保存原始测量值。[首轮验收回执](../../tmp/geometry-autopreview-browser-qd3ahdq9/geometry-autopreview-result.json)
- 本轮让独立 SQLite 写者在真实单文件保存提交后持锁，Edge 收到 HTTP 200、保存摘要与预览准入提示，编辑器清除了该草稿的未保存状态，期间沿用旧场景。下一次全部保存采用前次回执摘要，文本已写入，几何文件在提交前收到 HTTP 409，实际仍保持前次内容。第一次独立写者已结束，第二次独立写者尚未开始。这轮没有确认造成提交前冲突的事务。[阶段回执](../../tmp/geometry-autopreview-handoff-browser-l7_iniod/geometry-handoff-result.json) · [提交前冲突的只读核对](../../tmp/geometry-autopreview-handoff-browser-l7_iniod/save-all-precommit-observation.json)
- 接入有界保存准入后，真实 Edge 下单文件保存和全部保存在提交后均返回 HTTP 200、新摘要和预览准入提示。两次都清除了未保存状态，第二次请求使用前一次回执摘要，旧发布保持可读。截图显示提示和旧几何同屏。之后手动重建返回 HTTP 200，CadQuery 在工作目录写出新场景，45 秒内页面仍显示旧几何。停止夹具后数据库中的作业与尝试仍为运行中，发布停在准备状态，退出码未核验。[两种保存验收](../../tmp/geometry-autopreview-handoff-browser-1r80__g9/geometry-handoff-result.json) · [旧几何与提示](../../tmp/geometry-autopreview-handoff-browser-1r80__g9/saved-without-preview.png) · [手动发布检查](../../tmp/geometry-autopreview-handoff-browser-1r80__g9/manual-preview-publication-observation.json)
- 作业原生收尾改为先等 SQLite 写入窗口，再核对尝试归属。修复后同一 Edge 夹具完整通过。单文件保存与全部保存均返回 200，交接提示保留同一旧场景，第二次保存使用第一次回执的摘要。切到另一算例没有提示串入。切回原算例点击重建，真实作业 #2 退出码为 0，新发布显示 `save_all_geometry`，页面清除交接提示。[最终通过回执](../../tmp/geometry-autopreview-handoff-browser-ft_v87d5/geometry-handoff-result.json) · [保存后保留旧几何](../../tmp/geometry-autopreview-handoff-browser-ft_v87d5/saved-without-preview.png) · [手动重建结果](../../tmp/geometry-autopreview-handoff-browser-ft_v87d5/manual-preview-restored.png)

验收使用 D 盘隔离本机文件存储和 SQLite，浏览器通过真实产品 API 运行 CadQuery，模型调用为 0。入口包括 [自动重建验收](../server/geometry_autopreview_browser_acceptance.py) 和 [保存交接验收](../server/geometry_autopreview_handoff_browser_acceptance.py)，均执行项目的回归审查核验。
