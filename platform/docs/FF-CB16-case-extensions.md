# FF-CB16 算例扩展

算例面板可以按命名空间注册、编辑、启停和删除扩展。编辑沿用 caseDict 修订号和文件摘要，和其他写入发生冲突时保留本地草稿，核对服务器版本后可以重新提交。注册和删除使用原有 `PUT/DELETE /cases/{id}/caseDict/extensions/{namespace}`，旧空块仍可读取。

修复前，实际 API 返回的扩展设置可能与保存后的值不同。隔离测试中，包含引号的两个不同键合并成一个，列表对象写成普通文本，引号和列表边界也发生了变化。现在只在扩展设置内解析标量列表，带引号的字面键和值按数据保留。普通数值和布尔参数沿用 caseDict 的 OpenFOAM 原子表示。设置支持子字典和标量列表；专用 API 和块编辑 API 对空值和列表内的复合值返回 422。专用写入保留其它命名空间原来的文本，包括注释和列表空白。[修复前实际读回](../../tmp/cb16-before-uxxrrnia/extension-values-before.json)

每次扩展实际变化进入原溯源哈希链，记录扩展块和 `extensions_sha256`。专用写入也继续保留 `source=extensions`。删除后的记录保留当前扩展块，旧修订仍可查阅原条目。

## 2026-11-19 验收

- 真实 Edge 完成注册、重新加载、更新版本、停用、处理陈旧版本冲突及确认删除。格式错误的 JSON 不发请求。验证重新加载后仍能区分含引号的键、单项和空列表、嵌套子字典及字面括号。[浏览器记录](../../tmp/case-extensions-browser-s9ci0j74/browser-result.json) · [编辑页面](../../tmp/case-extensions-browser-s9ci0j74/extensions-desktop.png)
- 独立 API 核验不支持的设置返回 422，已删除或其它账号的扩展请求返回 404，拒绝前后文件与溯源不变。块编辑保留同样的设置语义，旧命名空间原文逐字一致。停止旧 API 进程后从同一磁盘与 SQLite 启动新 API，重新登录读取相同 caseDict 和溯源链。[完整回执](../../tmp/case-extensions-browser-s9ci0j74/case-extensions-result.json)

首次浏览器运行在删除后的选项刷新完成前就读取列表，留下失败回执。复核该轮磁盘字典与溯源已提交删除，最终复跑等待页面选项更新后通过。[首次浏览器回执](../../tmp/case-extensions-browser-sryf_xyn/case-extensions-result.json)

本轮使用 D 盘隔离文件存储和 SQLite，模型调用为 0。原 `extensions_smoke.py`、TypeScript、Vite 和 Impeccable 检查通过。同一最终构建复跑了 CB15 的真实作业证据、错误编辑和溯源流程。[CB15 回归](../../tmp/case-errors-browser-3pcc4tvs/case-errors-result.json) · [回归入口](../server/case_extensions_browser_acceptance.py)
