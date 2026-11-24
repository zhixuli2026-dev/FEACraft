# FF-CB05 命名材料与工作区材料库

`materials.entries` 支持用途、相态、来源、确认状态和十类物性。旧有标量仍按 SI 解释，也可使用 `{value, unit}` 记录带单位的数值。密度、动力黏度、热容、导热率、弹性模量、泊松比、屈服应力、电导率、相对介电常数和磁导率逐字段校验。字典允许既有工程自定义字段。 [材料实现](../server/app/services/case_materials.py) · [NIST SI 单位](https://www.nist.gov/pml/special-publication-330/sp-330-section-2)

在算例 `caseDict` 面板展开「工作区材料库」，可管理命名材料、读取指定版本、归档和恢复。应用时把属性和源摘要写入当前算例的 `materials.entries`，现有名称的其他材料和字典注释保留。库更新及归档后，已经应用的算例保存原属性和来源。包含算例的工作区复制及 v1 备份恢复会携带材料库历史。结构化界面、公开 API 和权限沿用工作区与算例现有规则。 [端点](api.md#workspaces--cases) · [工作台实现](../web/src/panels/CaseMaterialLibrary.tsx)

托管串行 `icoFoam` 从唯一的 `fluidDomain` 材料计算 `ν = μ/ρ`，单位换算后生成封存的 `constant/transportProperties`。工作副本中已有数值一致的原字典时沿用原文件。显式未确认的材料和冲突输入会阻止准备。运行回执、D 确认和续算使用同一封存输入契约。热、弹性与电物性已能保存、换算和验收负例，多后端求解映射沿原 FF-A07、FF-CB06 及求解器范围推进。 [串行求解输入](../server/app/services/solve_artifacts.py)

## FF-A07 活动载荷的流体材料

串行 `icoFoam` 的 `boundariesLoadsInitials.activeLoadCase` 可通过 `activeMaterials` 为唯一的 `fluidDomain` 指定一份确认的流体材料。材料必须具备正的密度和动力黏度，支持有单位的记录。托管求解与「下载标准 OpenFOAM 算例」共用 `ν = μ/ρ` 规则。可以提供与之匹配的原字典，或让平台在封存副本和导出包中生成。修改活动载荷后，从已完成流程用「重新计算」按当前输入实际运行。 [求解输入](../server/app/services/solve_artifacts.py) · [标准包输入](../server/app/services/openfoam_case_package.py)

同一坐标、网格和边界上，两个载荷分别得到运动黏度 0.001、0.002 m²/s，64 个单元的峰值速度分别约 0.437、0.541 m/s。两套存储的私有字典与速度场摘要分别一致。两档均确认 D，导出包在非特权 WSL 中独立运行并得到相同字节。拒绝未确认材料、混选两份材料和冲突原生黏度。首轮验收从已确认的阶段直接新建流程时触发了过期几何校验，改用已注册的当前输入重算流程后通过。 [SQLite 文件系统](../../tmp/ff-a07-active-cavity-material-ocot9_kh/active-material.json) · [PostgreSQL 认证对象存储](../../tmp/ff-a07-active-cavity-material-9sbev04c/active-material.json)

## 2026-11-22 实测

- SQLite 文件系统和 PostgreSQL 认证 SeaweedFS 用同一 64 单元的移动盖腔体完成真实 OpenFOAM `icoFoam`。`ν=0.001 m²/s` 时最大单元速度约 `0.437 m/s`，`ν=0.01 m²/s` 时约 `0.615 m/s`。实际日志读到了 `transportProperties`，封存哈希与调用回执一致，活跃目录未生成该文件，两组都确认 D。 [SQLite 原生回执](../../tmp/ff-cb05-materials-sg2jng2k/material.json) · [SeaweedFS 原生回执](../../tmp/ff-cb05-materials-47x6ktq4/material.json)
- 双库双存储通过两个并发写入中一个提交、一个 409，旧版本仍可读。隔离 API 重启后仍可读归档历史及原算例引用。带引号的工程备注保持原值。异账户对读、写、应用均收到 404，工作区复制、备份恢复和篡改摘要拒绝均通过。 [本地回执](../../tmp/ff-cb05-material-library-g1s2gegm/library.json) · [远端回执](../../tmp/ff-cb05-material-library-bhvp2n6_/library.json)
- Edge 实际提交负密度并收到 422，摘要获得焦点，错误链接可回到密度字段。随后更正单位与数值，新建、引用、归档再恢复，当前库版本为 3，算例仍使用版本 1。紧凑视口核对表单无横向溢出。 [浏览器回执](../../tmp/ff-cb05-material-library-g1s2gegm/browser.json) · [紧凑界面](../../tmp/ff-cb05-material-library-g1s2gegm/materials-compact.png)
- 数据库修订 `1be4eb58aa74` 在临时 PostgreSQL 实际升级，另复制旧 SQLite 结构完成升级和回退，旧工作区与外键正常。原有运行服务仍按原镜像运行。部署切换使用既有 Alembic 升级流程。回退前导出含材料历史的工作区备份，回退会删除新材料库表。 [迁移验收](../../tmp/FF-CB05-materials-20261122/migration/result.json)

以上验收调用实际数据库、对象存储、浏览器及求解器，模型请求 0。纯输入核对验证了原有普通和压缩字典保留、冲突数值拒绝和未确认材料拒绝。初次 SQLite 重放发现时间戳格式差异，首次 Edge 发现内嵌表单选项影响可访问名称，修正后复验通过。 [汇总、原失败和清理](../../tmp/FF-CB05-materials-20261122/validation.json) · [材料实算](../../tmp/FF-CB05-materials-20261122/check.py) · [材料库集成](../../tmp/FF-CB05-materials-20261122/library.py)
