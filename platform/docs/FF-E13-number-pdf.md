# FF-E13 为现有 PDF 添加页码

2026-11-19。原合同要求正式 `POST /pdf:number` 接收已有 PDF，使用登记的原生工作器添加底部居中灰色 `page / count`，保留文档内容、页数和原件，输出独占创建。历史验收涵盖真实 LaTeX 来源、PDF 阅读器文本层、普通下载以及本地和远端正式工作器。[原审计 E13](feature-gap-audit.md) · [既有历史产物验收](N2-07-acceptance-audit.md) · [远端工作器](remote-document-workers.md)

工作器按 MediaBox 与 CropBox 的可见交集定位页脚，并针对页面自身旋转反向换算坐标。原页面尺寸、旋转、字库和内容流得以保留。同名页脚字体使用新的资源名称，原页面的裁剪或坐标变换限制在原内容范围。发布时核验工作器的收据及页码的位置和字体。[PDF 坐标与可见区域](https://opensource.adobe.com/dc-acrobat-sdk-docs/library/interapp/IAC_API_CoordinateSystems.html) · [工作器库](../server/app/services/pdf_pages.py) · [发布核验](../server/app/services/pdf_number_handler.py)

首轮方案中，实际 Windows 工作器已经成功给纠正后的 PDF 编入页码。验收脚本使用 Poppler XHTML 所报的未旋转页面尺寸，遇到旋转页面时计算出了错误的足边距离。随后用同一份真实产物的实际栅格图幅交叉核对，0°、90°、180°、270° 的四页页脚均位于可见图幅的横向中央。夹具读取实际图幅后再计算位置，首轮结果和进程独立退出回执保留。[首轮记录](../../tmp/e13-number-existing-pdf-dsuk68cf/pdf-number-result.json) · [独立实际渲染](../../tmp/e13-number-existing-pdf-dsuk68cf/raster-check-first-attempt.json) · [首轮进程核验](../../tmp/e13-number-existing-pdf-dsuk68cf/cleanup-independent.json)

## 实际验收

从公开上传 API 送入截断 PDF 后，真实 Windows 工作器退出码为 1，原件字节保持，输出路径不存在。同一个幂等键返回同一失败作业。通过条件上传换成有效 PDF，再以新键请求页码成功。已有目标和同键不同请求分别返回独占冲突和幂等冲突，同源覆盖返回 400。[最终结果](../../tmp/e13-number-existing-pdf-mol_0lxe/pdf-number-result.json)

随后发布了第二个输出版本。两代 PDF 各有四页，逐页保留原始内容流、原 Courier 字体、边框和旋转，同时保留文档元数据、目录和原 Page Labels。Poppler 核对八页的实际图幅和页脚位置，页码与中央的偏差小于 0.01 点，距可见底边约 20.14 点。源文件经公开原件接口按字节复核。最新可见文件与新版本一致，旧版按发布 ID 仍可独立读取全量、HEAD 和 Range，错误版本令牌返回 409。[旋转页实图](../../tmp/e13-number-existing-pdf-mol_0lxe/numbered-generation-2-2.png) · [第一代 PDF](../../tmp/e13-number-existing-pdf-mol_0lxe/numbered-generation-1.pdf) · [第二代 PDF](../../tmp/e13-number-existing-pdf-mol_0lxe/numbered-generation-2.pdf)

未获授权的账号读取和提交均返回 404。获工作区 viewer 权限后能按原字节读取旧发布，编页码继续返回 404。实际验收三次工作器运行，状态为一次失败、两次成功。原 API 进程和三份 Windows 命名 Job Object 均独立核实已退出，隔离端口关闭。模型调用为 0。阶段前后 C 盘可用分别为 22.17 和 22.16 GiB，Docker 构建缓存为 0。[独立进程证据](../../tmp/e13-number-existing-pdf-mol_0lxe/cleanup-independent.json)

## 复验入口

从 `platform/server` 运行业经审查的 `pdf_number_acceptance.py`，使用真实 FastAPI、D 盘隔离 SQLite 与文件存储、Windows 注册工作器和 WSL 非特权 Poppler。该验证只调用文档 API。[验收脚本](../server/pdf_number_acceptance.py)

```powershell
& .venv/Scripts/python.exe -B -X utf8 pdf_number_acceptance.py
```
