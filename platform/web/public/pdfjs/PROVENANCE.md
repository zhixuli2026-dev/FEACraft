# PDF.js 资产来源与核验状态

核验日期：2026-10-24。

## 已确认

- 当前运行文件标识：PDF.js **2.16.105**，build `172ccdbe5`。
- 官方 release：`https://github.com/mozilla/pdf.js/releases/tag/v2.16.105`。
- 官方归档：
  - `pdfjs-2.16.105-dist.zip`：SHA-256 `47abfa0dd7574333e801c1dc159340e0d22956edcd6d7293ed5fee54f0e07e0b`。
  - `pdfjs-2.16.105-legacy-dist.zip`：SHA-256 `4fce357e8c665a25d44498f466ad93829f672f782eb95d6db8f20ee479dd6af4`。
- 当前 CMap 文件与官方 release 的 169 个对应文件一致；CMap/foxit/Liberation 许可证文本一致（部分历史文件仅换行符不同）。
- 本目录 `LICENSE` 为 Apache-2.0 全文；`LICENSE-CORE-JS.txt` 为 core-js 3.24.1 的 MIT 文本。
- 当前共同文件与原 VibeFlow 安装包提取资产一致；这只是安装包来源链，不等于上游未修改证明。

## 未闭环

- 官方 release zip 提供未压缩 `pdf.js`/`pdf.worker.js`，本目录为 `pdf.min.js`/`pdf.worker.min.js`。因此当前 minified 文件不是可直接与官方 zip 逐字节比较的原件，仍需记录具体压缩/兼容转换工具和步骤。
- 上游 NOTICE 是否存在于对应分发包及是否需保留，仍需逐项核对；当前不把“未发现 NOTICE”写成“上游无 NOTICE”。
- 发布时应同时保留 PDF.js、内嵌 core-js、CMap、Foxit 和 Liberation 的相应文本，而不是只带 Apache 全文。
