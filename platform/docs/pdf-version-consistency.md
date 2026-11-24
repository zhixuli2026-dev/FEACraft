# PDF 跨请求版本一致性

2026-11-05。PDF.js 先 HEAD 再多次 Range；此前这些请求只比对长度，同长度文件被替换时可能拼接不同内容。现为整次打开固定一个文件版本，变化后停止读取，用户明确重新读取才能取得新版本。

`/cases/{id}/raw` 的成功 HEAD/GET/206 响应增加 `X-Vibeflow-File-Version`，并设置 `Cache-Control: no-store`。客户端在后续请求发送同名头作为预期版本；标识格式为 64 位小写十六进制。不同版本或已删除的指定版本返回 409 `file_changed_during_read`，格式错误返回 422。无条件旧客户端保持原下载接口；没有预期版本的缺失文件仍为 404。

该标识是文件身份和存储位置的摘要，不是全文内容 SHA-256，也不是宣称具有 HTTP 强 ETag 语义的字段。本地使用解析后的路径、设备/文件标识、长度及纳秒修改/元数据时间；S3 使用 endpoint、bucket/key、ETag 与长度。同一存储版本在新 API 实例中保持相同标识。每次实际读取仍执行原有文件身份前后检查或 S3 If-Match；跨请求校验没有替代请求内校验。

PDF 阅读器要求 HEAD 提供有效版本，后续每段验证响应版本、Content-Range 的偏移与总长、Content-Length 及实际长度。首次错误中止其余请求并销毁加载任务，忽略迟到片段；错误页和正常工具栏均有“重新读取”。切换文件会中止旧 HEAD 和范围请求。没有以全量下载绕过 Range。

验证结果：

- 文件存储 HTTP 17 组，真实 SeaweedFS 4.52 HTTP 16 组，含同长度替换、删除、不同路径、错误标识、新 API 实例及原分段下载兼容；另保留 6 组模拟存储协议异常。
- `pdf-range-consistency-smoke.mjs` 13 项，含错误版本/范围/长度、短正文、200 回退、同时请求的取消与迟到结果、任务尚未挂接时失败和正常销毁。
- 真实 PDF.js 浏览器 7 项，使用两份同长度、60 页、4,217,841 字节 PDF。验证文本与翻页、替换拒绝、显式重新读取、错误协议响应、恢复和迟到 HEAD。首次呈现观察为 61 次请求、3,955,697 字节；只是证明保留了分段读取，并非优秀加载性能或性能基线。
- 前端构建通过；截图已检查。测试 API/Vite、S3 实例及临时 PostgreSQL 均独立于开发环境并已退出/删除。

文件存储身份校验不承诺对抗拥有操作系统权限且能伪造所有身份元数据的写者。跨主机一致性、所有数据库/存储组合、全量生产故障矩阵仍归 N7-01；选择任意历史 PDF、SyncTeX 与选中历史版本联动、刷新恢复等仍按原任务交付。

实现与证据入口：`app/services/file_read.py`、`routers/files.py`、`web/src/utils/pdfRange.ts`、`panels/ViewerPanel.tsx`；`tmp/mainline-N2-publication-consistency-closure-evidence/`。
