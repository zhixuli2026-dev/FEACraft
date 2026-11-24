# ECXML 原始模型导入与求解验收

2026-11-20。FloTHERM Studio 的文件预检支持选择单个 ECXML 后生成算例中的 IR。界面预检返回的 SHA-256 随导入提交。如果原文件变化，服务端返回 409，用户重新预检。IR 经已有的条件保存入口写入新路径，保存成功后自动选作转换输入。

```text
python platform/server/flotherm2foam.py import model.ecxml --output model-ir
python platform/server/flotherm2foam.py validate model-ir
python platform/server/flotherm2foam.py build model-ir --output openfoam-case
python platform/server/flotherm2foam.py check openfoam-case
```

导入支持带命名空间的 `neutralXML`、显式计算域、环境温度、装配层级、各向同性材料和活动三维盒体。具有明确材料的 `sourceBlock` 也按三维盒体导入。IR 保留原文件 SHA-256、对象路径、计算域和热参数。缺失材料、无效计算域及无法解析的活动部件返回可定位错误。当前 OpenFOAM 求解覆盖填满计算域、外壁恒定温度的单一均质固体。存在空气或多区域时，生成的报告含求解阻断项。现有 IR 的生成契约继续兼容历史输入。

## 实际结果

| 范围 | 验收结果 |
| --- | --- |
| Edge 和本地文件存储 | 使用原始 `Ref/thermal.ecxml` 完成预检、导入、条件保存和转换。作业 4 发布 `preflight.json` 及算例文件，保留原始文件。 |
| 认证 SeaweedFS | 原文件变更后拒绝旧 SHA-256，使用实际新文件导入。第二次同路径保存返回 409。异步作业生成报告，正确读回 1,200 W。 |
| Windows CLI 和非特权 WSL | 两端均用独立编写的带装配 ECXML 热算例运行 `import`、`validate`、`build`、`check`。实际 OpenFOAM v2406 在非特权 WSL 完成 `blockMesh`、`topoSet`、`checkMesh` 和 `laplacianFoam`。 |
| 热量和场结果 | 用 160 W/(m·K)、2,000 kg/m³、500 J/(kg·K) 计算热扩散率 0.00016 m²/s。1,000 个网格单元计算到时刻 100，内部温度 310.090–312.077 K，与独立离散回算的最大绝对差 0.000487 K。 |

功率从总瓦数和体积热容换算，`absolute` 由 OpenFOAM 按实际选区体积分配。本轮模型调用 0，模拟调用 0。[OpenFOAM 源项文档](https://api.openfoam.com/2312/classFoam_1_1fv_1_1SemiImplicitSource.html) · [浏览器证据](../../tmp/flotherm-studio-browser-9_gs3xs7/attestation.json) · [桌面结果](../../tmp/flotherm-studio-browser-9_gs3xs7/import-desktop.png) · [CLI、OpenFOAM 与 S3](../../tmp/flotherm-neutral-ecxml-20261120.json) · [WSL CLI](../../tmp/flotherm-neutral-ecxml-wsl-20261120.json) · [验收入口](../server/flotherm_ecxml_acceptance.py)

后续已验收 PDML 三维实体与 XTXML 配对。FF-I01、FF-I04、FF-I07、FF-I08 的复杂几何、多材料共轭传热和 Pack/FloSCRIPT 继续交付。Pack 导出需要可运行的商业 FloTHERM 环境。[PDML 与 XTXML 配对验收](FF-I01-I07-paired-import.md)
