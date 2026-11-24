# FF-B18 网格转换、质量和边界成果

托管网格发布 `B-mesh/artifacts.json`。索引记录 `mesh.artifacts` 的 `meshScript`、`meshFile`、`convertedMesh`、`qualityReport` 和 `boundaryReport`，并附原始输入、实际原生网格文件、质量报告及边界报告的尺寸和 SHA-256。显式路径需与运行目录和该网格源的实际文件一致，工作流会将错误声明记录为被阻止的步骤。索引的内容从封存的原生输入和输出复核，求解封存引用同一网格发布中的索引与网格。 [索引和转换证据](../server/app/services/mesh_artifacts.py) · [发布复核](../server/app/services/mesh_publication.py)

Gmsh 转换会核对 ASCII MSH 版本、`gmshToFoam` 的节点和元素数量、单元族，以及 `checkMesh` 的原生体单元数。两套隔离环境均通过真实 Gmsh、`gmshToFoam`、`checkMesh` 和 `laplacianFoam`。125 节点、160 输入元素生成 64 个六面体，保留三个物理边界和 `solid` 单元集。`T(x)=300+100x` 的实际温度误差为 0，工作流确认 D。显式错误引用在启动网格前被拒绝，算例保留 B。 [文件系统验收](../../tmp/ff-b18-gmsh-artifacts-zkxiem43/mesh-artifacts.json) · [认证对象存储验收](../../tmp/ff-b18-gmsh-artifacts-m_ezcpe5/mesh-artifacts.json) · [错误引用](../../tmp/ff-b18-gmsh-artifacts-bumt1ou5/mesh-artifacts.json)

拥有算例读权限的用户能通过 `/mesh/results` 查看当前或历史索引，通过版本化原件端点下载转换网格。修改工作副本的网格脚本后，旧发布显示为过期，原件和索引仍可读取。两种存储均核对整件 SHA-256、HTTP Range 与其他算例 404。 [完整回执](../../tmp/FF-B18-mesh-artifacts-20261122/validation.json)

初次 WSL 生成与 Windows 校验使用了各自的按名称排序，造成同一批路径的列表顺序不同并拒绝发布。改用标准斜线相对路径作为统一排序键，再次验收后通过。早期无索引的已发布网格仍可按原规则复核。五区域导入也通过了网格和求解验收。 [排序诊断和旧发布复核](../../tmp/FF-B18-mesh-artifacts-20261122/sorting-and-legacy-verification.json) · [五区域最新复验](../../tmp/ff-cb06-cht-managed-331pb510/cht.json)

另在认证对象存储流程中一次性中断首次原生派发。后台重新领取已登记的同一尝试并完成真实 CadQuery、Gmsh 和求解，全程保留同一作业和尝试 ID。 [派发故障实测](../../tmp/ff-b18-gmsh-artifacts-u03kf310/mesh-artifacts.json)
