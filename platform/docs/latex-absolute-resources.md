# LaTeX 绝对路径输入冻结

FF-E04 已完成取消回收与 API 冷启动复验。下面保留绝对路径输入的阶段证据。[取消和冷恢复验收](latex-compile-cleanup.md)

`POST /api/v1/cases/{id}/latex:build` 接受 `input_aliases`。键为文档引用的绝对 POSIX 文件名，值为当前算例中的文件。将外部资料上传到算例后，可让原文档沿用引用路径。宏展开、Lua 拼接和 biber 使用相同的绑定。

```json
{
  "main": "E-report/latex/main.tex",
  "recipe": "pdflatex",
  "input_aliases": {
    "/home/document-author/vendor/section.tex": "Ref/vendor-section.inc"
  }
}
```

每次请求允许最多 32 个绑定，绑定表最多 8 KiB。源文件与 `resources` 共用既有输入限额和所有权检查。绑定别名必须为规范路径，系统目录、祖先文件冲突和算例外源路径会被拒绝。绑定随作业、幂等键、冻结文件摘要和发布代次持久保存。

每次有绑定的编译在私有 Linux 挂载空间中将冻结文件以只读方式挂到约定路径。主编译和发布前独立复编译均使用同一份冻结输入，并分别记录实际读取的别名及字节摘要。发布保存实际读取的算例源文件。若源文件随后修改，历史 PDF 和源文件仍可按发布 ID 读取，当前结果标为过期。SyncTeX 的绝对路径映射回算例文件。无绑定的请求继续使用既有 JSON 参数和输入证据格式。

## 实测

- 本机 SQLite 与文件系统使用两个独立 API 进程。实际 WSL pdfLaTeX 解析宏生成的绝对路径，LuaLaTeX 通过 `io.open` 打开动态拼接的路径。两个版本都完成独立复编译并通过 PDF 文本核验。另验证相同键重放、修改绑定后冲突、跨账号 404、旧 PDF 与源文件逐字节保留。[文件系统回执](../../tmp/FF-E04-absolute-resources-20261122/fs.json)
- 认证 SeaweedFS 复跑通过两次发布及跨进程历史回读。真实 SyncTeX 从 PDF 定位到 `Ref/vendor-section.inc` 第 1 行。第二个 API 在观察到实际编译打开该文件后取消作业，停止凭据确认进程范围为空，旧 PDF 保持原字节。[认证 S3 回执](../../tmp/FF-E04-absolute-resources-20261122/s3.json)
- 复用 Linux TeX 镜像 `vibeflow/api:linux-tex-v6-20261118`，挂载本次实际原生编译器代码。三个 recipe 均实际生成 PDF，XeLaTeX 用 biber 读取已绑定的绝对 `.bib` 路径并写出参考文献。三个编译都核对了实际读取源字节的 SHA-256，容器外未创建该引用路径。[Linux 回执](../../tmp/FF-E04-absolute-resources-20261122/linux.json) · [原生编译日志](../../tmp/FF-E04-absolute-resources-20261122/linux-biber.log)
- 真实 pdfLaTeX 持续编译至原 120 秒期限。包含准备和停止的测量为 121.261 秒，退出码 124。文件访问记录确认此前已读到绑定输入。[期限回执](../../tmp/FF-E04-absolute-resources-20261122/timeout.json)
- Edge 双击编译只提交一次，实际 pdfLaTeX 成功发布。旧请求的历史回读、过期保护、并发保存和期限后重试均通过。[Edge 回执](../../tmp/FF-E04-absolute-resources-20261122/edge.json) · [旧请求复验](../../tmp/FF-E04-absolute-resources-20261122/legacy-fs.json)

初次 WSL 探测发现 BibTeX 依赖 `/etc/alternatives` 中的链接，随后 Windows 和 Linux 的 biber 探测定位到共享库 `libbtparse.so.2`。补齐这些运行时文件后通过复验。[初次 WSL 日志](../../tmp/synctex-preparation-budget-iiu8852h/actual-job-1.log) · [biber 探测日志](../../tmp/synctex-preparation-budget-lkvqp20_/actual-job-1.log)

首次取消脚本依赖缓冲日志观察编译启动，观察超时。复跑直接检查原生文件读取记录并核对持久停止凭据。当时实际算例工作目录留下一个 `.latex-compile-*`，包含编译副本和读取记录，随后已在本地及认证 S3 完成回收复验。[原始阶段回执](../../tmp/FF-E04-absolute-resources-20261122/validation.json) · [回收复验](latex-compile-cleanup.md)

本轮为实际编译、文件存储和流程验证，模型调用 0。回退时按现有 LaTeX 部署流程等待运行中作业结束，再切回上一版 API 和 worker。已有成功版本可按发布 ID 查看。[部署与回退](linux-tex-deployment.md)
