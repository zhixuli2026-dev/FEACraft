# 原生编译必须具有明确所属通道

2026-11-03，N4-03.h 的旧 worker 与条件回退核对。正式 HTML/DOCX、LaTeX 请求此前已经使用所属执行器；本批没有声称复现了正式 HTTP 路由的无归属执行。发现并移除的是内部辅助函数在缺少通道时仍直接运行程序的隐式分支，以及已不被正式路由使用的旧 worker。

## 调用关系核对

对当前 `app` 下 236 个 Python 文件进行 AST 扫描，并记录关键调用者的源码、行号和 SHA：

| 路径 | 归属关系 |
|---|---|
| 报告/LaTeX 提交准备 | `complete_preparation → NativeRequest.scope → RequestExecutor.run_sync → NativeExecutor.local`，随后才解析 Markdown 或探测 LaTeX |
| 注册 worker | 固定 `registered_report_worker` / `registered_latex_worker` 入口使用 `require_supervision()`，只读取为本次 worker 分配的私有通道 |
| 独立发布复验 | `JobProcess._run` 保持 `self._children.local()`；`artifact_publication.prepare_for_finish` 经复制 ContextVar 的线程调用 `handler.artifacts`，复编译仍回到父执行器 |
| 提交锁内的报告来源重验 | `ReportHandler.source` 使用已选 `known_files` / `known_resolution`，不会重新启动 Pandoc 解析 |
| 旧报告 worker / 快照提交辅助 | 没有正式 app 调用者；历史纯文件快照测试仍保留，不能代表当前注册发布协议 |
| CLI 发现 | runtime router、runtime registry、Codex session 的共 4 处调用都未提供 WSL runner，保持文件发现 |

报告参数中的 `standalone` 表示普通文档而非工程报告，绝不表示允许无归属地启动原生程序。

静态核对脚本与结果在 `tmp/audit-native-entry-reachability.py` / `tmp/native-entry-reachability.json`，副本纳入本批证据。它验证显式调用、导入和固定 worker 字符串，不证明任意反射、用户 Python 或远程程序的行为；实际准备、worker 和复验能力另由真实工具回归确认。

## 行为改变

`native_execution_client.channel()` 现在要求已选择的所属上下文。显式注册 worker 可以在 `require_supervision()` 范围内读取专用环境变量；环境中恰好存在地址和 capability，不能自行开启执行。地址仍必须为有效的 localhost TCP 通道，capability 格式继续严格校验。

没有上下文、通道格式错误、连接失败或通道中断都会失败，不再返回 `None` 让调用者选择直接启动。Pandoc 解析/编译/版本查询、LaTeX 编译在创建临时工作副本前检查通道；原直接 `subprocess.run` 回退已移除。真实执行仍经过原 NativeRequest / NativeExecutor、持久子执行登记和退出等待。

旧 `report_worker.py` 保留为明确拒绝运行的入口，提示通过 API 提交注册报告；不会创建 `out` 或启动 Pandoc。当前正式 API、已发布历史文档以及注册 worker 的功能保持原契约。内部辅助函数的独立调用方必须建立所属执行器，不能再依赖缺省回退。

CLI 发现移除了默认原生 runner。`None` 表示只查文件；显式 callback 必须可调用，`False`、`0` 等不再意外启用直接 WSL 探测。返回假布尔值但本身可调用的对象仍按明确提供的 callback 使用。正式 API 没有开放 callback 参数；后续真实 WSL 自动发现必须接入有归属的调用方。

## 验证与修正记录

修复前证据用替身拦截 `subprocess.run`，确认缺少上下文时 Pandoc/LaTeX 会尝试无归属启动，`wsl_runner=False` 会选择默认 WSL runner。该复现没有实际启动这些工具，也不将其表述为正式 HTTP 绕过。

`native_channel_smoke.py` 的 14 项边界检查覆盖缺少/错误通道、环境变量不能自行授权、上下文退出、三种 Pandoc 操作、LaTeX 探测/编译、退役 worker、连接失败。测试禁止任何 `subprocess.run/Popen`，并核对源文件和目录清单未变。

`cli_discovery_smoke.py` 的 7 项覆盖显式路径、版本目录及预发布排序、PATH 后缀、缺失显式路径拒绝、明确 WSL callback 与无效 callback、隔离认证后的 capability API。该套件现使用一次性账户/API/存储，已移除对开发账号和端口 8000 的依赖。

报告快照测试改为在 `pandoc_execution.run` 边界提供完整的 `CompletedProcess`，继续测试真实文件冻结、来源变化和禁止部分发布；不再通过替换全局 `subprocess.run` 依赖已移除的隐式分支。

首轮真实 LaTeX 与文档准备测试发现本批误删了仍用于 SyncTeX 映射和日志归一化的 `win_to_wsl_path` 导入。已恢复该导入，并重跑受影响检查；失败日志保留。最终通过结果按原始与重跑日志分别记录，不把最初的失败汇总改写成通过。

规范证据：`tmp/mainline-N4-native-channels-evidence/`。`evidence-origin.json` 列出实际 LaTeX/Pandoc 双库、原生准备、文档准备、普通文档、快照和 CLI 发现的路径及具体结果。源码基线：`platform/backups/mainline-N4-native-channels-20261103.zip`，同名 `.sha256` 和证据中的逐文件匹配用于校验；没有恢复后重跑。

```powershell
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/native_channel_smoke.py
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/cli_discovery_smoke.py
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/native_execution_smoke.py --postgres
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/pandoc_execution_smoke.py --postgres
```

本批没有数据库或 UI 修改，开发库与已有服务未迁移/重启，没有远端推理。N4-03 继续核对专业入口剩余竞争、FloTHERM 复制分支与跨运行时边界；N4-04/05/06/07 及 N5 保持各自任务，不因该入口核对关闭整个阶段或任何 FF 整项。
