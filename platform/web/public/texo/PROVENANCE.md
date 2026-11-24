# Texo / FormulaNet 资产来源与核验状态

核验日期：2026-10-24。

## 运行时证据

- Transformers.js：**2.17.2**，上游 tag commit `da2688626d7812ad1ea47fd304c2072cc685051b`，Apache-2.0；全文见 `LICENSE-TRANSFORMERS.JS.txt`。
- ONNX Runtime Web：**1.14.0**，MIT；全文见 `LICENSE-ONNXRUNTIME.txt`。
- 原版 README 明示 bundle 经 Qt 兼容转换，不能标成“未修改的上游原件”。

## 上游代码与模型声明

- `alephpi/Texo-web`：GitHub 元数据和 README 声明 **AGPL-3.0**；审计时 master 为 `4cff3bcff82846b31d7c1207d883e1b29d2b7db0`。全文见 `LICENSE-TEXO-WEB.txt`。
- `alephpi/Texo`：GitHub 元数据声明 **AGPL-3.0**；审计时 main 为 `7773a3ef26cae12106bc724d800fd931c5fca903`。代码许可证全文见 `LICENSE-TEXO-MODEL.txt`。
- 原安装包 README 声明模型为 `alephpi/FormulaNet`、参考实现为 `alephpi/Texo-web`，并记录 decoder ONNX IR 9→8 转换。实际源 commit 和完整转换清单仍未建立。
- Texo 模型 README 还声明蒸馏/训练涉及 PPFormulaNet-S、UniMERNet 和 PaddleOCR 预训练资产；这些上游模型、数据集和代码不能由本目录任一许可证替代核查。

## 当前权重身份

- `model/onnx/encoder_model.onnx`：54,168,533 字节，SHA-256 `95cccef463e5ed3623282f1541c0011a00b8a5d0828ea2cd57d6953ad4310b5b`。
- `model/onnx/decoder_model_merged.onnx`：25,946,296 字节，SHA-256 `84742898ca7374b61846373b9969e9782ea6a42c009cd46b3218ee940ba8fb30`。

## 未闭环 / 发布限制

- Hugging Face `alephpi/FormulaNet` 在当前网络不可达，尚未取得模型卡、权重专属许可证、NOTICE 或其他再分发依据。
- 代码仓库的 AGPL-3.0 不能自动证明权重及其训练上游均可再分发；AGPL 的代码/网络使用义务也需单独评审。
- **在权重及训练上游许可证据闭环前，不应将包含这些权重的镜像或静态包对外发布。**
