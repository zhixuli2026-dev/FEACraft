# 报告 CSS 与引用资源冻结

2026-11-17：HTML 报告支持本地 CSS、嵌套 `@import`、图片和字体。资源原件进入构建输入清单，导出 HTML 使用自包含数据，不依赖算例中的可变文件。实际 Pandoc/Chromium 的 4 项检查通过；本轮模型调用 0、模拟 LLM 0。

## 使用方式

在 `E-report/report.md` 的 YAML 元数据中指定一个或多个样式表：

```yaml
---
css:
  - styles/main.css
reference-doc: templates/brand.docx
---
```

CSS 控制导出 HTML；DOCX 使用独立的 `reference-doc` 样式模板。样式表路径按报告目录优先、算例根目录其次的既有规则选择。CSS 内部的相对 URL 以所在样式表目录为起点，选中的候选路径、原件字节和哈希随报告冻结。

也支持正文中的 `<link rel="stylesheet">`、`<style>`、行内 `style`，包括 Pandoc 转换为结构化节点后的属性。CSS 解析使用 `tinycss2==1.4.0` 与 `webencodings==0.6.1`，不会用正则表达式代替 URL/转义解析。嵌套导入保留顺序及媒体条件；图片和字体内嵌到导出结果，原始 CSS/字体/图片仍保留在输入清单供来源复核。

远程引用需先保存到算例内；循环引用、超过 32 层的引用、缺失文件、无法解析的 CSS 会明确失败，保留已有成功发布。导出不会在构建时下载远程样式、图片或字体。HTML的 `srcset/picture` 后续已单独实现并实际验收，见[响应式图片](report-responsive-images.md)，其证据与CSS的 `image-set` 分开记录。

应用内 HTML/DOCX 面板继续提供安全语义阅读，不把自定义 CSS 注入宿主界面。本次样式实际渲染证据来自打开下载的独立 HTML 文件；不能将其表述为应用内语义阅读器已逐像素呈现自定义模板。

## 验收

入口 `platform/tools/report-css-browser.cjs`，审查 `tmp/FF-E02-report-css-review-v2-20261117.json`，669 份源码及依赖声明已归档。最终 [result.json](../../tmp/docker-deployment-20261117/report-css-3eb91c8bae/result.json)：

1. 实际作业 22 生成 HTML/DOCX。Chromium 从磁盘打开原样下载的 HTML，验证两级 CSS 导入、真实 WOFF2 字体加载、独立样式链接、style 块和行内属性中的三张 SVG 背景图。无 HTTP/HTTPS 请求，字体状态为 loaded；桌面标题 36px，390px 视口按媒体查询变为 24px。桌面/窄屏截图均已查看。
2. 正文、全部样式表、字体及 SVG 的原始字节哈希均在实际冻结清单中。
3. 修改内层 CSS 与图片后，旧来源标为过期；实际作业 23 生成新发布，标题由 `#135c72` 变为 `#7a3550`，两份历史下载原件保持。
4. 循环导入、缺失图片、远程 CSS 导入和 CSS 转义后的远程 URL 均返回 400，不额外创建作业，成功发布保持。

两个报告根停止证明通过，未捕获浏览器页面错误。失败目录 `report-css-51f813bbe0` 保留：样式表、字体正常，但 Pandoc 把行内 style 转为 Attr 元组，绕过原 RawHTML 入口，导致一张图片仍指向相对路径。修复遍历入口后沿用原断言通过，没有弱化检查。

字体算例复用仓库已安装 KaTeX 的 `KaTeX_Main-Regular.woff2`，仅用于本地验收。SVG 是自行编写的几何示意图，不冒充求解或实验结果。

## 构建状态与范围

浏览器验收镜像为 `vibeflow/api:report-css-incremental-v2-20261117`。随后完成正式 `Dockerfile.api` 全量构建，并从当前上下文重新构建为 `vibeflow/api:report-css-v2-20261117`，已部署至 API 和 worker。镜像内 463 个 Python 源文件与工作区逐文件哈希一致；全部 104 个原有 Python 包版本未变，仅新增上述两个依赖。web 继续使用已验收的动态后端地址解析配置。

依赖变化触发 CAD/VTK 等大包下载的初次完整构建已成功退出；初次上下文早于行内 Attr 修复，因此没有部署其旧源码镜像。当前 v2 镜像 ID 为 `sha256:034e20318945ce2754edf4191e79cf8cc8c61dd029c3c987e4a589fa7870759b`。实际替换容器后，公开健康接口返回 200，原账户登录和三份历史报告下载均通过逐字节核对，见 `tmp/docker-deployment-20261117/css-full-restart-check.json`。构建日志分别为 `build-api-css.log`、`build-api-css-v2.log`；没有以增量镜像成功冒充完整构建成功。

本阶段未声明跨浏览器、全部 CSS 语法、任意 Word 复杂布局及全部存储/权限/恢复组合完成。下一步应按 FF-E02/FF-E22 原验收条件汇总现有证据并识别具体缺口，避免将已通过项反复列为未实现。主线仍 49/53、剩 4；资源 11/17、剩 6；295 项全功能交付目标保持。
