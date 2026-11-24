# ParaView Server Manager 目录

`GET /api/v1/cases/{case_id}/paraview/catalog` 从执行端已配置的 `pvpython` 读取 Server Manager 的 Sources 与 Filters 编目。查询可用 `group` 选择类型，用 `q` 搜索原生名称或标签。`GET /api/v1/cases/{case_id}/paraview/catalog/{group}/{name}` 返回该项的原生属性名、类型、面板可见性、默认值及声明的约束。两个入口均复核算例读权限。

已安装的 Windows ParaView 读取到 159 个 Sources 与 251 个 Filters。相同接口所用的提取代码在现有 Linux ParaView 容器读取到 245 个 Sources 与 258 个 Filters。目录随运行时构建和已注册定义变化。API 的 410 个 Windows 条目逐项与实际运行时一致。另核验 Sphere 的默认半径 0.5、Contour 的输入数组约束、过滤搜索、未知项和未认证请求。[Windows API 验收](../../tmp/FF-D03-D05-server-manager-20261123/catalog-api.json) · [Windows 原生定义](../../tmp/FF-D03-D05-server-manager-20261123/catalog-windows.json) · [Linux 原生定义](../../tmp/FF-D03-D05-server-manager-20261123/catalog-linux.json) · [ParaView Server Manager 文档](https://www.paraview.org/paraview-docs/v5.11.0/python/paraview.servermanager.html)

## 应用属性与查看结果

在工作台的「可视化」面板展开「ParaView 来源与过滤器」。选择可执行的来源或过滤器后，可在 Inspector 修改原生属性。过滤器从算例选取 VTK 数据集，由已安装的 `pvpython` 读取实际数组，表单显示名称、点或单元归属以及分量数。提交后由 ParaView 原生输入域和属性域校验。作业成功时可以查看结果或撤销到上一版的属性及数据；两次计算所发布的文件都可从作业历史读取。

`POST /api/v1/cases/{case_id}/paraview/pipeline` 接受按依赖排序的节点。文件节点读取算例内的 VTK 数据，来源和过滤器节点引用先前的输出。平台封存执行输入，使用既有受管 `pvpython` 或 `pvbatch` 运行。成功后每个作业发布自己的 VTK 数据、`pipeline.pvsm` 和 `pipeline.json`。后者记录参数、点数、单元数、几何边界及结果路径。参数超出原生域或引用无效时，作业不发布结果。重复提交同一幂等键返回原作业。

Windows 和现有 Linux ParaView 容器各实跑 29 个已接入算子，覆盖来源、几何操作、过滤、数据关联转换和字段运算。平台 API 验证了 `pvpython`、`pvbatch`、发布后的数据重读及既有脚本入口。真实 Edge 在 1440、900 和 390 像素宽度实测球体半径 1 到 2、撤销后重新打开第一版、关闭渲染会话以及选取实际 RTData 字段生成 884 个等值面单元。[平台 API 回执](../../tmp/FF-D03-D05-server-manager-20261123/pipeline-api.json) · [Windows 算子矩阵](../../tmp/FF-D03-D05-server-manager-20261123/operator-windows/matrix.json) · [Linux 算子矩阵](../../tmp/FF-D03-D05-server-manager-20261123/operator-linux/matrix.json) · [Edge 验收](../../tmp/ff-d03-pipeline-edge-agg9hcqk/catalog-browser.json)

## 输入域、复合属性和执行类别

选择「按输入数据筛选」后，ParaView 读取实际 VTK 数据及字段，用各算子的原生输入域判断数据能接入哪些端口。目录显示候选节点及可接入端口。只有一个兼容端口时，Inspector 自动绑定所选数据。命名输入各自选择数据，同一个可重复端口支持按顺序连接多个数据集。时间统计接受按时间排序的同格式文件。需要多个端口的算子在原生执行前核对是否已连接全部必需端口。

属性编辑从已安装的 Server Manager XML 取得复合类型和内部字段，解析 Plane 等别名到原生定义。实际验收覆盖布尔、枚举、单值、向量、字段选择、字段列表、复合代理、命名输入、重复输入及应用与撤销。Windows 和 Linux 两端各通过 35 个实际工作流。代表结果包括 10 条流线单元、210 个 Glyph 单元、2 块合并数据，以及两个时间步的 RTData 标准差。Edge 在 1440、900、390 像素复验复合参数与命名输入。浏览器测试记录了两次 `storage_quota_busy`，通过原幂等键的短暂重试继续。 [验收汇总](../../tmp/FF-D03-D05-server-manager-20261123/d03-d05-acceptance.json) · [Edge 回执](../../tmp/ff-d03-d05-ports-edge-qpwc2qdl/native-ports-edge.json)

目录覆盖已装定义，可执行集合以配置的算子和输入形态为准。原生插件各自的运行与兼容由 FF-D20 继续验收。此段真实模型调用 0，模拟调用 0。
