# FF-CB04 几何配置与网格联动

算例字典检查几何来源、生成器、拓扑、正比例、对称、周期、水密和流形设置。有坐标系声明时检查原点与各轴的三个有限分量。三个轴一同提供时检查线性无关和左右手方向。旧模板、未填写的字段和工程自定义条目继续可读。[几何 schema](../server/app/services/casedict_schema.py)

算例元数据的「检查配置」会提示显式矛盾，包括关闭周期却声明周期拓扑或接口，关闭对称却指定对称边界，导入网格与生成网格配置并存，以及运动或多孔拓扑与对应耦合物理开关冲突。维度和学科沿用同一入口的检查。提示允许经过工程确认的混合工作流保存。原有水密及流形阶段检查按真实 CadQuery/OCCT 测量执行，缩放、镜像和周期复制由几何操作显式触发。[配置检查](../server/app/services/case_meta.py) · [几何声明和原生修复](FF-A17-geometry-evidence.md)

2026-11-22，SQLite 搭配文件系统及 PostgreSQL 搭配认证 SeaweedFS 均通过字典负例和修订保护。两个组合用实际 CadQuery 导出体积 `0.0001` 的闭合腔体，确认水密与流形，经 B 确认启动真正的 OpenFOAM `blockMesh` 与 `checkMesh`，随后确认 C。Edge 的元数据表单显示周期、对称和网格来源三条提示。历史 Edge 验收覆盖水密门禁、固定发布回读和跨账号读取返回 404。两个当前模板与运动／多孔配置另行通过纯规则检查。[当前四次原生流程与清理](../../tmp/FF-CB04-geometry-schema-20261122/validation.json) · [Edge 配置提示](../../tmp/ff-cb04-geometry-schema-f0o5h2rf/geometry-advisories.png)

早期认证 SeaweedFS 作业遇到一次暂态存储占用，经同一工作流重试并成功确认 C。四个隔离 API 均已退出，两个临时 PostgreSQL 库已删除。此专项涉及真实基础设施、CAD 和网格，模型请求为 0。[暂态回执](../../tmp/ff-cb04-geometry-schema-hoheqwc0/launch-errors.txt)

复跑使用服务端虚拟环境进入 `tmp/FF-CB04-geometry-schema-20261122/check.py`。文件存储浏览器验收使用 `--browser`，认证对象存储验收使用 `--postgres --s3`。如需回退可分别撤回几何 schema 和配置提示，几何发布仍采用原 v1 记录。[当前验收入口](../../tmp/FF-CB04-geometry-schema-20261122/check.py)
