# FF-A15 ParaView 宏脚本约定

2026-11-19。ParaView 5.11.2 原生宏菜单能保存、导入、编辑并运行 Python 宏。项目规划将 `.macros/` 用作算例下的脚本存档目录。网页将它接入现有可视化面板，A15 和 D19 共享同一入口。[ParaView 5.11.2 原生宏说明](https://docs.paraview.org/en/v5.11.2/Tutorials/ClassroomTutorials/pythonAndBatchParaViewAndPython.html#macros) · [原版机制核对](mechanism-audit.md) · [项目目录约定](plan-cs-to-bs.md)

## 使用与执行上下文

选中算例，在「可视化」展开「ParaView 宏」。列表从当前算例文件树读取 `.macros/` 下的 `.py` 文件。新建以文件不存在为条件保存，打开脚本复用普通编辑器，已有草稿需先保存再运行。执行后端可选 `pvpython` 或 `pvbatch`，浏览器按用户和算例记住脚本、后端与已受理作业。只读共享用户可以打开脚本查看。新建、运行和取消由原文件及作业接口核验写权限。[网页入口](../web/src/panels/ParaViewMacros.tsx) · [现有原生 ParaView 接口](../server/app/routers/paraview.py)

执行使用现有 `paraview:run` 的输入封存、独立运行目录、原生停止证明和版本发布。输出可从项目文件树在原生可视化代理中打开，其渲染会话独立于宏作业。已受理作业从同一作业 ID 恢复状态，返回丢失时重试使用同一提交键。可在作业面板查看错误、日志和恢复操作。[原生作业与发布](FF-D15-completion-review.md) · [ParaView 处理器](../server/app/services/paraview_handler.py)

## 实际验收

- Edge 新建并保存 `.macros/球面.py`，编辑器中存在未保存草稿时按钮不可运行。实际 `pvpython` 与 `pvbatch` 各生成一个含 130 点的球面 VTP。修改半径从 1.25 到 0.75 后，逐点最大误差分别为 `4.77e-8` 和 `2.77e-8`，两个不可变发布版本均与各自的实时输出逐字节相符。从文件树打开首版 VTP 时，真实原生可视化会话绑定同一路径且代理返回 200。[双后端及可视化回执](../../tmp/a15-paraview-macros-cq905r2y/macro-result.json) · [实际浏览器界面](../../tmp/a15-paraview-macros-cq905r2y/macro-preview.png)
- 真实提交通知丢失后，网页以原提交键查回同一个已受理作业。异常宏的真实日志保留失败原因，实时结果保持上一版。刷新浏览器重接仍在实际执行的宏后，原按钮取消原作业，私有就绪文件留在尝试目录。对活跃作业的取消与失败均核验原生停止范围。只读共享用户界面保留脚本选择和打开入口，运行与写入由接口拒绝。[浏览器回执](../../tmp/a15-paraview-macros-cq905r2y/macro-browser.json)
- 最终共四个实际 ParaView 作业，两成功、一预期错误、一取消。隔离 API 已退出。独立探测四个根、四个 ParaView 子进程和渲染会话的两份身份，均确认原始进程已经退出，其中一个 PID 已由更晚出生的进程重用。模型调用为 0。网页本轮使用隔离文件系统，S3 的原生批处理证据沿用 D15。[独立回收核验](../../tmp/a15-paraview-macros-cq905r2y/cleanup-independent.json) · [既有 S3 原生批处理](FF-D15-completion-review.md)

## 复验

复验入口使用实际 Edge、SQLite/文件系统隔离 API 和已安装的 Windows ParaView。入口通过模型审查后启动，测试数据和浏览器临时目录位于 D 盘。[复验脚本](../server/paraview_macro_browser_acceptance.py) · [生产构建](../../tmp/a15-paraview-macros-build-20261119.log) · [界面检测](../../tmp/a15-paraview-macros-impeccable-20261119.json)

```powershell
& 'platform/server/.venv/Scripts/python.exe' -B -X utf8 `
  'platform/server/paraview_macro_browser_acceptance.py' `
  --dist 'tmp/a15-paraview-macros-dist-20261119' `
  --browser-python 'C:/ProgramData/anaconda3/python.exe' --channel msedge
```

首轮浏览器的 45 秒等待先结束。SQLite 并行读取确认原作业继续完成，启动意图到发布可见耗时 57.77 秒，原生根进程已退出，实时与不可变 VTP 的 SHA-256 一致。该轮保留为浏览器未通过记录。[首轮浏览器界面](../../tmp/a15-paraview-macros-yr9anwcw/macro-browser-failure.png) · [独立作业与发布核验](../../tmp/a15-paraview-macros-yr9anwcw/actual-final-state.json)

第二轮新建请求收到 `storage_quota_busy`。第三轮模拟断连前的提交收到非 200，测试当时未保存具体状态。同一轮数据库记录原作业最终成功。最终入口只对实际 `storage_quota_busy` 做有时限的重试，确认实际 HTTP 200 后再模拟连接丢失，并记录了两次实际准入等待。[创建失败现场](../../tmp/a15-paraview-macros-szmhl1iv/macro-browser-failure.png) · [提交阶段未通过的运行记录](../../tmp/a15-paraview-macros-lsvxqv4z/macro-result.json) · [完整复验](../../tmp/a15-paraview-macros-cq905r2y/macro-result.json)
