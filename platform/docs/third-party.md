# 第三方资产与合规核查

> 本文是工程证据台账，不构成法律意见。当前核验日期：2026-10-24。
> **使用决策（2026-10-24，用户拍板）：当前不做商业发布，PDF.js、texo/FormulaNet、OpenFOAM 等现有资产直接保留用于项目，许可核查不阻塞当前开发。以下未闭环项仅在将来对外分发、打包或提供服务时作为复核门禁。**
> 状态分为：已证实、部分闭环、未闭环。公开可访问不等于允许再分发，代码许可证也不能自动覆盖模型权重、数据集、字体或文档。

## 1. 状态总览

| 资产 | 当前状态 | 已取得的证据 | 仍需处理 |
|---|---|---|---|
| PDF.js 2.16.105 | 部分闭环 | 版本、官方 release/归档哈希、Apache 全文、CMap/字体对照、core-js MIT 文本 | minified 转换记录、上游 NOTICE、最终发布清单 |
| Transformers.js 2.17.2 | 运行时已核 | 精确 tag/commit、Apache-2.0 全文已随 texo 携带 | bundle 传递组件和 Qt 转换记录 |
| ONNX Runtime Web 1.14.0 | 运行时已核 | 精确版本、MIT 全文已随 texo 携带 | WASM 构建及传递组件清单 |
| Texo / Texo-web | 高风险、未闭环 | 两仓库均声明 AGPL-3.0；commit 与全文已取得 | 实际源 commit、AGPL 义务、转换链、源代码提供方式 |
| FormulaNet 权重及训练上游 | **未闭环** | 当前 ONNX 哈希、安装包来源和模型声明 | 权重许可、模型卡、PPFormulaNet/UniMERNet/PaddleOCR 等上游条款 |
| OpenFOAM 补全索引 | 部分闭环 | tag/commit、归档 SHA-256、COPYING、当前/重建索引差异 | 原始提取方式、大小写/链接规范化、可重复构建与索引许可分析 |
| npm 依赖 | 可作 SBOM 种子 | lockfile 含 118 个非根条目的 version/resolved/integrity/license；Monaco 嵌套 DOMPurify 已由 override 统一至 3.4.17，`npm audit --omit=dev`=0 | 过滤 dev/optional/平台，收集完整文本，审查 bundle 资产；升级时复查嵌套覆盖 |
| Python 依赖 | 未锁定 | requirements 有 24 个直接需求项 | 生成目标环境精确锁文件/哈希及传递依赖文本 |
| Docker/基础设施 | 未闭环 | Dockerfile/compose 声明和离线测试 | 镜像 digest、OS/apt/pip/npm/原生库 SBOM、实际镜像内容 |

**当前不能写“第三方许可证已核验完成”。特别是 FormulaNet 权重未闭环前，不应把包含权重的镜像或静态包对外发布。**

## 2. PDF.js

已证实：

- 当前版本为 **2.16.105，build 172ccdbe5**；不是同包 core-js 的版本。
- 官方 release：`https://github.com/mozilla/pdf.js/releases/tag/v2.16.105`。
- 官方归档 SHA-256：
  - 标准包：`47abfa0dd7574333e801c1dc159340e0d22956edcd6d7293ed5fee54f0e07e0b`。
  - legacy 包：`4fce357e8c665a25d44498f466ad93829f672f782eb95d6db8f20ee479dd6af4`。
- 当前 CMap 的 169 个对应文件与官方包一致；CMap/Foxit/Liberation 文本一致，部分历史文件只有换行符差异。
- [Apache-2.0 全文](../web/public/pdfjs/LICENSE) 和 [core-js MIT 文本](../web/public/pdfjs/LICENSE-CORE-JS.txt) 已放入发布目录。
- [PROVENANCE.md](../web/public/pdfjs/PROVENANCE.md) 随资产携带。

未闭环：

- 官方 zip 是未压缩 `pdf.js`/`pdf.worker.js`，当前为 minified 文件，不能声称是与官方 zip 逐字节一致的原件；需补压缩/兼容转换记录。
- 上游 NOTICE 和最终接收者应得到的完整文本清单仍需核对。
- 与原安装包提取资产一致只证明安装包来源链，不证明已与 Mozilla 上游一致。

## 3. Texo / FormulaNet

### 已证实的运行时与上游声明

- Transformers.js **2.17.2**，tag commit `da2688626d7812ad1ea47fd304c2072cc685051b`，Apache-2.0：[许可证](../web/public/texo/LICENSE-TRANSFORMERS.JS.txt)。
- ONNX Runtime Web **1.14.0**，MIT：[许可证](../web/public/texo/LICENSE-ONNXRUNTIME.txt)。
- `alephpi/Texo-web` 在 GitHub 元数据和 README 中声明 **AGPL-3.0**；审计时 master 为 `4cff3bcff82846b31d7c1207d883e1b29d2b7db0`：[许可证](../web/public/texo/LICENSE-TEXO-WEB.txt)。
- `alephpi/Texo` 的 GitHub 元数据也声明 **AGPL-3.0**；审计时 main 为 `7773a3ef26cae12106bc724d800fd931c5fca903`：[代码许可证](../web/public/texo/LICENSE-TEXO-MODEL.txt)。
- 原安装包 README 明示 Qt bundle 兼容转换和 decoder ONNX IR 9→8；不能把当前文件标成未修改上游原件。
- 两份当前 ONNX 的 SHA-256 分别为：
  - encoder：`95cccef463e5ed3623282f1541c0011a00b8a5d0828ea2cd57d6953ad4310b5b`。
  - decoder：`84742898ca7374b61846373b9969e9782ea6a42c009cd46b3218ee940ba8fb30`。
