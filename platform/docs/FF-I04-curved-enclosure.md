# FF-I04 曲面加热器与密闭外壳

2026-11-23。ECXML 原始输入包含密闭外壳和圆柱加热器，分别采用两种具有明确热物性的材料。生成器为外部流体和每个独立空腔选择内部种子，按包容关系生成流固接面。WSL OpenFOAM v2406 实际运行 `snappyHexMesh`、`splitMeshRegions` 和 `checkMesh`，四个区域均通过。圆柱的网格体积约 0.000156107 m³，解析值约 0.000157080 m³。实际接面为圆柱到空腔、空腔到外壳、外壳到外部流体。外壳与圆柱相交的输入在预检拒绝。[原生网格与求解](../../tmp/FF-I04-curved-enclosure-20261123/first/native.json)

| 非特权 WSL 求解到第 280 步 | 功率 |
| --- | --- |
| 原始加热器输入 | 0.06000000 W |
| 外壳到外部流体的界面净热 | 0.05999999 W |
| 进出口净显热 | 0.05822340 W |
| 进出口净动能输运 | 0.00168485 W |
| 显热与动能合计 | 0.05990824 W |

CLI `flow --energy-balance --include-kinetic` 从实际 `U`、`phi` 和面数核对进出口动能输运，报告保留显热和动能两项，并绑定原生场摘要。上述算例的总输运与壁面净热差约 0.00009175 W。默认的 `--energy-balance` 继续按显热核对。[动能对账](../../tmp/FF-I04-curved-enclosure-20261123/combined-native-energy.json)

文件系统与认证 SeaweedFS 的转换产物都可下载，两个 STL 和网格字典与实际求解的输入逐字节一致。先前的凸棱柱、四面体、薄板、PCB、2R、风扇、流阻和两种散热器的 OpenFOAM 结果纳入同一 T09 证据。真实模型调用 0，模拟调用 0。[文件系统发布](../../tmp/FF-I04-curved-enclosure-20261123/fs-publication.json) · [认证 SeaweedFS 发布](../../tmp/FF-I04-curved-enclosure-20261123/s3-publication.json) · [T09 核对](../../tmp/FF-I04-curved-enclosure-20261123/t09-validation.json)
