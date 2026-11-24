# CLI 快捷启动使用实际安装路径

## 2026-11-19 Codex WSL 兼容配置

工作台选中 `codex-wsl` 后，Codex 快捷按钮在单独的原生 WSL 终端启动它。后端提供的 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8` 和 `TERM=xterm-256color` 只覆盖 Codex 子进程。Codex 退出后，同一终端的 shell 及已打开的其他 WSL 终端均保留原环境。普通终端的“新建类型”仍保留用户选择，之后可继续直接新建 Windows cmd。

浏览器在 Edge 点击真实按钮，被动观察到快捷命令只发送一次。Linux 原生进程的 UID 为 65534，三个兼容变量与配置一致。终端中的 Codex TUI 使用用户已授权的 `deepseek-flash` 经官方 Responses 端点完成真实回合，实际调用 shell 读取算例内的随机文本，回复与文本逐字一致并留下原生回合完成事件。退出后，两个 WSL 会话和 cmd 会话均通过实际停止核验。

验收为 WSL CLI 使用独立可写的临时 home，复用本机已授权 API 凭据。它按原生 CLI 的配置方式连接模型，未更改实际用户的 CLI 配置。初次经过 Windows 回环转发的试验在发送请求时出现连接错误，后续使用 WSL 直接 HTTPS 成功。报告计完成的真实模型回合，原生直连的 HTTP 请求次数据此无法统计，模拟模型调用为 0。

SQLite 与文件系统、PostgreSQL 与真实 SeaweedFS 各完成一次交互式模型回合。后者首轮的测试驱动仅把部分字符送入 TUI，尚未提交模型请求。改为一次粘贴并检查完整草稿后，复验通过。复跑入口见 [live_llm_codex_wsl_profile_acceptance.py](../server/live_llm_codex_wsl_profile_acceptance.py)。

- SQLite 与文件系统的 [浏览器回执](../../tmp/codex-wsl-profile-hxf_f4p4/codex-wsl-browser.json)和 [真实回合回执](../../tmp/codex-wsl-profile-hxf_f4p4/codex-wsl-result.json)。
- PostgreSQL 与 SeaweedFS 的 [浏览器回执](../../tmp/codex-wsl-profile-b4r4j51h/codex-wsl-browser.json)、[真实回合回执](../../tmp/codex-wsl-profile-b4r4j51h/codex-wsl-result.json)及 [临时数据库清理记录](../../tmp/terminal-agent-postgres-bsvull_s/cleanup.json)。
- [回环试验失败截图](../../tmp/codex-wsl-profile-uqc1e6ji/codex-wsl-failure.png)及 [浏览器夹具初次失败](../../tmp/codex-wsl-profile-uaspw29q/codex-wsl-browser.json)。

## 2026-11-17 Windows 安装路径

2026-11-17。原终端按钮仅发送 `kimi`、`codex` 等短名称，即使能力接口已经发现管理员指定的文件，也仍依赖终端PATH查找；自定义安装不在PATH时会失败，或启动其他版本。

Windows快捷启动现在发送带双引号的实际发现路径，支持目录空格、中文和 `&`。显式配置、版本目录以及PATH发现统一返回绝对路径，避免API工作目录与算例工作目录不同导致相对路径失效。路径包含命令提示符会展开或无法正确表示的字符（`%`、`!`、双引号、换行、NUL）时，界面提示修正安装路径，不把它作为命令直接发送。

WSL和隔离沙箱仍在所选Linux环境中解析自身安装的CLI，不把Windows路径发送进去；发现的POSIX路径按shell单词引用。codex-wsl配置档保持WSL原生命令，环境变量值增加单引号转义并检查变量名称。本次没有完成这些Linux快捷启动的真实模型验收，也不将Windows证据替代它们。

## 实际浏览器验收

入口为 `server/live_llm_cli_launcher_acceptance.py` 与 `tools/live-cli-launcher-browser.cjs`。独立API、SQLite、文件存储和Vite复用本机环境，不改开发数据库。Kimi的已安装官方程序通过算例外的真实 `.cmd` 启动器执行；启动器位于 `CLI tools & 中文` 目录，未加入PATH，并通过相对 `VIBEFLOW_KIMI` 配置交给后端发现。启动器只传递测试用参数并执行已安装Kimi，不产生模型回复。

浏览器实际登录、选择算例、点击Kimi按钮；被动观察WebSocket确认仅发送一次完整路径。真实Kimi自主选择Read工具，读取提示词里没有的随机文件标识并回复；CLI退出0，源文件未变。然后实际点击关闭按钮，正式会话接口及持久停止证明确认关闭。模型凭据只传给API及其真实CLI子进程，不传给Vite或浏览器。

证据目录：`tmp/live-cli-launcher-458cj9ba/`。

- `launcher-result.json`：整轮passed=true，真实DeepSeek模型deepseek-flash，两次HTTP200、一次CLI超时重试，模拟0；API及Vite实际进程均退出。HTTP响应数不等于全部网络尝试数，未观察token用量。
- `browser-done.json`：实际发送路径、会话标识、界面关闭、桌面1600×1000及手机宽度390×844检查，无页面脚本错误。
- `launcher-desktop-before.png`、`launcher-desktop-complete.png`、`launcher-mobile.png`：实际Chromium页面，手机宽度无新增横向溢出；不宣称物理手机验收。
- `browser-failed.json`：首个浏览器驱动错误地按ARIA tab定位Dockview标签而超时，未启动CLI或模型。改用实际`.dv-tab`定位后复用同一API重试；原失败保留。

Playwright插件连接断开后，直接使用本机已有Playwright1.62.1及Chromium，未重新安装。没有替换网络响应、固定模型回复或模拟CLI推理。来源审查为 `tmp/N7-05-cli-launcher-browser-review-v2-20261117.json`，650个文件摘要。前端完整构建通过；构建仍提示既有大chunk。impeccable机械检测返回空问题列表，实际页面已查看。

## 交付范围与后续

该修改补齐FF-F54/F57/J25中的Windows自定义安装快捷启动实际流程，不完成整个多CLI配置/切换/取消/恢复矩阵。主线仍48/53，剩N4-05、N4-07、N7-01、N7-05、N7-06，N7-05原3组剩余条件不变。295项全功能仍按完整规则验收，严格整项关闭0、待整项关闭295；已有实现与局部验收并非0。

下一步核对CLI配置切换及备份恢复的现有实现和原交付要求，优先补齐可实际使用的完整流程；继续复用现有环境，不增加权限层或新的阶段退出条件。
