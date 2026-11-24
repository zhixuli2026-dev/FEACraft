# FF-E22 HTML/DOCX 专用阅读器验收

2026-11-17。按 FF-E22 原 required_work、原审计备注和全功能计划第5节逐项核对，HTML/DOCX 专用语义阅读器已验收。全功能整项完成 1/295、剩294；主线仍49/53、资源11/17。全功能计划第7节的全产品部署、性能、权限矩阵及最终发布继续由原主线承担，不以单项完成替代。

## 原要求与证据

| 原条件 | 当前结论与可复核证据 |
| --- | --- |
| 正常阅读及完整 UI/API | 实际文件树打开普通与发布 HTML/DOCX；标题、表格、图片、嵌套列表、代码换行、脚注与书签。[复杂文档](report-office-layout.md) 的100行表格和本轮 [S3 阅读](report-s3-resources.md) 均实际显示。 |
| 原备注的残余能力 | [DOCX公式](docx-math-reader.md) 修复此前全部省略，12处实际公式含分式/矩阵/积分/脚注；未知结构给出整条降级提示。 [章节定位](document-anchor-navigation.md) 的5项实际检查覆盖跨文件中文片段、同文件、DOCX解析后定位、错误和迟到响应。 |
| 目录、页内与跨文件导航 | heading导航、明确锚点、DOCX空书签定位及键盘焦点已验证；缺失ID/无效编码/真实404明确提示，后续仍可导航。 |
| 固定版本与原件下载 | [固定发布](document-publication-reader.md)、[任意历史](report-history-selection.md) 的版本绑定和分页基线；本轮S3真实新旧版本选择、刷新选择保持、HTML/DOCX下载逐字节核验，读取不替换历史资源。 |
| 错误、取消与恢复 | 实际损坏DOCX显示可操作中文说明、禁用下载且清除上一份正文；实际修复文件后显式重读恢复。跨算例迟到响应丢弃已有实际检查。构建排队取消与API/worker重建后的历史阅读在本轮通过。 |
| 权限与数据边界 | 本轮真实第二用户对结果、原件、模板、构建和作业均404；普通图片实际Bearer读取后使用Blob。主动HTML脚本/事件/javascript链接/样式/iframe/object/embed和远程SVG/srcset被过滤，外网请求0，合法本地图片保留。原N2-07所有者/通道边界证据保持。 |
| 配置、兼容、实际产物 | FS及S3/独立worker实际部署；下载的DOCX由实际Pandoc生成，办公软件排版另有LibreOffice实测；前端当前完整tsc/Vite构建成功，未增加依赖或迁移。当前目标为实际Chromium桌面，已有1440/1100宽度和深浅主题检查。 |
| 文档、失败证据与回退 | 本文、各能力页、S3结果及本次规范证据目录覆盖；源码审查/哈希/失败归档保留。web可回退srcset-v2镜像，数据库和原件保持。 |

## 最新实际证据

`tmp/report-s3-20261117/resources-ac009b1df9/result.json` 后端7项、`browser-result.json` 浏览器3项、`reader-boundaries.json` 当前错误提示下3项。最后一次只改DOCX转换失败的中文提示，后端源码、正常转换、资源消毒和导航没有改变；定向复验3项通过，不声称重跑全部历史集合。

规范记录为 `tmp/mainline-report-s3-reader-evidence/validation.json`；当前审查684份源码，来源 `tmp/FF-E22-s3-boundaries-review-v3-20261117.json`。相关实际旧结果为 `tmp/mainline-document-anchor-evidence/validation.json`、`tmp/mainline-office-layout-evidence/validation.json`、`tmp/mainline-docx-math-evidence/validation.json`、`tmp/mainline-srcset-evidence/validation.json`；核对真实结果文件，不以测试名称或绿色标签代替断言。

所有本批检查属于实际基础设施、浏览器或文件故障，模型0、模拟LLM0。未重跑历史模拟LLM集合。

## 支持边界

保持原备注的语义阅读范围：不执行宏/脚本，不编辑DOCX，不承诺Word分页与任意版式逐像素复刻。导出原件保留用户需要的办公软件排版。尚未支持的OMML结构明确显示无法预览并提供原件；不存在静默丢弃未知算子后伪装正确的声明。

不把Chromium模拟窄屏当作真实手机/所有浏览器验收，不把同宿主多个Docker容器当作跨物理主机。OCR准确率、PPT可编辑性属于T07中其他功能项；不转嫁为E22额外能力，也不宣称一并完成。FF-E02工程报告模板组合仍实施中。
