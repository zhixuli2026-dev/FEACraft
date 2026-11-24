# FF-B17 托管网格工具链

`mesh.source snappyHexMesh` 读取当前运行目录的 `system/blockMeshDict` 和 `system/snappyHexMeshDict`，先生成基础网格，再运行 `snappyHexMesh -overwrite`。`mesh.source cfMesh` 读取 `system/meshDict`，调用原生 `cartesianMesh`。`surfaceFile` 引用的算例内几何随其他原始输入封存。两种来源均使用已有注册作业、工具版本及可执行文件摘要、完整 `checkMesh`、质量门禁和版本化发布。标准算例导出带有离线划分入口。 [OpenFOAM snappyHexMesh](https://doc.openfoam.com/2606/tools/pre-processing/mesh/generation/snappyhexmesh/) · [cfMesh 原作者文档](https://cfmesh.com/wp-content/uploads/2015/09/User_Guide-cfMesh_v1.1.pdf)

现有 `blockMesh` 和 Gmsh 继续使用同一作业协议。Gmsh 可从 `.geo` 实际生成网格，也可直接转换现有 ASCII MSH 2 原件。原生输出和报告绑定到求解读到的同一发布。 [已有网格工具](mesh-workflows.md) · [原生转换产物](FF-B18-mesh-artifacts.md) · [已有 Gmsh 网格导入](FF-CB09-mesh.md)

`snappyHexMesh` 实测从立方域去除边长 0.2 m 的内部方块，得到 992 个六面体，流体总体积为 0.992 m³。文件系统和认证对象存储均通过完整检查、原生传热及 D 确认，冷热两侧独立回算的积分扩散通量均约为 `0.983927 K·m³/s`，测量差为 0。两个离线包重建后与托管 `T` 字段逐字节一致。 [文件系统](../../tmp/ff-b17-snappyHexMesh-d2a0e16r/mesh-tool.json) · [认证对象存储](../../tmp/ff-b17-snappyHexMesh-zudnqkkq/mesh-tool.json)

cfMesh 原生试验先用 `surfaceFeatureEdges -angle 30` 生成包含棱边的 `.fms`，再用带壁面名称的立方体划分得到 64 个六面体，总体积为 1 m³。早期直接使用 STL 的结果体积约 0.751 m³，改为保留棱边后通过体积对照。 [早期原生体积日志](../../tmp/FF-B17-mesh-tools-20261122/native-cfMesh-3983l205/checkMesh.log) · [FMS 原生对照](../../tmp/FF-B17-mesh-tools-20261122/native-cfMesh-zd_wosdp/validation.json)

cfMesh 两套托管环境均跑到 D。按 100 K 的边界温差衡量，温度解析误差小于 `1e-6`。对应离线包的 `faces`、`owner`、`neighbour`、`boundary` 与托管结果逐字节一致，坐标最大变化约 2.35 μm，温度差最大约 `8.04e-5 K`。Edge 从实际发布显示工具和 64 个单元。 [文件系统](../../tmp/ff-b17-cfMesh-bofswbi8/mesh-tool.json) · [认证对象存储](../../tmp/ff-b17-cfMesh-sd5fonh0/mesh-tool.json) · [浏览器](../../tmp/ff-b17-cfMesh-bofswbi8/browser.json)

`snappyHexMesh` 的保留点在域外、cfMesh 的几何原件缺失时，实际原生进程报错。作业停止已核验，流程未发布无效网格。 [域外保留点](../../tmp/ff-b17-snappyHexMesh-87jt_au1/mesh-tool.json) · [缺少几何原件](../../tmp/ff-b17-cfMesh-ed32gar7/mesh-tool.json)

另在运行中的 `snappyHexMesh` 上通过工作流取消并核验进程退出。按原输入重试后发布 26,784 个单元，体积保持 0.992 m³，确认 C。Edge 在实际发布追溯中显示源、单元数和通过的门禁。 [取消与重试](../../tmp/ff-b17-snappyHexMesh-g9zpwb_l/mesh-tool.json) · [浏览器](../../tmp/ff-b17-snappyHexMesh-qoccf20p/browser.json)

FF-B17 按原 T03 完成。网格质量预设、自适应和更多边界层仍由 FF-B15 和 FF-CB09 追踪。本次网格验收模型调用为 0。 [完整回执](../../tmp/FF-B17-mesh-tools-20261122/validation.json)
