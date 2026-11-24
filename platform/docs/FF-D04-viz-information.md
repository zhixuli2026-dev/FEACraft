# FF-D04 可视化数据信息

在工作台打开结果文件，展开对象管线抽屉后查看「数据信息」。面板显示当前网格或所选滤波器输出的点数、单元数、边界范围、单元类型和 VTK 内存。结构化数据还显示维数，图像网格显示原点和间距。数组按点、单元和全局关联显示类型、分量数、元组数、可计算的字节数和数值范围。字符串数组显示 VTK 分配量，向量显示幅值范围。[读取和量测](../server/app/services/result_readers.py) · [工作台面板](../server/app/services/viz_kernel.py)

「读取 ParaView 源数据」在该可视化会话的进程范围内运行真正的 `pvpython`，读取当前选定的输入及时间步。它从 Server Manager 的 `vtkPVDataInformation` 取得网格类型、数据块、数组和内存。当时间步改变时，旧 ParaView 结果清除，迟到的结果丢弃。界面显示程序不可用、读取失败和超时状态。VTK 与 ParaView 报告的内存以各自运行时的 KiB 统计为准。[ParaView 5.11 API](https://www.paraview.org/paraview-docs/v5.11.0/cxx/classvtkPVDataInformation.html) · [VTK 内存统计](https://vtk.org/doc/nightly/html/classvtkDataSet.html)

复合结果原来使用错误的 VTK 基类名检测，导致实际 VTM 打开时将多块对象直接交给渲染管线并失败。修正后，渲染管线接受提取后的表面，ParaView 源数据仍报告原始复合类型及数据块。工作台主动关闭可视化时先卸载其视图，再向 API 请求停止，避免已关闭会话的浏览器重连。如果停止失败且用户仍在同一算例，原视图可以恢复。[复合结果失败回执](../../tmp/viz-information-d04-16om4fqw/viz-information-browser.json) · [工作台关闭处理](../web/src/panels/VizPanel.tsx)

真实 Edge 经正式分块上传和鉴权代理打开三种输入。工程 VTU 核对 5,233 点、10,216 单元，实际 ParaView 5.11.2 报告 482 KiB。系列中的实测大型 VTP 核对 130,050 点、260,096 单元，以及点标量、单元数组和全局字符串。切换至 1.25 时刻后，旧的 ParaView 结果清除并能读出该步 VTU。VTM 的实际 Server Manager 结果为两个数据块、5,280 点和 10,306 单元。浏览器选中切面时显示其输出信息。跨账户启动返回 404，三次点击关闭均获得已核验的范围停止回执，页面错误和模型请求均为零。[最终验收](../../tmp/viz-information-d04-h6wkrpi_/viz-information-result.json) · [大型曲面](../../tmp/viz-information-d04-h6wkrpi_/information-large-surface.png) · [复合结果](../../tmp/viz-information-d04-h6wkrpi_/information-composite.png)
