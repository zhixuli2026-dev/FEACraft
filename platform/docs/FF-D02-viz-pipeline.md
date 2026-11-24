# FF-D02 可视化对象管线

管线按滤波器类型分组。每个对象显示实际渲染状态，包括可见、隐藏、无几何输出及构建失败。单击选择对象，Ctrl 或 Command 增减选择，Shift 按树顺序选择一段。活动对象保留独立标记，属性和数据信息随它更新。右键、行末操作按钮或 Shift+F10 打开同一菜单，支持显示、隐藏、只显示选中对象、删除和清空选择。[管线实现](../server/app/services/viz_kernel.py)

选择偏好按账户、算例及数据源保存在当前浏览器。同一会话刷新后保持状态。新会话中，同名同类型的对象由场景脚本重新建立后恢复选择。脚本写入通过浏览器验收时立即推送树状态。同名对象更新沿用原渲染对象，删除从活动集合移除相应记录。[会话范围](../server/app/services/viz_sessions.py)

真实 Edge 经鉴权网关打开由 VTK 生成的 450 点、896 单元球面。通过正式文件写入接口更新场景脚本，得到可绘制切面、空切面和实际构建失败的对象。测得 Ctrl/Shift 多选，右键及 Shift+F10 操作，活动对象与属性、数据信息联动，批量显隐与删除，浏览器刷新和新渲染会话恢复，以及另一数据源隔离。三个真实会话均由界面关闭并获得已核验的进程停止回执，异主返回 404，浏览器错误与模型请求均为零。[验收入口](../server/viz_pipeline_browser_acceptance.py) · [完整回执](../../tmp/viz-pipeline-d02-y7sv22_x/pipeline-result.json) · [实际多选截图](../../tmp/viz-pipeline-d02-y7sv22_x/pipeline-multiselect.png)

最初正式场景写入已生成对象，树仍显示空状态。服务端后台 watcher 缺少状态提交，修正后可即时显示。初轮 Vue 事件模板异常及浏览器刷新后需重新打开可视化标签的对照也保留。[脚本更新未显示记录](../../tmp/viz-pipeline-d02-rsopddz_/pipeline-browser.json) · [模板异常记录](../../tmp/viz-pipeline-d02-afk4elsu/pipeline-browser.json) · [刷新对照](../../tmp/viz-pipeline-d02-_qee7rjd/pipeline-browser.json)

同一版本再次跑通 FF-D04 的实际 VTU、时序 VTP 和复合 VTM，验证内存、切面数据和会话关闭。[受影响功能回归](../../tmp/viz-information-d04-kfmhvt04/viz-information-result.json)
