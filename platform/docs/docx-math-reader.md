# DOCX 原生公式阅读

2026-11-17：修复 DOCX 中 Office Math 公式在语义阅读器里被省略的问题。直接转换下载原件中的 OMML，不读取同名 HTML 或重新生成公式。此前报告的两处公式现已显示；新增实际 Pandoc 报告的 12 处公式及不支持结构提示通过 Chromium 验收。

## 实现和行为

`web/src/utils/docxMath.ts` 在转换副本中提取公式，将随机位置标记交给 Mammoth 保留正文结构，再在对应文本位置恢复 MathML。读取正文、脚注、尾注和评论 XML；未进入语义阅读结果的文档区域计入转换提示。用户下载仍使用最初获取的 Blob，副本从不写回服务器。

支持行内/独立公式、分式、上下标和组合上下标、前置上下标、平方根/高次根、定界符、矩阵、公式数组、求和/积分等带限算子、上下极限、函数、重音及上划线等结构。MathML 使用 DOM 创建，只读取公式文本和指定属性，再经过现有 DOMPurify 和 Shadow DOM 阅读流程。转换没有新增依赖，既有 SVG、标题导航、脚注及下载路径保持。

未知结构或缺少必要参数时，整条公式显示“公式未能预览，请下载原件查看”，避免丢弃未知算子后展示数学意义错误的表达式。`phant`、`borderBox` 等尚未实现的结构使用这个明确降级；不声明所有 Word 排版属性或全部 OMML 结构均已支持。字体、特殊对齐、组合字符等没有穷举视觉验收，复杂分页仍不在语义阅读器的逐像素保证之内。

转换器依据 OOXML 结构实现。结构参考包括 Microsoft 的 [Fraction](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.math.fraction?view=openxml-3.0.1)、[Nary](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.math.nary?view=openxml-3.0.1) 和 [Delimiter](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.math.delimiter?view=openxml-3.0.1)。未安装已停止维护的 omml2mathml，也未替换整套阅读器。

## 实际验收

入口 `platform/tools/docx-math-browser.cjs`，执行前审查 `tmp/FF-E02-docx-math-review-v2-20261117.json`，664 份源码归档到对应 sources-v2 ZIP。入口仅使用 Node 内置模块、已锁定的 JSZip 和真实 Playwright；未调用模型或导入推理替身。

最终结果：[browser-result.json](../../tmp/docker-deployment-20261117/docx-math-064c29af78/browser-result.json)。

1. 原先的真实报告两处公式恢复，SVG 图片继续可见，下载与所选发布原件完全一致。
2. 新建算例 16、实际 Pandoc 作业 17 成功，根停止证明通过。12 处公式覆盖重复行内公式、分式、上下标、两类根式、2×2 矩阵、求和、积分、极限、重音、二项式和脚注；核验 DOM 结构、非零可见尺寸、文本位置及下载字节一致。
3. 真实上传修改后的 DOCX 故障文件，将一个分式算子替换为未知结构。对应整条公式显示提示，其余 11 处公式保留，下载仍等于上传原件。

浏览器未捕获页面错误；实际截图已经查看，分式/矩阵/积分排版及故障提示可见。此次为 Docker/Pandoc/浏览器和文件故障验收，真实模型 0、模拟 LLM 0、Claude 0。

保留首次浏览器结果 `docx-math-3fe41ea8ae`：前两项通过，第三项在异步切换完成前读取到上一个文档。后续只修改测试等待条件，等待工作区文件快照和对应提示后再断言，生产代码未因此改动。

本地 TypeScript 检查通过，impeccable 检查无发现。首次镜像命令存在工作目录复制错误，且未配置进程代理导致仓库认证失败；保留日志。修正后构建日志证明 tsc/Vite 与镜像导出完成，虽然 PowerShell 会话返回 1；进一步核对部署容器与导出镜像 ID 均为 `sha256:7811a383a32ed299bdb647045d85eb5158b5ab70b70f3817a80b9590b1dbc75e`，实际浏览器运行了该镜像，不以 shell 返回值冒充成功。

## 剩余范围

此前“DOCX 公式全部空白”的具体缺陷已修复。完整报告模板资源、复杂版式以及原计划的权限/恢复/部署组合仍继续，FF-E02/FF-E22 不因这次修复自动整项关闭。主线 49/53、剩 4；资源 11/17、剩 6；295 项全功能交付目标保持。
