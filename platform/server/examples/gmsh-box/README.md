# Gmsh → OpenFOAM → 平台预览

本例只生成并检查网格，不运行流场求解。需要 WSL 内实际安装的 gmsh 和已配置 OpenFOAM v2406；平台 Python 需 meshio/VTK。

将 `box.geo`、`run.sh` 上传到算例 `B-mesh/gmsh/`，将 `controlDict`、`fvSchemes`、`fvSolution` 上传到 `C-run/case-gmsh/system/`。通过现有作业入口选择 `runtime=wsl`、`type=mesh`，命令为：

```bash
source /usr/lib/openfoam/openfoam2406/etc/bashrc && bash B-mesh/gmsh/run.sh
```

如果 Setup 使用其他 OpenFOAM 安装路径，应使用实际配置的 bashrc。脚本从算例根目录执行；不需要 root。输出是 `B-mesh/gmsh/box.msh`、`C-run/case-gmsh/constant/polyMesh`、原生日志和 `toolchain.json`。

网格使用 ASCII MSH 2.2，并设置 `walls` 表面组和 `fluid` 体积组供 `gmshToFoam` 保留边界/区域。[Gmsh 官方命令说明](https://gmsh.info/doc/texinfo/#Gmsh-command_002dline-interface)给出 `-3` 和 `-format` 参数。修改几何后应重新生成和执行 checkMesh。

完成后调用 `POST /api/v1/cases/{id}/geometry/msh-preview`，body 为 `{"path":"B-mesh/gmsh/box.msh"}`；再调用 `POST /api/v1/cases/{id}/mesh:evaluate`，body 为 `{"log_path":"C-run/case-gmsh/checkMesh.log","run_dir":"C-run/case-gmsh"}`。预览返回真实 VTU 路径，质量报告保留实际日志摘要和当前网格参数绑定。

2026-11-16 实际验收得到144节点、391四面体、655个含边界的预览单元，执行UID65534，gmsh4.12.1，checkMesh通过。不同版本或算法的单元数可能变化，不应把这些数字硬编码为网格生成结果。该例不替代 snappyHexMesh、cfMesh、复杂边界层或所有网格类型的全功能验收。
