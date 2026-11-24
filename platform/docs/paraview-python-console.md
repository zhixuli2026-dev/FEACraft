# 算例 Python 控制台（FF-D08）

算例所有者在「可视化 → ParaView 来源与过滤器」点「打开 Python 控制台」。终端新建一个绑定当前算例的 WSL 沙箱会话并启动交互解释器。`case_dir` 指向算例副本 `/case`，装有 ParaView 时 `simple` 指向实际 `paraview.simple` 模块。支持正常导入、变量、函数、循环和多条命令。浏览器刷新后，可从终端列表重连原会话并继续使用已建立的变量。

使用 Ctrl+C 或「中断代码」终止当前执行，解释器保留上下文。通过终端标签关闭会话并核对原生进程退出。结果文件沿用「恢复文件」中的算例沙箱保存流程。

```python
mesh = simple.Sphere(ThetaResolution=12, PhiResolution=10)
mesh.UpdatePipeline()
print(mesh.GetDataInformation().GetNumberOfPoints())
simple.SaveData(str(case_dir / "D-post" / "sphere.vtp"), proxy=mesh)
```

## 当前运行环境

验收环境为 Windows API 与 `vibeflow-linux` WSL 发行版。ParaView 5.11.2 安装在 WSL 的 `/opt/vibeflow-paraview-5.11.2`，该发行版的数据盘位于 D 盘。复用项目已校验的官方 OSMesa 包，以及既有 Linux 镜像中验证过的 `libglapi.so.0`。沙箱只读挂载这个已安装目录；缺少该目录的 WSL 环境使用系统 Python。Linux-only 部署的 ParaView 作业通过既有 `pvpython` 和 `pvbatch` 入口执行。 [Linux 工具链来源和摘要](linux-paraview-deployment.md)

## T06 实测

Edge 在真实 WSL 沙箱使用 UID 65534，读取启动时的算例文件，生成 98 点球体，核对宿主路径不可见和工具链只读。两段输出间隔约 1.09 秒。执行 `1/0` 后可继续，Ctrl+C 和「中断代码」均返回 `KeyboardInterrupt` 并保留变量。刷新页面重连后变量仍为原值。最后通过终端状态 API 核验归属进程退出。实测调整了原生 ParaView 的 SIGINT 处理，使之继续使用 Python 中断后的交互流程。 [验收记录](../../tmp/FF-D08-python-console-20261123/d08-acceptance.json) · [终端截图](../../tmp/ff-d08-python-edge-s0k64kjj/python-interactive.png)

本次工具链部署前后分别采样 Windows 文件系统和 Docker 内部占用。WSL 程序写入 D 盘，本次未手动回收 Docker 缓存。 [Windows 前](../../tmp/FF-D08-python-console-20261123/before-storage.json) · [Windows 后](../../tmp/FF-D08-python-console-20261123/after-storage.json) · [Docker 前](../../tmp/FF-D08-python-console-20261123/before-docker.txt) · [Docker 后](../../tmp/FF-D08-python-console-20261123/after-docker.txt)
