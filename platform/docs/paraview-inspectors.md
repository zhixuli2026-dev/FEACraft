# 比较视图、内存和材质（FF-D18）

在算例可视化面板选择 ParaView 算子，展开「比较视图、内存和材质」。勾选生成检查结果后，可保留单视图，或填写各格的数值并选择网格或叠加布局。各格共享提交时选定的前视、俯视或等轴视角。结果保存原生 PNG、各格点数与单元数、实际内存记录和 `pipeline.pvsm`。参数研究的布局、共享视角、数据时刻和叠加模式依据 ParaView 的原生比较视图。 [ParaView 5.11 比较视图](https://docs.paraview.org/en/v5.11.2/ReferenceManual/comparativeViews.html)

`POST /api/v1/cases/{case_id}/paraview/pipeline` 可传 `inspection.comparison`。每条轨道的 `values` 数量等于行列乘积，按先行后列排布。原生数值属性用节点 ID 和属性名，显示属性使用 `@display`，已有文件序列的原生数据时刻使用 `{"node":"@time","property":"Time","values":[0,1]}`。服务以原生数据和属性域校验每格输入。

## 内存

受管比较报告记录 `pvpython` 或 `pvbatch` 的当前进程和宿主内存。打开实时渲染会话后，在侧栏「内存检查器」可读当前进程常驻内存、峰值、宿主可用和总量。默认随管线更新，支持手动刷新。Linux 使用系统进程计数，Windows 使用工作集和物理内存 API。数据集内存估算仍可在原有信息面板查看。 [ParaView 内存检查器](https://docs.paraview.org/en/v5.11.2/ReferenceManual/memoryInspector.html) · [Windows 工作集](https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-process_memory_counters)

## 材质

PBR 模式支持基色、金属度、粗糙度、不透明度和清漆。Windows 和 Linux 使用原生 ParaView 属性并导出实际渲染。配备 OSPRay 的 Linux 工具链还可在单视图加载、修改和导出带名称的材质库。随附的 Windows 5.11.2 使用 PBR。 [ParaView PBR 属性](https://docs.paraview.org/en/v5.11.2/ReferenceManual/objectShadingProperties.html) · [Kitware 材质编辑器](https://www.kitware.com/the-material-editor-in-paraview/)

在「OSPRay 材质库」输入所选材质名与要创建或修改的定义，传入 `file` 可合并算例中的 OSPRay 0.0 格式库，`remove` 按名称移除导入库中的条目。成功后可直接下载新库并在后续任务导入。导入文件的纹理相对库目录定位，导出的文件记录算例根相对路径。导入与提交均经过算例文件校验。

```json
{
  "name": "CoatedBlue",
  "materials": {
    "CoatedBlue": {
      "type": "Principled",
      "doubles": {"baseColor": [0.15, 0.35, 0.85], "roughness": [0.35]}
    }
  }
}
```

## T06 实测

Windows 和非特权 Linux 的受管作业均以同一球体对照半径 0.5、0.8、1.1、1.4，原生四格非背景像素为 1,968、4,990、9,395、15,225。两端叠加视图、两步时间序列、PBR 改色及非法非标量拒绝通过。Edge 在 1440、900 和 390 px 核对四格，实际刷新渲染会话的内存并验证退出；该次读取常驻 222.1 MiB、峰值 226.6 MiB。Linux OSPRay 实际完成创建、重导入、修改、移除和浏览器下载，前后材质输出的中心像素由蓝色变为橙色。 [验收证据](../../tmp/FF-D18-visualization-inspectors-20261123/d18-acceptance.json) · [比较窄屏](../../tmp/ff-d18-inspect-edge-fnvf1vxg/compare-390.png) · [OSPRay 浏览器预览](../../tmp/FF-D18-visualization-inspectors-20261123/ray-edge.png)