- [PROVENANCE.md](../web/public/texo/PROVENANCE.md) 随资产携带。

### 未闭环

- Hugging Face `alephpi/FormulaNet` 当前网络不可达，尚未取得模型卡、权重专属许可或其他再分发依据。
- 代码仓库的 AGPL-3.0 不自动证明权重及训练上游可再分发；AGPL 的代码/网络使用义务也需专项评审。
- 模型 README 涉及 PPFormulaNet-S、UniMERNet、PaddleOCR 预训练等资产；这些模型、数据集、代码和文档需分别核查。
- 实际源 commit、上游修改对照和完整转换清单仍未建立。

因此 UI 的 About 页已改为“逐项核验中”，不再使用“均为官方渠道开源组件”等概括性表述。

## 4. OpenFOAM 补全索引

已证实：

- 上游 tag `version-14` 对应 annotated tag object `86db2c5c57c47fec51f40a9fb83895ae43eee13e`，commit 为 `c046c72b858174c93374423dc98ea84c05d62830`。
- 审计归档 `https://codeload.github.com/OpenFOAM/OpenFOAM-dev/tar.gz/version-14` 的 SHA-256 为 `e088c506600831bed01d28d579a9950907bfa9c8fcb10ee136167e1c895744e4`。
- 当前索引为 8692 项，SHA-256 `ed710889be061acc69d701fc180fdc285767bd189540713a2c3d196c648c5d13`。
- [源 COPYING](../server/data/completion/OPENFOAM-SOURCE-COPYING.txt) 和 [PROVENANCE.md](../server/data/completion/PROVENANCE.md) 已随索引保留。

重建与未闭环：

- 当前生成器在审计归档上重建得到 8693 项，排除 `generated` 后语义仍不相等；重建 SHA-256 `5f809bbb80c3453bd9cf6b5721a8b292ab4a688e016939fa54f10b9aa898f9c9`。
- 差异主要涉及大小写文件名变体、`fvcDDt.H`/`fvcDdt.H` 和符号链接提取方式。
- 因而不能把当前 `source` 字符串当作完整 provenance，也不能承诺跨平台逐字节重建。需固定归档、提取器、生成器版本和规范化策略。
- 不把“事实性数据”注释直接等同于无版权、GPL 或免声明结论；索引生成物的义务需单独分析。
- 沙箱 apt OpenFOAM 与索引 version-14 是否一致没有证据，部署清单必须分开记录。

## 5. CalculiX 2.23

- 来源：CalculiX 作者官方站点 `https://www.dhondt.de/` 的 `calculix_2.23_4win.zip`（Windows ccx/cgx 可执行文件）。
- 归档 SHA-256：`87fc4e1721df37370ec67d0a244bb14f9bf804b0a95df49e2ae7369d30ec72e8`。
- 本地使用路径：`third_party/calculix-2.23-bin/calculix_2.23_4win/ccx_static.exe`；实际执行显示 `This is Version 2.23`。
- 许可：官方页面提供 GNU GPL v2 文本 `https://www.dhondt.de/gpl-2.0.txt`；当前仅在本机非商业环境运行，发布镜像前需补 ccx 及 SPOOLES/ARPACK 等传递组件、对应源码提供方式和接收者清单。


## 6. SU2 8.5.0

- 来源：SU2 官方 GitHub Releases 的 Windows x64 OpenMP 发行包；本地归档 `third_party/su2-8.5.0-omp/su2.zip`。
- 归档 SHA-256：`4466fe21aedb5e0bad57afd45f829acbdec6ec79fe8c3f8954ddea06a4b4bc11`；实际运行路径 `third_party/su2-8.5.0-omp-runtime2/bin/SU2_CFD.exe`。
- Golden 来源：官方仓库 `QuickStart/inv_NACA0012.cfg`（SHA-256 `8a66537ad519e2acecaf368249d17f53cc683d96`）和 `QuickStart/mesh_NACA0012_inv.su2`（SHA-256 `0c809e5807ecc404a2ef69608b7aa4a17a992162`），保存在 `server/examples/su2/`，测试只把 `ITER` 从 250 缩为 3。
- 许可：SU2 采用 GNU LGPL v2.1；当前仅本机非商业运行，对外分发前仍需随包保留许可证、版权/修改说明及对应源码提供方式，并核对原生数学库的传递组件。


## 7. ParaView 5.11.2

