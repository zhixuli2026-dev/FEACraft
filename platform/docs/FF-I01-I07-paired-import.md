# FloTHERM PDML 与 XTXML 配对导入

## FF-I07 装配到求解结果的追溯

2026-11-23。生成的 `case-manifest.json` 和 `conversion-coverage.json` 对含装配或配对关联的输入发布同一份 `source_trace`。可查回对象的原始路径、ECXML 元素位置或 PDML 记录偏移、具名装配、配对依据与求解区域。对并盒和流动设备亦记录已有的单元区标识。无法解析的配对对象 ID 或重复的 PDML 偏移在 IR 验证时拒绝。CLI `check` 可继续读取此字段加入前生成的算例。

用两层 ECXML 装配生成包含金属和聚合物的独立固体区，分别施加 0.3 和 0.2 W。非特权 WSL OpenFOAM v2406 实际解出两个固体最高温度约 308.07020 K、305.24314 K，界面净热约 0.499985 W，流道出口净显热约 0.499997 W。实验通过查询原始对象路径定位到实际求解区域和温度。另一份原始 ECXML、PDML、XTXMLA 三件套同时保留装配实例、实体配对偏移和固体区域，并拒绝悬空配对引用。文件系统和认证 SeaweedFS 的实际异步转换均发布了相同的装配及对象区域关联。真实模型调用 0，模拟调用 0。[原生求解与配对](../../tmp/FF-I07-source-trace-20261123/native-first/native.json) · [文件系统](../../tmp/FF-I07-source-trace-20261123/fs-first/fs.json) · [SeaweedFS](../../tmp/FF-I07-source-trace-20261123/s3-first/s3.json)

2026-11-20。FloTHERM 支持在 CLI 和网页导入 ECXML、V2310 PDML，以及 ECXML、PDML、XTXML/XTXMLA 的配对组合。同组文件使用相同主文件名。网页先运行输入预检，再选择计算域配置和 IR 保存路径。导入时核对预检返回的每个文件的 SHA-256。生成 IR 后可提交异步转换。

```text
python platform/server/flotherm2foam.py import thermal.pdml --config domain.json --output model-ir
python platform/server/flotherm2foam.py import thermal.xtxmla --pdml thermal.pdml --ecxml thermal.ecxml --output model-ir-paired
python platform/server/flotherm2foam.py build model-ir --output thermal-case
python platform/server/flotherm2foam.py check thermal-case
```

PDML 输入缺少 ECXML 配套文件时，需显式配置计算域和环境温度。以下数值对应验收用的 0.2 m 立方实体。

```json
{"domain":{"location":[0,0,0],"size":[0.2,0.2,0.2],"ambientTemperature":310}}
```

- V2310 读取使用记录类型、边界、父节点、显式材料引用和总功率字段。保留每个对象的记录偏移与计算域坐标。三维盒体支持通过装配轴转换到全局坐标。非轴对齐实体和未支持的活动部件会得到导入诊断。
- ECXML 与 PDML 配对通过唯一名称和 SI 包围盒关联。IR 保留 PDML 原始偏移，采用 ECXML 的材料和功率。配对歧义和不同包围盒会阻断导入。
- XTXML 和 XTXMLA 保存文档引用、装配实例路径与累计刚体变换。几何坐标来自 PDML 或已配对的 ECXML。XTXML 的装配数据供交叉审计。

## 验收

| 路径 | 实际结果 |
| --- | --- |
| Windows 和非特权 WSL CLI | 两边都完成全部七种导入组合及 `validate`、`build`、`check`。二进制文件由独立夹具按已核对的 V2310 字段布局生成。累计旋转恢复全局三维盒体，六个负例覆盖记录断裂、错误引用、名称和边界冲突、缺失计算域及非法变换。 |
| Edge + 文件系统 | 实际以 HTTP 上传 PDML 二进制，浏览器预检 XTXMLA＋PDML，选择显式计算域，生成并保存 IR，再发布作业 5 的 OpenFOAM 算例。原 PDML 字节一致。 |
| 认证 SeaweedFS | 更改 XTXMLA 后，旧成套摘要返回 409。使用更新的文件摘要生成 IR、保存并发布算例，报告为 1,200 W。 |
| OpenFOAM v2406 | 从独立 PDML 导入结果实际划分 1,000 个单元，并以非特权 WSL 求解至时刻 100。与独立离散回算最大差 0.000487 K。 |

本轮模型调用 0，模拟调用 0。求解覆盖单个填满计算域的均质固体，复杂实体与多材料共轭传热继续按原计划推进。Pack/FloSCRIPT 导出仍待对接和实际认证运行，当前运行环境未找到 `flotherm.bat`。[CLI、OpenFOAM 与 S3](../../tmp/flotherm-paired-20261120.json) · [WSL CLI](../../tmp/flotherm-paired-wsl-20261120.json) · [浏览器回执](../../tmp/flotherm-studio-browser-b2wsjfit/attestation.json) · [网页结果](../../tmp/flotherm-studio-browser-b2wsjfit/paired-desktop.png) · [ECXML 导入](FF-I01-I07-ecxml-import.md)
