# FF-B03 几何预览精度与 OCP 数据

几何预览接受可选正数 `tolerance`，默认 0.1。服务端将非默认值写入注册作业参数，并核对场景和验证记录与登记值一致。旧请求和旧作业仍使用默认值。同一个幂等键重试相同精度返回原作业，改用另一精度返回 409。[预览 API](../server/app/routers/geometry.py) · [注册几何作业](../server/app/services/geometry_handler.py)

场景保留 Web 界面使用的三角网格，同时输出 OCP 实例、对象树、包围盒及编码缓冲，其中包含顶点、三角面、法线、边和面索引。组装后的子件仍在对象树内。OCP 依赖不可用或转换出错时，继续发布已经生成的正常三角网格和工程验证，并记录实际失败类型。[几何执行器](../server/app/services/geo_script.py)

五个实际 CadQuery 作业在隔离 SQLite 和文件系统上通过。单个算例覆盖带倒圆和钻孔的实体、球体、开放平面及双部件装配。两个精度下均验证了五个 OCP 形体和可解码缓冲。球体普通面数从 8,002 降为 528，OCP 球体三角索引数从 24,006 降为 3,732。两个隔离作业分别令 OCP 依赖不可导入和转换抛错，真实预览成功并保留原三角网格和工程验证；恢复后重新生成 OCP 数据。旧发布和 caseDict 完整读回，五种无效请求返回 422。[原生验收](../../tmp/geometry-ocp-ut6w5g64/geometry-ocp.json) · [复验入口](../server/geometry_ocp_acceptance.py)

首轮测试对离散网格包围盒使用了过严的固定误差。板件实际宽度为 9.0006，修正为按请求精度核对离散包围盒后，同一生产代码通过。[首轮记录](../../tmp/geometry-ocp-qf4oiuun/geometry-ocp.json)

真实 Edge 验证了 0.025 的手动选择、刷新后的记忆，以及保存脚本自动使用默认 0.1。界面分别显示手动选择与场景实际值，授权范围内的 caseDict 无写入。现有浏览器画布继续使用场景中的三角网格。[Edge 回执](../../tmp/geometry-tolerance-browser-3h8a8207/geometry-tolerance-result.json)

预览复用现有的有界配额等待后，真实 Edge 手动提交 0.025 的 OCP 回退作业也通过。发布包含 2,014 个顶点和 2,008 个三角面，画布与实际回退原因均可见。暂时繁忙及此前的 SQLite 锁证据保留在界面专项记录。[手动回退回执](../../tmp/geometry-tolerance-browser-_rg8jxvv/geometry-tolerance-result.json) · [界面测试记录](FF-B03-preview-ui.md)
