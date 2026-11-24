# ParaView Catalyst 和提取器（FF-D17）

## 离线提取

在算例的可视化面板展开「ParaView 来源与过滤器」，选择算子后在「提取器」勾选 VTK、CSV 或 PNG。应用属性会提交原生 ParaView 作业。完成后可展开各格式的结果并下载原件。并行 VTK 结果保留索引文件和它引用的所有分块。

`POST /api/v1/cases/{case_id}/paraview/pipeline` 同时支持单文件 `file` 和同扩展名的有序文件序列 `files`。原生读取器确定时间值。`start`、`end` 按从零开始的时间步序号选择闭区间，`every` 是间隔。省略 `end` 时使用最后一个有效时间步。例如从五个时间步中选出第 0、2、4 步的温度表。

```json
{
  "nodes": [
    {"id": "mesh", "group": "file", "files": [
      "D-post/temperature_0.vtk", "D-post/temperature_1.vtk", "D-post/temperature_2.vtk",
      "D-post/temperature_3.vtk", "D-post/temperature_4.vtk"
    ]}
  ],
  "extractors": [{"id":"temperature","node":"mesh","format":"CSV","start":0,"end":4,"every":2}]
}
```

`pipeline.json` 记录使用的原生格式、实际时间步及发布文件。作业失败时，该版本的输出目录不会对外发布。提取和属性动画可随同一次任务提交，分别沿数据步和属性时间轴生成输出。提取器的行为参见 ParaView 文档。 [Extractors](https://docs.paraview.org/en/v5.11.2/UsersGuide/savingResults.html#extractors)

## 求解时触发 Catalyst

将驱动和分析状态上传到同一算例，在「Catalyst 原位分析」选择两个文件及与当前环境相符的版本。作业面板记录驱动输出，完成后在算例中读取提取文件和 `catalyst.json`。

| 本地 ParaView 版本 | 接口 | 示例 |
| --- | --- | --- |
| Windows 5.11.2 | Catalyst 1，`vtkCPProcessor` | [驱动](../server/examples/paraview/catalyst_v1_driver.py) · [分析状态](../server/examples/paraview/catalyst_v1_state.py) |
| Linux 5.11.2 OSMesa | Catalyst 1 与装有 ParaView 实现的 Catalyst 2 | [Catalyst 2 驱动](../server/examples/paraview/catalyst_v2_driver.py) · [分析状态](../server/examples/paraview/catalyst_v2_state.py) |

`GET /api/v1/cases/{case_id}/paraview/catalyst/capabilities` 返回已安装的版本。选择其他构建时，Catalyst 2 可用 `CATALYST_IMPLEMENTATION_PATHS` 指定包含 `libcatalyst-paraview.so` 或 `catalyst-paraview.dll` 的目录。版本检测查看实际程序和插件路径。

向 `POST /api/v1/cases/{case_id}/paraview/catalyst` 传入 `driver_path`、`state_path`、`version` 及可选的 `backend`。驱动运行于冻结的算例副本，使用 `VIBEFLOW_CATALYST_STATE` 读取分析状态，并通过 `VIBEFLOW_CATALYST_OUTPUT` 取提取目录。两个示例均在仿真循环中向 Catalyst 传递内存数据。Catalyst 1 使用 VTK 数据描述，Catalyst 2 使用 Conduit Mesh Blueprint。 [Catalyst 接口](https://docs.paraview.org/en/latest/Catalyst/getting_started.html) · [Conduit Mesh Blueprint](https://llnl-conduit.readthedocs.io/en/latest/blueprint_mesh.html)

返回的 `job_id` 使用现有作业状态、日志和取消接口。成功的 `catalyst.json` 保存冻结的驱动和状态相对路径，以及每个提取文件的大小和 SHA-256。每个运行独占 `D-post/paraview-catalyst/<版本目录>`，同一幂等键可安全重试响应。发生原生错误、无输出或输入被修改时拒绝发布，历史成果可继续读取。

## T06 实测

Windows 和非特权 Linux 的算例 API 均从五个 VTK 步生成第 0、2、4 步的并行 VTP、CSV 和 PNG。回读最后一步的原生几何为 3 点，温度 328 K。Windows Catalyst 1 的原生 VTI 中心温度为 400、316、302.56 K。Linux Catalyst 2 的 81 点网格中心温度为 400、325、313.0225 K，PVTU 与 CSV 一致。两端都验证幂等请求、失败不发布和历史摘要保持。Edge 在 1440、900 和 390 px 验证提交及下载，CSV 共 50 行节点数据。 [验收证据](../../tmp/FF-D03-D05-server-manager-20261123/d17-acceptance.json) · [移动端截图](../../tmp/ff-d17-extract-edge-fnm8orzk/extractors-390.png)

API 集成复验修正了启动目录准备、日志回传和受管清单字段。Docker Desktop 的 Windows 目录挂载不支持测试使用的 Linux 原子目录变更，Linux 验收以容器的 Linux 文件系统运行，结束后将日志和成果复制到 D 盘。构建与验收区间内 Windows C 盘可用空间从 17,116,143,616 字节变为 16,712,089,600 字节，Docker 未执行缓存回收。 [Windows 前](../../tmp/FF-D03-D05-server-manager-20261123/d17-linux-before-c.json) · [Windows 后](../../tmp/FF-D03-D05-server-manager-20261123/d17-linux-after-c.json) · [Docker 前](../../tmp/FF-D03-D05-server-manager-20261123/d17-linux-before-docker.txt) · [Docker 后](../../tmp/FF-D03-D05-server-manager-20261123/d17-linux-after-docker.txt)
