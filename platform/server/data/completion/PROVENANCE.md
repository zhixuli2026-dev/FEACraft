# OpenFOAM 补全索引来源与可重复性

## 当前发布索引（2026-11-19）

`openfoam-keywords.json` 使用 OpenCFD 官方 v2406 源码归档，保留既有基准中精确的 31,542 个标签和全部分类。归档 SHA-256 为 `8d1450fb89eec1e7cecc55c3bb7bc486ccbf63d069379d1d5d7518fa16a4686a`，MD5 与官方发布记录一致。在 Linux 上于不同 Python 哈希种子下独立生成两次，排除生成时间后的索引 SHA-256 都为 `e30fe9c327b980e76f9939b1c118e956ee4b4ec0d5ff65d2751ad14dd9e6fa20`。[OpenCFD 发布记录](https://www.openfoam.com/download/release-history) · [官方归档](https://dl.openfoam.com/source/v2406/OpenFOAM-v2406.tgz) · [本次核验证据](../../../../tmp/openfoam-completion-20261119/index-verification.json)

扫描按源码相对路径排序，跳过 `lnInclude` 和链接。教程 gzip 先有界解压，再验证文本和字典格式。每个值按所属字典键和文件名记录一个源例，API 可以定位确切的源码文件。实际归档扫描得到 1,449 个源码注册、13 个非空标签分类、275 个复合键、18 种 FoamFile 类和 428 个命令行标记。旧统计的 1,446 条额外注册逐条与 `lnInclude` 重复路径配对。[构建和验收说明](../../../docs/FF-H12-H15-foam-completion-acceptance.md)

## 2026-10-24 历史索引

- 当时文件为 `openfoam-keywords.json`。
- 当前 SHA-256：`ed710889be061acc69d701fc180fdc285767bd189540713a2c3d196c648c5d13`。
- 当前统计：8692 项；生成器声明来源为 OpenFOAM-dev version-14。
- `source` 是生成器固定写入的来源字符串，不是独立 provenance 证明。

## 本次独立取得的上游证据

- 仓库：`https://github.com/OpenFOAM/OpenFOAM-dev`。
- annotated tag：`version-14`，tag object `86db2c5c57c47fec51f40a9fb83895ae43eee13e`。
- 对应 commit：`c046c72b858174c93374423dc98ea84c05d62830`。
- 审计归档：`https://codeload.github.com/OpenFOAM/OpenFOAM-dev/tar.gz/version-14`。
- 归档 SHA-256：`e088c506600831bed01d28d579a9950907bfa9c8fcb10ee136167e1c895744e4`。
- 对应源许可证文本保留为 `OPENFOAM-SOURCE-COPYING.txt`；这不等于对索引生成物作了许可证法律结论。

## 重建结果与未闭环项

在上述归档的隔离提取目录中运行当前 `app/services/completion_builder.py`，未覆盖现有索引：

- 当前索引：1321 个字典文件、3846 个头文件、8692 个补全项。
- 本次重建：1304 个字典文件、3847 个头文件、8693 个补全项。
- 重建文件 SHA-256：`5f809bbb80c3453bd9cf6b5721a8b292ab4a688e016939fa54f10b9aa898f9c9`。
- 排除 `generated` 时间字段后，语义比较仍不相等。

差异主要涉及同名大小写变体、`fvcDDt.H`/`fvcDdt.H` 和归档符号链接处理。由此只能确认上游 tag/commit 和本次重建结果，**不能声称当前索引的原始输入、提取工具或跨平台输出已完全复现**。后续应固定归档、提取方式、生成器版本和大小写/链接规范化策略，再生成可重复的发布索引。
