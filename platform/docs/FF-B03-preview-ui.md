# FF-B03 几何预览的容差与显示

几何面板分别展示重建时填写的容差和当前场景实际应用的容差。手动重建接受大于零的有限数值，通过正式预览接口提交。浏览器会话按账号和算例记住已接受的手动选值。保存几何脚本仍触发默认容差 0.1，场景加载完成后更新实际值。caseDict 与修订号保持原样。

真实 Edge 与 CadQuery 验证了输入拦截、手动提交、刷新及保存自动预览。手动容差 0.025 在原生作业日志、场景和验证记录中一致，当前发布包含 OCP 数据和 2008 个三角面。刷新后仍可使用 0.025。随后编辑保存，页面同时显示填写值 0.025 和实际应用值 0.1。[手动与自动验收回执](../../tmp/geometry-tolerance-browser-3h8a8207/geometry-tolerance-result.json) · [自动预览画面](../../tmp/geometry-tolerance-browser-3h8a8207/automatic-preview-tolerance.png)

首次回退验收中，正式保存已接收脚本，原生 CadQuery 日志记录了可选 OCP 导入失败和三角场景生成。该轮发布停在收尾阶段。同轮作业列表的 SQLite 查询记录写锁异常。保留这一失败回执，复跑时先等待页面自身的轮询，再核对作业详情。[首次回退回执](../../tmp/geometry-tolerance-browser-8nxgi12j/geometry-tolerance-result.json) · [SQLite 日志](../../tmp/geometry-tolerance-browser-8nxgi12j/api.log)

第二轮编辑器记录了 `storage_quota_busy` 响应正文，对应错误定义为 HTTP 409。隔离磁盘已写入完整脚本，数据库没有新建作业、尝试或发布。保存后的自动预览准入故障由 FF-B05 跟进。[第二轮回执](../../tmp/geometry-tolerance-browser-4bc631xc/geometry-tolerance-result.json) · [落盘与数据库只读核对](../../tmp/geometry-tolerance-browser-4bc631xc/save-admission-observation.json)

第三轮通过正式文件接口保存测试脚本，默认 0.1 预览退出码为 0。可选 OCP 导入在隔离作业内失败，发布的圆柱包含 506 个顶点和 500 个有效索引三角面。Edge 验证了「已应用容差 0.1」和「三角网格回退（OCP 不可用）」。随后点重建提交 0.025，显式预览接口同样返回 `storage_quota_busy`，只读数据库确认仅有默认作业与发布。[作业与浏览器核对](../../tmp/geometry-tolerance-browser-uieabkul/fallback-manual-admission-observation.json) · [第三轮回执](../../tmp/geometry-tolerance-browser-uieabkul/geometry-tolerance-result.json)

预览准入启用现有的限时额度等待后，同一夹具复验通过。Edge 手动提交 0.025，原生 CadQuery 作业退出码为 0。场景与验证回读均为 0.025，指向同一新发布。Edge 显示 OCP 不可用的三角网格回退提示和包含 2014 个顶点、2008 个三角面的圆柱。算例字典与修订号保持原样。[最终通过回执](../../tmp/geometry-tolerance-browser-_rg8jxvv/geometry-tolerance-result.json) · [手动回退画面](../../tmp/geometry-tolerance-browser-_rg8jxvv/fallback-rounded-preview.png) · [原生作业日志](../../tmp/geometry-tolerance-browser-_rg8jxvv/files/workspaces/1/cases/1/.vibeflow/jobs/job-2.log)

专用入口为 [geometry_tolerance_browser_acceptance.py](../server/geometry_tolerance_browser_acceptance.py)。验收使用隔离的本机文件存储、SQLite 和实际注册的 CadQuery 作业，模型调用为 0。
