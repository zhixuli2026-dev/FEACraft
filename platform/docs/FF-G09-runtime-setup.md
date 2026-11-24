# FF-G09 运行环境设置验收

2026-11-19 已通过。使用真实 Microsoft Edge、独立 API 进程、SQLite 和文件存储，复用本机已安装的 Python、`vibeflow-linux` 与 OpenFOAM v2406。环境设置写入验收目录内的独立配置文件。[通过回执](../../tmp/runtime-setup-browser-fk_i8eba/runtime-setup-result.json) · [浏览器截图](../../tmp/runtime-setup-browser-fk_i8eba/runtime-setup-browser.png)

- 管理员通过 Setup 保存另一个已安装的 Python，浏览器显示该解释器的实际版本。保存与清除配置都触发真实能力刷新，数据库中的已完成原生探测记录与所选工具一致。API 进程退出后用同一独立配置重新启动，页面保留上次设置。将字段清空后恢复默认配置并重新探测。[编辑回执](../../tmp/runtime-setup-browser-fk_i8eba/runtime-setup-edit.json) · [重启回执](../../tmp/runtime-setup-browser-fk_i8eba/runtime-setup-restart.json)
- 配置中包含不存在的 Python 或格式非法的 OpenFOAM bashrc 时返回 HTTP 400，整批设置保留此前持久化值。普通成员保存时收到 HTTP 403，页面显示错误。填写格式合法但实际缺失的 OpenFOAM bashrc，以及选择允许但当前未安装的 WSL 发行版，页面均显示实际探测到的不可用状态。恢复已安装的设置后重新探测可用。[编辑回执](../../tmp/runtime-setup-browser-fk_i8eba/runtime-setup-edit.json) · [重启回执](../../tmp/runtime-setup-browser-fk_i8eba/runtime-setup-restart.json)
- 文件菜单复制真实算例文件的 Windows 和 POSIX 路径。恢复默认后提交实际 WSL 作业，作业从转换后的 `/mnt/d/.../run` 读取算例文件，完成回执包含已核验的进程停止证明。[通过回执](../../tmp/runtime-setup-browser-fk_i8eba/runtime-setup-result.json)

首次复现时，Setup 接受并写入了路径中含制表符的 OpenFOAM bashrc，原生探测校验会拒绝这个值。保存入口现在共用原生 WSL profile 的 bashrc 校验。格式有效但文件缺失的配置仍可保存，由实际探测报告不可用。[首次失败回执](../../tmp/runtime-setup-browser-vtkdyjf5/runtime-setup-result.json) · [首次写入的隔离配置](../../tmp/runtime-setup-browser-vtkdyjf5/runtime.json) · [修复](../server/app/services/runtime_config.py)

本轮浏览器验收限于当前 Windows/Edge 与 SQLite/文件存储组合。Pandoc 路径优先级、进程环境变量覆盖、Python/WSL 探测故障和完整 OpenFOAM 工程运行沿用既有验收。此次环境设置流程记录了 0 次真实模型调用、0 次模拟模型调用。[工具链部署和既有工程证据](toolchain-deployment.md) · [原生探测专项](owned-runtime-probes.md)

在项目根目录复跑，下例复用当天的前端产物和具备 Playwright 的本机 Python。

```powershell
& 'platform/server/.venv/Scripts/python.exe' -B -X utf8 `
  'platform/server/runtime_setup_browser_acceptance.py' `
  --dist 'tmp/codex-wsl-profile-20261119/web-dist' `
  --browser-python 'C:/ProgramData/anaconda3/python.exe' --channel msedge
```
