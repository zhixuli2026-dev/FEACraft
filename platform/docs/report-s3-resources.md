# 报告模板资源的 S3 / 独立 worker 验收

2026-11-17，FF-E02 / FF-E22，T07。新增 reference-doc、嵌套 CSS、字体、背景图和 srcset 已在真实 S3 与独立 worker 组合中通过。普通 Markdown 输入，非空正文包含表格、公式、代码和嵌套列表。本轮没有模型推理，真实模型 0、模拟 LLM 0。

## 实际环境

独立 Compose 项目 `vf-report-s3-20261117`，入口 `http://127.0.0.1:57700`。PostgreSQL 16、Redis 7、SeaweedFS 4.52、API、arq-worker、nginx 共六个容器。API 与 worker 的文件缓存、索引和运行时卷分别配置，没有共用文件缓存来冒充 S3 读取。SQL 中首个和重建后作业的 worker_id 分别匹配对应 worker 容器 hostname。

API/worker 使用 `vibeflow/api:srcset-v2-20261117`；SeaweedFS 使用本地已存在的真实 Linux 4.52 二进制构建，哈希 `ce138fc99edc946ebd4d38589543f717e388bfe0b45186c06cff6ee6734ee8df`。最终 web 为 `vibeflow/web:reader-error-20261117`，镜像 `sha256:a4bd493c09b03d4260a7e41aebacfb9f18dd43b24d40c850b3be9d2fdef64409`，TypeScript/Vite 完整构建退出 0。私有 compose 和账户文件只保存在 tmp 下，不进入发布包。

这是同一台 Windows 宿主上的独立 Linux 容器组合，不代表 N7-01 跨物理主机矩阵已完成。S3 仍显式启用现有 allow_partial_s3 门槛，本轮不解除全产品 S3 门槛。

## 结果

实际目录 `tmp/report-s3-20261117/resources-ac009b1df9`，算例 2、作业 1/3/4 成功，作业 2 在实际 worker 停止期间排队并取消。

后端入口 `tools/report-s3-resources-acceptance.py`，7 项通过：

1. 全部模板、CSS、嵌套导入、字体、图片与 Markdown 哈希进入清单；独立 worker 实际 Pandoc 生成 HTML/DOCX，后者保留模板标题色、页眉及图片。
2. 真实第二账户请求结果、原件、模板、构建和作业全部 404。
3. 缺失候选图片在作业创建前 400；已有发布下载保持。
4. 停止实际 worker 后提交并取消排队报告，重启 worker，旧原件保持。
5. 修改 CSS、srcset 候选和 DOCX 模板使旧来源过期，新版包含新样式，旧 HTML/DOCX 字节不变。
6. 重建 API/worker 容器后，当前和历史 HTML/DOCX 仍逐字节相同。
7. 重建后的新 worker 再次成功执行 S3 报告构建。

浏览器入口 `tools/report-s3-resources-browser.cjs`，3 项通过：下载 HTML 的前后样式、实际加载字体、背景图和候选图片正确，外部请求 0；应用内历史 HTML 选图正确、刷新保留选择和原件下载一致；DOCX 回退图、MathML、表格、代码换行及历史原件一致。

独立阅读器边界入口 `tools/document-reader-s3-boundaries.cjs`，最终另有3项：真实上传的主动 HTML 内容被过滤，本地图片保留；真实损坏 DOCX 清空旧正文、显示中文错误并禁用下载；实际覆盖正确文件后显式重读恢复公式/表格，下载等于上传原件。没有路由替身或模拟模型。

前两项入口分别使用审查 `FF-E02-s3-resources-review-v2-20261117.json`（682份）与 `FF-E02-s3-resources-browser-review-v1-20261117.json`（683份）。最后一次中文错误提示修复的定向复验采用 `FF-E22-s3-boundaries-review-v3-20261117.json`（684份），所有前序源码归档保留。错误提示修复没有改变后端、消毒规则或正常转换路径，不把旧结果冒充新源码的全套重跑。

## 保留的失败与边界

首轮 `resources-66f20eee62` 在连续上传时遇到明确的 `storage_quota_busy`。实际 SQL 核验相应预留已结算；后续驱动只对这一准入拒绝做最多15秒的有限重试，记录4次，其他错误直接失败。未降低存储配额、不直接改业务表，也不把暂时拒绝掩盖成无错误。

阅读器首轮错误把 Shadow DOM 自带的固定样式表计入上传文档的危险标签；源码证实该样式位于 article 外。改为检查实际上传的 document-content 后通过，外网请求、事件属性、脚本和危险标签断言未删除。原失败 `reader-boundaries-before-content-scope.json` 与修改中文提示前的成功 `reader-boundaries-before-error-copy.json` 均保留。

取消覆盖真实排队状态；运行中取消继续引用既有[报告构建控件](report-build-controls.md)和原主线停止证明，不声称本轮重跑全部底层故障。重建保留各自持久缓存卷，验证新进程读取与执行，不声称擦除了所有磁盘缓存。

继续使用当前 API 镜像；web 回退可恢复 `srcset-v2-20261117`，保留数据库、S3 与原件卷。后续工程 compose 模式和模板配置的组合按 [交付核对](report-delivery-reconciliation.md) 推进。FF-E22 的逐项结论单列于 [阅读器验收](document-reader-acceptance.md)。
