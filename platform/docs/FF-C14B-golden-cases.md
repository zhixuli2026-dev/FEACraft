# FF-C14B 原生黄金算例和参考对照

数值验证绑定实际运行的网格、原生场、独立参考和容差。风洞实验、异网格参考及报告使用相同的版本化后处理契约。 [参考场和风洞](FF-CB12-reference-grid.md)

## PitzDaily 壁面压力力

OpenFOAM 2406 `simpleFoam` 运行原教程的 12,225 单元网格并通过完整 `checkMesh`。两侧壁面使用原生 `forces`，空气参考密度为 1.2 kg/m³。选定 200 和 300 步，后处理分别用 `surfaceIntegrals` 的 `pressureForce` 对 `upperWall` 和 `lowerWall` 积分，再用公式合并各方向。初次完整运行的独立和托管作业分别提前收敛在 281 和 279 步。固定比较于第 300 步后，两套存储完成 E 并生成正式报告。 [原教程](https://www.openfoam.com/documentation/tutorial-guide/3-compressible-flow/3.1-steady-turbulent-flow-over-a-backward-facing-step) · [配置含义](FF-D11-field-extraction.md#数值和来源) · [首次托管记录](../../tmp/ff-c14b-pitz-force-zp252yzw/pitz-force.json)

托管压力力为 `(4.027720128 × 10⁻⁴, 8.934647210 × 10⁻⁵, 0) N`，与同次原生 `forces` 压力项的相对 L2 为 `2.6445 × 10⁻⁸`。水平和竖向分量与独立原生求解分别相差约 0.0436% 和 0.1266%，低于预设的 0.5%。负参考密度使后处理作业失败，原成功版本仍可读取。额外用激波管真实 Pa 字段核对有向力，参考压力 0 Pa 或 3000 Pa 都得到解析合力 `(-360000, 0, 0) N`，同时提供密度时拒绝该 Pa 配置。 [独立 `forces` 原件](../../tmp/FF-C14B-pitz-forces-20261123/fixed-iteration/pitz-force-native.json) · [文件系统](../../tmp/ff-c14b-pitz-force-aql15v27/pitz-force.json) · [认证对象存储](../../tmp/ff-c14b-pitz-force-5wd_uv1i/pitz-force.json) · [Pa 复验](../../tmp/FF-C14B-pitz-forces-20261123/pressure-force-Pa.json)

Edge 以键盘打开实际历史作业的后处理证据，按所选版本读取上下壁「压力合力」和 N 单位。1440、900 和 640 像素宽度下通过横向显示核对。前端构建前后分别保存 Windows C 盘和 Docker 指标。 [Edge](../../tmp/cb08-h18-edge-pressure-6p1c2mo5/browser.json) · [构建前后磁盘](../../tmp/FF-C14B-pitz-forces-20261123/web-build-disk.json)

## 溃坝动画

溃坝原生水相体积分、压力修正和两套存储的实际报告记录在求解矩阵。以原生 `alpha.water` 给 2268 个单元染色，Edge 依次读取 0、0.25、0.5、0.75 和 1 s。初次验收发现后台播放循环未提交 trame 状态，修复后自动播放能推进时间，暂停后时刻保持，并能逐帧导出五份不同摘要的 PNG。通过 UI 关闭会话并核对实际作用域已退出。 [两相流证据](FF-C07-openfoam-matrix.md#溃坝两相流) · [Edge 播放与导帧回执](../../tmp/cb08-h18-edge-dam-lvknbiim/browser.json)

## 方腔 UDF 压力均值

方腔 `functionObject` 在 `write()` 中从 400 个原生单元及其体积计算运动学压力均值。使用标准 `fvCFD.H` 时，首轮 wmake 发现搭建模板缺少 `meshTools`。补全后真实编译、动态加载并随 `icoFoam` 执行。六位输出导出的场均值和原生内存相差约 `2.08 × 10⁻⁸ m²/s²`，超过首轮高精度复算阈值。将复验写出精度固定为 12 位后，SQLite 文件系统确认 E，回调和独立读取同次原生场相差 `7.17 × 10⁻¹⁴ m²/s²`，与 VTK 的差为 `9.00 × 10⁻⁹ m²/s²`。独立存档方腔作为物理解回归参考。 [运行源代码](../../tmp/FF-C14B-udf-pressure-20261123/myFunctionObject.C) · [文件系统](../../tmp/ff-c14b-udf-pressure-kdfqpj2_/udf-pressure.json)

PostgreSQL 加认证 SeaweedFS 也完成了真实编译、动态加载、原生执行与 E 确认，末态压力文件逐字节一致。C 盘在该复验阶段减少 270,749,696 B 可用空间，Docker 回收 0 B。 [认证对象存储](../../tmp/ff-c14b-udf-pressure-35v92x01/udf-pressure.json) · [独立数据库清理](../../tmp/terminal-agent-postgres-l5p6vi24/cleanup.json) · [磁盘记录](../../tmp/FF-C14B-udf-pressure-20261123/workspace-disk.json)

## FloTHERM 转换基准

沿用已归档的 G6 原生证据。提取版和平台版从同一 ECXML 装配转换后实际求解并逐文件核对 82 条路径。两版均映射 `0.4 W` 热源。在时刻 2000，提取版默认能量设置只得到 `0.290798 W` 的界面净热。提取版启用严格能量求解并设定松弛 0.95 后得到 `0.3999897085 W`，与平台版相差 `0.0000102915 W`。七种离线输入组合另保存了两版共 14 次原生求解与文件摘要。 [逐文件与原生求解](FF-I05-thermal-balance.md#g6-原版转换对拍) · [能量设置复验](../../tmp/flotherm-g6-reference-20261120/reference-tuning.json) · [七组输入回执](../../tmp/flotherm-g6-reference-20261120/matrix/native-matrix.json)

## T05 验收

2026-11-23，FF-C14B 的独立参考、误差及实际黄金算例按 T05 通过。验收汇总包含实测风洞、异网格、方腔、壁面力、溃坝、几何、方腔 UDF 和 FloTHERM 转换。20 时刻溃坝对象存储的 D 确认问题仍保留在求解矩阵。其余 UDF 类型归 FF-A10 和 FF-C10。此次追加的数值与图形验收调用模型 0 次，风洞和异网格各有此前独立的真实模型回读。 [T05 汇总](../../tmp/FF-C14B-udf-pressure-20261123/golden-validation.json) · [风洞与异网格](FF-CB12-reference-grid.md)
