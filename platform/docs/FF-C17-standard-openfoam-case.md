# FF-C17 标准 OpenFOAM 算例包

在算例上使用 `GET /api/v1/cases/{case_id}/solve/package?run_dir=C-run/case-1` 下载 ZIP。请求按算例读权限授权。导出流读并校对每个输入的长度和 SHA-256，包含 `caseDict`、原生系统和常量配置、已有 `0` 和 `0.orig`。托管生成网格从其原生控制文件重建，已导入的多区域网格随包导出。导出结束会复核源文件，保留项目中的原始脚本、输入和历史结果。 [导出实现](../server/app/services/openfoam_case_package.py) · [下载端点](../server/app/routers/solve.py)

解包后在 Linux 或 WSL 加载 OpenFOAM 环境。`Allrun.pre` 生成或读取网格并执行完整 `checkMesh`，`Allrun` 从初值重新准备并运行原生串行求解器。每次都是独立的 `runs/<id>`。有 `0` 时复制该目录，只有 `0.orig` 时从它生成工作 `0`。含空格的包目录使用同一文件系统的临时目录和有效的 `PWD` 执行，结果再移回包内。运行日志及结果摘要保留在该次目录。 [包内执行代码](../server/app/services/openfoam_package_runtime.py)

`Allclean` 核对这次运行登记的文件摘要后清理未更改的副本。实测保留原 `0.orig`、`0`、网络端原脚本、已经编辑的成果、未登记的测量文件和独立目录。三次真实重跑的温度场摘要相同。在两种存储来源的下载包中验证了实际网格准备、求解、清理和重跑。 [SQLite blockMesh](../../tmp/ff-c17-standard-openfoam-ut_golqm/standard-case.json) · [PostgreSQL 认证 SeaweedFS Gmsh](../../tmp/ff-c17-standard-openfoam-e0qh_3dk/standard-case.json)

直接复验了五区域导入包。五套原生网格齐全并通过严格检查，真实 `chtMultiRegionFoam` 的 280 单元加热器最高温度为 `306.07834 K`。首次多区域测试移回目录时返回权限拒绝。实现有界重试和失败留存后，新算例通过。移回终止时，暂存目录保留实际结果并显示位置。 [五区域包回执](../../tmp/FF-C17-standard-openfoam-20261122/package-cht-r8bg4u63/validation.json) · [含原失败的完整记录](../../tmp/FF-C17-standard-openfoam-20261122/validation.json)
