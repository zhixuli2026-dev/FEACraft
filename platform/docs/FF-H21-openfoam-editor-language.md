# FF-H21 无扩展名 OpenFOAM 场文件语言识别

2026-11-19。旧规则按文件名及父目录选择 Monaco 语言。带点场名虽然匹配前端规则，真实 `/file` 会把 `alpha.water` 归为二进制，编辑器因此没有打开。整数结果时刻或区域子目录里的场也可能落入纯文本。OpenFOAM 允许不同时间目录保存场，并用 `FoamFile` 字典声明内容类别和 `ascii` 或 `binary` 格式。[原审计](feature-gap-audit.md) · [OpenFOAM 算例目录](https://www.openfoam.com/documentation/user-guide/2-openfoam-cases/2.1-file-structure-of-openfoam-cases) · [OpenFOAM v2406 时间目录实现](https://api.openfoam.com/2406/Time_8C_source.html) · [OpenFOAM 文件头说明](https://api.openfoam.com/2312/classFoam_1_1IOobject.html)

文件编辑器读取候选文件同一存储版本的前 8 KiB，识别开头的有效 `FoamFile` 声明。确认 ASCII 后，API 返回原文本和 OpenFOAM 语言提示。声明为二进制的文件沿用普通下载，已打开的文本仍通过原 SHA-256 条件保存。旧 `0`、`0.x`、`0.orig` 规则继续处理历史文本。Python、Markdown 和普通文本扩展名优先于目录回退规则。[文件头判定](../server/app/services/openfoam_editor.py) · [文件 API](../server/app/routers/files.py) · [编辑器规则](../web/src/utils/fileLanguage.ts)

实际 Edge 打开生产构建，从文件树打开 13 个文本文件并核验 Monaco 语言和完整内容。其中九个 OpenFOAM 文件覆盖旧 `0.orig`、带点的 `alpha.water`、整数结果时刻、科学计数法时刻、负数时刻、并行计算区域、`constant/polyMesh` 和 `system` 字典。普通 Python、Markdown、纯文本和只有注释提到 `FoamFile` 的文件得到各自原语言。对区域下的 `alpha.water` 修改并保存，新的 SHA-256 与服务端回读一致。一个 404 文件补齐后可正常打开，另一个声明为二进制的场从同一界面下载，文件名和全部字节一致，旁边的普通二进制文件保留下载入口。跨账户读取和只读写入拒绝通过。隔离 FS/SQLite API 已退出，模型调用为 0。[真实验收回执](../../tmp/h21-openfoam-editor-zlzwkhst/openfoam-editor-result.json) · [实际着色](../../tmp/h21-openfoam-editor-zlzwkhst/foam-field-editor.png)

复验使用生产构建、服务端虚拟环境和装有 Playwright 的 Python。Node 规则检查与生产构建通过。[真实浏览器入口](../server/openfoam_editor_browser_acceptance.py) · [规则检查](../tools/file-language-smoke.mjs) · [生产构建](../../tmp/h21-openfoam-editor-build-20261119.log)

```powershell
& 'platform/server/.venv/Scripts/python.exe' -B -X utf8 `
  'platform/server/openfoam_editor_browser_acceptance.py' `
  --dist 'tmp/h21-openfoam-editor-dist-20261119' `
  --browser-python 'C:/ProgramData/anaconda3/python.exe' --channel msedge
```
