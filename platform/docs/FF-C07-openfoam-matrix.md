# FF-C07 OpenFOAM 原生求解矩阵

20 时刻溃坝现已在 PostgreSQL 与认证对象存储贯通 B–E，实际 D 和 E 请求耗时为 19.94 秒和 29.02 秒。末态 `alpha.water` 与独立原生场逐字节一致，水体积为 `0.0006321253811267315 m³`，指标误差为 0，并由公开入口生成含独立参照的工程报告。 [完成回执](../../tmp/ff-c14b-dam-break-im_46x7x/dam-break.json) · [临时 PostgreSQL 清理](../../tmp/terminal-agent-postgres-s_lrv25b/cleanup.json)

此前一轮使用 `Prefer: respond-async` 在 24.8 秒完成 D，后台实际完成后处理，E 拒绝证据。四时刻算例有单独的双存储 E 证据。 [早期复验](../../tmp/ff-c14b-dam-break-j3tw77xf/dam-break.json) · [临时 PostgreSQL 清理](../../tmp/terminal-agent-postgres-lc4gqn4j/cleanup.json)

后续复验捕获 D 的确切失败为 `TimeoutError`。实际求解已发布，共享请求时限耗尽导致证据读取退出。D/E 的对象存储核验现读取统一的版本快照，每组最多四个条件读取，逐文件计算完整 SHA-256，并复核读取前后实际版本。E 还复用同一确认中的当前发布来源检查。核验超时按可重试状态返回。 [D 原错误](../../tmp/ff-c14b-dam-break-iju8x72o/solve-gate-diagnostics.txt) · [版本核验](../server/app/services/case_evidence.py)

在原失败归档的隔离对象存储副本上复核七件发布产物。更改公开的 `1/alpha.water` 后，确认门禁指出该文件并拒绝。还原原字节后，同一个归档再次通过。此轮仅校验证据，完整工程流程另见上方回执。 [实际对象存储篡改与还原](../../tmp/FF-C14B-dam-break-20261123/postprocess-gate-parallel-replay.json)

现有托管路径通过封存的网格和求解输入启动 OpenFOAM，逐个验证写出时刻、场及版本来源。不同物理的输入、解析基准和可发布场分别验证。原 `icoFoam` 的二维、三维运行和 `laplacianFoam` 导热回归记录在主线基准。 [主线工程基准](N7-03-cae-benchmark-progress.md)

## 一维声压驻波

