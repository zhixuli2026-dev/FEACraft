# HTML/DOCX 跨文件章节定位

2026-11-17，FF-E22 / WP09 / R4 / T07。阅读器原有算例文件链接现在保留章节片段，例如 `chapters.html#%E7%BB%93%E6%9E%9C`。目标 HTML 或 DOCX 解析、消毒和挂载完成后，定位对应 ID 并移动键盘焦点。文件内容及下载原件不被修改。

## 行为与实现

`store.openFile(path, hash?)` 在文件打开成功时原子设置本次章节请求；请求绑定算例、文件和唯一 ID。`DocumentPanel` 等目标快照实际显示后消费一次请求，不以固定延迟猜测 DOCX 转换时间。账号、工作区、算例、文件或发布选择变化都会清除旧请求；读取失败也清理请求。章节位置不写入持久浏览状态，不会在下次随意打开文件时重复跳转。

同一文件的 `#章节` 和 `文件名#章节` 都可定位。缺失 ID 提示“未找到文档锚点”；无效百分号编码不切换文件；真实文件 404 显示链接打开失败，保留来源文档。HTTP(S) 外链和原有非文档文件打开方式保持。

目标范围是 HTML/DOCX 中实际存在的 ID，包括 HTML 明确 ID、DOCX 转换后书签或生成的 heading ID。没有增加跨算例 URL 或 PDF 页码、源码行号协议，也不以本文作为这些其他功能的验收。

## 实际验收

入口：`platform/tools/document-anchor-browser.cjs`。审查记录 `tmp/FF-E22-document-anchor-review-v3-20261117.json`，670 份源码及工具已归档；导入仅 Node 内置模块、Playwright 和既有产品代码，没有模型服务或推理替身。

实际 PostgreSQL/Docker API 与 Chromium 的 5 项检查通过，结果在 `tmp/docker-deployment-20261117/document-anchor-8967155908/result.json`：

1. 中文编码片段从另一 HTML 文件跳到“结果”，实际滚动 2662px，标题距阅读区顶部约 -0.47px，键盘焦点位于标题。
2. 带文件名的同文件片段回到开头。
3. 缺失章节、编码错误和真实文件 404 都明确提示，后续链接仍可操作。
4. 读取此前实际 Pandoc 生成的 DOCX，等待真实转换后定位“分式与根式”，下载与上传原件逐字节相同。
5. 暂缓已经读取到的真实 HTTP 响应，切换到另一个算例后再放行；旧正文和章节请求没有进入新算例。这里使用原始 `route.fetch` 响应，不伪造业务结果。

1440×1000 深色和 1100×800 浅色截图已查看；后者正文区域实际宽 184px，内部无横向溢出，定位与焦点仍有效。这是桌面 Dock 布局中的两种宽度，不声明手机或其他浏览器验收。机械 detector 没有报告匹配项，但其源码正则扫描不覆盖全部视觉问题。

首次 `document-anchor-0053a43528` 因测试在文件树加载前判断展开状态而超时；等待真实树后，`64e5e176a9` 复现产品缺陷：目标偏移 2661.53px、滚动 0、无焦点。两轮失败保留。修复后沿用原定位断言通过，没有修改成功阈值。

TypeScript/Vite 正式镜像构建成功；当前 web 镜像 `vibeflow/web:document-anchor-20261117`，ID `sha256:5f5a8fe6876cb5e027fbcb47cc1ccbef57e4a8ae672b223074562b46ece675b7`。API/worker 继续使用完整 CSS v2 镜像。此次实际模型 0、模拟 LLM 0，不作为模型回合验收。

## 交付与回退

源码为 `web/src/store.ts` 和 `web/src/panels/DocumentPanel.tsx`，不改数据库。回退 web 至 `vibeflow/web:dynamic-upstream-20261117` 可恢复修改前界面；文件、账号及报告原件无需回滚。

本批关闭“跨文件章节被丢弃”缺陷，FF-E22 原整项条件仍见 [报告与阅读器剩余核对](report-delivery-reconciliation.md)。主线49/53、剩4；资源11/17、剩6；295项完整交付目标不变。
