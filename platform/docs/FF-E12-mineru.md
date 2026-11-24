# MinerU ZIP 导入

将 MinerU 导出的 ZIP 上传到算例，在 `caseDict` 中展开「导入 MinerU 文档」。单份 Markdown 直接导入，包含多份 Markdown 时填写 ZIP 中的文件名。页面打开生成的 `E-report/mineru/<导入标识>/document.md`。本地图像写到同目录 `assets/`，随 Markdown 预览从当前算例鉴权读取。`import.json` 记录来源 ZIP、Markdown 成员和图像的哈希。

解析使用有界的 ZIP 成员、总大小和压缩比检查，拒绝越界路径、链接、加密成员、重复路径和文件与目录冲突。文本使用 UTF-8。Markdown 的本地行内图片、图片引用定义与 HTML 图片会改写为发布后的相对路径。资源和 Markdown 通过算例原子写入发布。重复导入时核对已有资产，用户修改后的文件保留。 [解析器](../server/app/services/mineru_bundle.py) · [发布入口](../server/app/routers/mineru.py) · [MinerU 导出格式](https://opendatalab.github.io/MinerU/reference/output_files/)

2026-11-24 用实际 MinerU Kit 4.2.3 在禁网 Linux 容器中解析带内嵌图像的 HTML 并导出 ZIP。Edge 导入原始 ZIP，读到 Markdown 标题和 132 像素宽的真实图像，重复导入返回相同路径与 SHA-256。另以独立协议夹具检查越界 ZIP 拒绝，并验证未登录响应。Windows 运行因 ONNX Runtime DLL 初始化失败转用 Linux。文件类型识别在工具本地执行，实际 LLM 调用 0 次，模拟 LLM 调用 0 次。 [真实 ZIP](../../tmp/FF-E12-mineru-20261123/actual-mineru.zip) · [工具日志](../../tmp/FF-E12-mineru-20261123/linux-parse.log) · [实际浏览器验收](../../tmp/ff-e12-mineru-edge-9fz3qkfh/mineru-acceptance.json) · [预览](../../tmp/ff-e12-mineru-edge-9fz3qkfh/mineru-preview.png)