在 1 m 直管内使用 OpenFOAM 2406 `acousticFoam`。设置声速 100 m/s、两端刚壁、均匀 64 个单元及零初始变化率。对 1 Pa 初始幅值，独立解析解为 `p(x,t) = cos(πx) cos(πct) Pa`。原生与托管均执行 50 步，写出 0.00125 s 和 0.0025 s 两时刻。 [求解器原生方程](https://api.openfoam.com/2406/acousticFoam_8C_source.html) · [独立原生算例](../../tmp/FF-C07-acoustic-20261123/native/log.acousticFoam)

SQLite 文件系统和 PostgreSQL 认证 SeaweedFS 均完成 B、C、D。最终 64 个 `pa` 数值与独立原生执行逐项相同，范围为 ±0.7013926929411 Pa，解析最大差为 0.0055011209 Pa。两个发布的声压原件和前一时间层 `pa_0` 的文件摘要均一致。隔离数据已清理。 [文件系统](../../tmp/ff-c07-acoustic-kivr7hjj/acoustic.json) · [认证对象存储](../../tmp/ff-c07-acoustic-v396nio_/acoustic.json) · [临时 PostgreSQL 清理](../../tmp/terminal-agent-postgres-mgcxw79y/cleanup.json)

首轮原生执行已经写出物理解，发布门禁漏识别 OpenFOAM 的 `diagonal` 残差日志。补齐识别后，第二轮发现 VTK 不展示原生前一时间层的 `pa_0`。现将该字段按原生压力单位、实际网格的单元数和有限标量另行验证，同时继续用 VTK 验证当前声压和边界。两时刻的二进制原生文件另通过旧时间层核验。 [日志识别](../server/app/services/native_openfoam_residuals.py) · [发布检查](../server/app/services/solve_publication.py) · [首轮日志](../../tmp/ff-c07-acoustic-4qmbpdq0/files/workspaces/1/cases/1/.vibeflow/jobs/job-4.log) · [旧时间层原生运行](../../tmp/FF-C07-acoustic-20261123/binary/log.acousticFoam)

本轮为实际求解、数据库与对象存储验收，模型调用为 0。声学结构耦合的验收归 FF-CB07。 [接口物理验证](FF-CB07-interfaces.md)

## 理想气体激波管

OpenFOAM 2406 `rhoCentralFoam` 在 100 单元直管中从 100,000 / 10,000 Pa 初压和零初速求解到 0.007 s。独立按理想气体精确 Riemann 解比较，压力与流向速度的相对 L2 分别为 0.0138951 和 0.0403612，解析激波位于距膜片 3.8794 m。SQLite 文件系统完成 D，时刻末的压力、速度、密度和温度按文件摘要比对，与独立原生输出逐字节一致。 [OpenFOAM 原生输入](https://doc.openfoam.com/2306/tools/processing/solvers/rtm/compressible/rhoCentralFoam/) · [独立解析和原生结果](../../tmp/FF-C07-compressible-20261123/native-shock.json) · [SQLite 托管回执](../../tmp/ff-c07-compressible-883dmt_f/compressible.json)

求解器日志记录守恒动量 `rhoUx` 和能量 `rhoE`。发布保留这些日志，同时按原生结果契约核验 `rho`、`p`、`T`、`U` 的尺寸及有限性。用真实压力管输出测试缺少最后温度场的清单，会被发布检查拒绝。 [强制输出字段](../server/app/services/solve_publication.py) · [缺温度场检查](../../tmp/FF-C07-compressible-20261123/missing-field.json)

PostgreSQL 认证 SeaweedFS 同样确认 D，四个字段摘要与文件系统一致。首次对象存储用例达到本机测试客户端的等待上限，复验保留实际服务端确认和原生字段证据。 [认证对象存储回执](../../tmp/ff-c07-compressible-lja2yx1n/compressible.json) · [临时数据库清理](../../tmp/terminal-agent-postgres-2n5m_ucj/cleanup.json)

## 溃坝两相流

使用 OpenFOAM 2406 `interFoam` 的水气溃坝教程。2268 个单元通过生产网格检查，真实 `setFields` 初始化 324 个水单元。开放顶边界算例运行到 1 s，由原生网格体积和 `alpha.water` 独立积分。在 SQLite 文件系统，托管末态的 `alpha.water` 与独立原生文件逐字节相同，实际后处理得到 `0.0006321253811267315 m³`，初态为 `0.0006460986219848665 m³`。1 s 时 545 个单元的相分数位于 0.01 和 0.99 之间，确认 E 后完成实际组稿。 [OpenFOAM 溃坝教程](https://www.openfoam.com/documentation/tutorial-guide/4-multiphase-flow/4.1-breaking-of-a-dam) · [独立原生基准](../../tmp/FF-C14B-dam-break-20261123/native/dam-break-native.json) · [文件系统及报告](../../tmp/ff-c14b-dam-break-9angau7g/dam-break.json)

首次实际托管计算被临时压力校正字段 `pcorr` 拦截。发布现在保留该原生残差，并明确验证写出的 `p_rgh`、`U` 和实际求解的相分数。 [首次验收记录](../../tmp/ff-c14b-dam-break-8wzg4vu0/dam-break.json) · [发布契约](../server/app/services/solve_publication.py)

按 0.25、0.5、0.75、1 s 四个写出时刻重新独立求解并通过两套托管存储。两套末态 `alpha.water` 摘要相同，实际体积分为 `0.0006319950106539104 m³`，独立积分为 `0.0006319950106539105 m³`，均确认 E 并发布报告。20 个写出时刻的文件系统运行通过，对象存储完成求解发布后在 D 确认返回 `evidence_invalid`，仍保留该限制和失败记录。 [独立原生四时刻](../../tmp/FF-C14B-dam-break-20261123/sparse/dam-break-native.json) · [文件系统](../../tmp/ff-c14b-dam-break-33b9tvzw/dam-break.json) · [认证对象存储](../../tmp/ff-c14b-dam-break-31vqsnrl/dam-break.json) · [20 时刻对象存储记录](../../tmp/ff-c14b-dam-break-p4iskyq1/dam-break.json) · [临时数据库清理](../../tmp/terminal-agent-postgres-qgf40nbw/cleanup.json)

20 时刻的对象存储诊断复跑保留了正常结束的原生求解日志，并记录后处理封存获取到算例存储租约。客户端达到 150 秒请求读取上限，整条链仍待复验。 [诊断复跑](../../tmp/ff-c14b-dam-break-g82nrc9j/dam-break.json) · [求解日志](../../tmp/ff-c14b-dam-break-g82nrc9j/files/workspaces/1/cases/1/.vibeflow/jobs/job-3.log) · [后处理租约](../../tmp/ff-c14b-dam-break-g82nrc9j/remote-stage-acquired.txt)

## T04 验收

2026-11-23，FF-C07 完成。已汇总原生不可压流动、导热、多区域传热、湍流、多相、可压缩流动和独立声学求解的实际运行与发布结果。本矩阵追加的求解与存储验证调用模型 0 次。 [T04 执行矩阵](../../tmp/FF-C07-compressible-20261123/matrix-acceptance.json)
