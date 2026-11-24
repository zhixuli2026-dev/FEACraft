# FF-A13 全局路径文本格式

2026-11-19，Windows 与 Linux／POSIX 路径格式完成浏览器验收。设置保存在当前浏览器。首次访问与遇到旧的无效设置值时均使用 Linux／POSIX，手动选择的 Windows 和 Linux 设置在刷新后恢复。

Microsoft Edge 和 Chromium 各通过七种设置状态，每种状态实际执行编辑器的「复制路径」和「复制相对路径」，再从浏览器剪贴板回读。复制的存储路径取自真实算例接口。Windows 格式使用反斜杠，Linux／POSIX 格式使用正斜杠。Windows 设置下刷新后重新打开文件，HTTP 查询仍使用 POSIX。编辑器的活动文件键和算例接口的存储前缀同样使用 POSIX。实际点击「加入聊天」生成 POSIX `@Ref/nested/format-proof.txt` 草稿。本轮保留为未发送草稿，模型与模拟调用均为 0。

第二个账户的目录树只显示其可访问算例。两种浏览器均核对了该账户自己的复制结果，并确认以其身份请求原账户的算例信息和文件均返回 404。

| 验收 | 证据 |
| --- | --- |
| Microsoft Edge | [行为回执](../../tmp/path-format-browser-003qn_lh/msedge-path-format.json) · [Windows 设置截图](../../tmp/path-format-browser-003qn_lh/msedge-windows.png) |
| Chromium | [行为回执](../../tmp/path-format-browser-003qn_lh/chromium-path-format.json) · [Windows 设置截图](../../tmp/path-format-browser-003qn_lh/chromium-windows.png) |
| D 盘前端构建与隔离 API 退出 | [包和源码摘要](../../tmp/path-format-browser-003qn_lh/bundle-attestation.json) · [构建日志](../../tmp/ff-a13-path-format-20261119/build.log) |

复验入口为 [path_format_browser_acceptance.py](../server/path_format_browser_acceptance.py)。已有 [path-refs-smoke.mjs](../tools/path-refs-smoke.mjs) 的纯函数检查也通过。
