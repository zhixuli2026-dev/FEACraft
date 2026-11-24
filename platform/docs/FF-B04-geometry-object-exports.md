# FF-B04 逐对象几何导出

STEP 与 STL 在同一次导出中使用同一文件名规则。对象原名写入 `geometry_manifest.json`，已经能生成唯一文件名的对象继续使用原文件名。名称归一后相同的对象使用不同编号，编号跳过被其他对象占用的原文件名。文件名比较兼容 Windows 的大小写规则，设备保留名会调整。[CadQuery 导出器](../server/app/services/geo_script.py)

在隔离 SQLite 和文件系统上，实际 CadQuery 导出了六个宽度不同的实体。十二个正式下载分别与发布清单摘要一致，STEP 具有合法文件头，STL 的实际顶点范围与各自对象尺寸吻合。无认证和跨账户读取分别返回 401 和 404。实际导出钩子失败后，原发布的十二份文件照常读取。修复脚本后执行新导出，旧发布文件仍原样保留。三次自动预览、两次成功导出和一次失败导出有原生作业回执，隔离 API 已退出，模型调用为 0。[本轮实测](../../tmp/geometry-export-identity-kw78l3yo/geometry-export-identity.json) · [复验入口](../server/geometry_export_identity_acceptance.py)

既有浏览器验收通过流程启动实际导出，在文件树打开 STEP，还从固定发布下载 STEP 和 STL，并复核下载字节摘要与切换算例时的迟到响应。本轮新增的重名和恢复场景通过同一公开导出和下载 API 复验。[既有浏览器结果](N6-07-browser-lifecycle-progress.md#第六增量固定几何原件下载移动布局与运行批次取消)