- 来源：当前项目已提取的原版运行资产 `extracted/bin/pvpython.exe` 与 `pvbatch.exe`；实际执行均报告 `paraview version 5.11.2`。
- 验证范围：通过 `paraview.simple` server-manager 创建 Sphere、VTP 和 RenderView PNG；未把 pip VTK 当作 ParaView 兼容证据。
- 许可：ParaView 使用 BSD 3-Clause 许可证；对外再分发这些二进制时需保留许可证/版权声明并核对随附 Qt、VTK、Python 和原生库组件。


## 8. Redis 7.0.15 与 arq

- 来源：Ubuntu Noble 官方 `redis-tools`、`redis-server`、`liblzf1`、`libjemalloc2` 包，仅解包到 `third_party/redis-runtime`，未安装 WSL 系统包。
- SHA-256：redis-tools `6e5c009ce7761d0a956dafcfcf054ddeecc7a51efcc38215f26bb5794a4e93e1`；redis-server `5f41607fb77674078c013b2a6196a748c3a5f6ec4bd3201cd08478dce3e26d05`；liblzf1 `253182d4718271565d6c24125f27dd93da56fce34b34d8226615cb3553bbe73f`；libjemalloc2 `0cccf7701765b5a633b964ed551c2bd1c074cad17daea8fa20a4e9cad0adb5a8`。
- 许可：Redis 为 BSD-3-Clause，jemalloc 为 BSD-2-Clause，LZF 为 BSD-2-Clause；分发时保留对应 LICENSE/copyright。Python `arq` 为 MIT。


## 9. SeaweedFS 3.99

- 来源：SeaweedFS 官方 GitHub Release `linux_amd64.tar.gz`，仅解包到 `third_party/seaweed-runtime`。
- 归档 SHA-256：`9bedd8b11527162a31fd1067b01cc3a0ea081379104c41b3802f92dbd4303508`；实际执行 `weed version` 为 3.99。
- 许可：Apache License 2.0；分发时保留 LICENSE/NOTICE 及传递组件声明。


## 10. 依赖与镜像

- `web/package-lock.json` 可作为 npm SBOM 种子：118 个非根条目有版本、registry、integrity 和 license 字段；这些字段仍需与实际 LICENSE/NOTICE 对照。
- `server/requirements.txt` 有 24 个直接需求项，但 18 个仅为 `>=`、6 个无版本约束、0 个精确 `==` 和 0 个哈希，**不是可复现锁文件**。
- API/Web/Sandbox 基础镜像只有 tag，未固定 digest；最终镜像必须单独生成 OS、apt、pip/npm、原生库和静态资产 BOM。
- dev compose 使用 SeaweedFS 4.48，prod 使用 3.99，发布时必须分别记录实际部署 BOM。
- `platform/.dockerignore` 已排除秘密、用户数据和大型本地产物；API 镜像已声明携带补全索引，`infra/compose_smoke.py` 五项离线检查通过。
- Docker 引擎仍不可用，最终镜像内的实际文件、许可证和运行行为尚未验证。

## 11. 原版提取资产

`extracted/`、manifest 和 `REPORT.md` 用于静态分析与机制对照：

- 原版 `CLAUDE.md`、`AGENTS.md`、技能、`vibeflow_geo` 和其他厂商资产只读参考，重写不照抄。
- 不要把整个提取目录、原版二进制、安装包或品牌资产混入 Web 发布包。
- 当前 `platform/server/skills/` 是 41 个自有技能文本；文本覆盖不表示对应求解器可随平台捆绑。
- 商业求解器名称仅用于兼容说明，组织需自配 license server 或 remote/HPC；不表示已获软件授权。

## 12. 对外分发前检查清单

- [ ] FormulaNet 权重及训练上游取得明确再分发依据；否则从发布物移除相关资产。
- [ ] Texo/Texo-web 的实际来源、AGPL 义务、修改记录和源代码提供方式通过专项评审。
- [ ] PDF.js minified 转换、NOTICE 和所有内嵌组件文本闭环。
- [ ] OpenFOAM 索引可重复构建，并记录索引生成物的许可分析。
- [ ] 生成精确资产清单：路径、组件/模型名、版本、上游 URL、commit/revision、SHA-256、直接来源、转换记录、许可证原文、NOTICE、核验状态和日期。
- [ ] 生成每个最终镜像的 SBOM，覆盖基础 OS、apt、pip/npm、原生库和静态资产。
- [ ] 所有要求的 LICENSE/NOTICE 实际进入发布物；About/第三方声明与清单一致。
- [ ] copyleft/ reciprocal 组件的源码提供、修改说明和链接方式经过审查。
- [ ] 商业求解器、外部 LLM、公共文献 API、用户数据和模型提示词的服务条款与隐私责任已评审。

当前按用户决策直接保留并使用这些资产，继续功能开发；若未来需要对外分发，优先复核顺序为：**FormulaNet 权重 → Texo AGPL 义务 → 最终镜像 SBOM → 索引可重复性**。其余文档记录不能替代届时所需的核查。
