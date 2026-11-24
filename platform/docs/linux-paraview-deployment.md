# Linux ParaView 部署与实际后处理

2026-11-25，基于当前发布基础镜像构建可选版时，实际 `pvpython` 缺少 `libcurl.so.4`。可选 Dockerfile 明确安装 Debian 的 `libcurl4t64`，保留原归档及专用 `libglapi` 检查。重新构建后，`pvpython` 和 `pvbatch` 均识别为 5.11.2，双 API 的原生渲染、跨副本重连和关闭已实测。 [实际构建](../../tmp/ff-j14-rolling-replicas-20261125/build-optional.log) · [原生验收](FF-J14-rolling-replicas.md)

2026-11-18：新增 ParaView 5.11.2 OSMesa Linux 可选镜像和 Compose。Linux 从 PATH 发现 `pvpython`、`pvbatch`，求解入口与能力清单共用该逻辑；显式 `VIBEFLOW_PVPYTHON` / `VIBEFLOW_PVBATCH` 优先，错误路径不会静默回退。Windows 保留现有程序路径。最终验收记录见本文件下方。

## 构建与部署

```sh
python platform/tools/fetch_paraview.py
docker build -f platform/infra/Dockerfile.api -t vibeflow/api:current platform
docker build -f platform/infra/Dockerfile.paraview --build-arg API_IMAGE=vibeflow/api:current -t vibeflow/api:paraview platform
```

也可以用 `python platform/tools/fetch_paraview.py --archive <已下载的官方包路径>` 复用本地包。下载工具和 Dockerfile 均核验固定 SHA-256；网络中断时保留 `.part`，再次调用按 HTTP Content-Range 继续下载。完整构建输入保存在 `platform/.build-inputs`，被版本管理忽略，源码交付包仅含准备工具，不包含这份约 505 MiB 的第三方安装包。

与 `docker-compose.prod.yml` 配合使用 `docker-compose.paraview.yml`，`VIBEFLOW_API_IMAGE` 指定基础镜像，`VIBEFLOW_PARAVIEW_IMAGE` 指定最终镜像。API/worker 必须使用同一个结果。已有 OpenFOAM、TeX、CalculiX、SU2 镜像可作为基础层；多种工具层应依次构建，不能把多个 Compose 构建覆盖文件当作串行构建。

正式入口仍是 `POST /api/v1/cases/{case_id}/paraview:run`，请求包含 `script_path` 和 `backend`。任意脚本实际执行于作业私有输入副本，后续按现有注册处理器发布文件变化与删除。详见 [ParaView 发布契约](paraview-publication.md)。

## 官方包与运行依赖

包来自[ParaView 官方 5.11 下载目录](https://www.paraview.org/files/v5.11/)，文件为 `ParaView-5.11.2-osmesa-MPI-Linux-Python3.9-x86_64.tar.gz`，529753943 字节。固定 SHA-256 是本次官方 HTTPS 下载后计算并固定的摘要，未声称目录提供了发布者签名或独立摘要：

```text
657a44f53c61fb784340b65faa16e91a7b786e802820cf1a6aef35c4d30d4aeb
```

本机 curl 首次遇到 Windows 吊销查询离线；随后使用保持 TLS 校验的 Python HTTPS 下载。读超时后核验服务端 206 与 Content-Range，续传完成。原日志与分段记录在 `../../tmp/linux-paraview-20261118`。

首个 Linux 镜像在真实版本探测时缺少 `libglapi.so.0`，当前 Debian 13 软件源没有该实际包。固定使用 [Debian 12 libglapi-mesa 22.3.6-1+deb12u2](https://packages.debian.org/bookworm/amd64/libglapi-mesa/download)，校验 Debian 页面给出的 SHA-256 `2db5b13a6b7f4042f9a417ad7fa4338b6c47528dc64d7f89cfd5e53d07d76bc4`，将库及版权说明放入 ParaView 自己的目录。未替换系统 Mesa 或修改全局链接路径。镜像使用 `/opt/paraview/bin`，两个程序实际 `--version` 都返回 5.11.2。

## 真实流程与判断标准

使用上一阶段真实 SU2 产出的 NACA0012 VTK 文件，源摘要与来源记录在 `server/examples/paraview/naca0012-su2-origin.json`。通过正式二进制上传 API 写入独立算例，再由实际 ParaView 读取 5233 点、10216 单元的数据。

每个 backend 分别用倍率 1、2 计算 `PressureRatio=factor*Pressure/101325`，输出 VTU；提取 `Mach=1` 等值线，输出 VTP 和 CSV；OSMesa 或 Windows 渲染器输出 640×360 PNG。验收逐点比较真实计算字段与输入值公式，核验等值线 Mach、CSV 点数、图片尺寸和颜色变化，并人工查看取景。首次渲染重置相机的问题已通过在脚本中关闭自动首次相机重置修正，旧结果保留。

还核验动态 JSON 参数与源 VTU 冻结、含空格/百分号/与号的脚本路径、真实文件删除、全部成果摘要与当前别名、历史不可变文件、幂等提交、真实异常后的旧成果保留，以及根/子进程停止证明。异常算例先实际执行 ParaView，再在私有副本写入并抛出异常；没有用固定输出替代原生工具。

本轮真实模型 0、模拟 LLM 0，属于真实后处理与实际基础设施测试。完整交互工作区、插件、MPI 多节点、大数据/并发、跨主机与全恢复矩阵仍按原计划继续，本证据不代表它们已完成。

## 验收结果与交付

最终 v3 矩阵两端各 6 个真实作业，共 8 次成功、4 次预期异常。四次成功处理分别覆盖两个 backend 的两档倍率；全部根进程、ParaView 子进程及能力探测请求停止均已核验。

压力比与独立输入公式比较的最大绝对误差为 `4.44e-16`；声速等值线为 148 点、146 单元，Mach 均为 1。两端输出的压力比、Mach 与几何坐标逐点一致。Linux/Windows PNG 分别有 722/744 种 RGB 颜色，两端图片已实际查看，能清楚显示翼型和压力分布；不同渲染器不要求图片逐像素完全相同。

首轮 Windows 作业本身成功，验收脚本错误地把 `GetParaViewVersion()` 的字符串当作点分版本，其实际格式是 `(5, 11)`；已修正并保留 `../../tmp/windows-paraview-rrf2tczz`。v2 数值和完整流程通过，但人工发现取景过远，证据保留 `../../tmp/windows-paraview-e1nfxuri`；修正相机后重新执行 v3 完整矩阵。

- Linux 最终证据：`../../tmp/linux-paraview-20261118/linux-v3`。
- Windows 最终证据：`../../tmp/windows-paraview-ksky3370`。
- 两端字段比较：`../../tmp/linux-paraview-20261118/comparison.json`。
- 部署证据：`../../tmp/linux-paraview-20261118/deployment-result.json`、`deployed-toolchain.json`、`compose-check.json`。
- 汇总：`../../tmp/mainline-linux-paraview-evidence/validation.json`。
- 源码包：`../backups/release-review-linux-paraview-20261118.zip`。

S3 环境 API/worker 已部署 `vibeflow/api:linux-paraview-v3-20261118`，467 个应用模块与工作区一致，两个 backend 的 wrapper/ELF 摘要一致、动态库无缺失，历史三份 TeX PDF 摘要未变。默认/自定义 Compose 镜像选择均通过；文件存储环境保持既有部署。

主线 **49/53，剩 4 项**；资源 **11/17，剩 6 项**；全功能整项交付 **2/295，剩 293 项**。本批计入 N7-05 和 FF-D15，未将完整矩阵或整个功能项提前关闭。下一阶段先以真实 DeepSeek 验证已部署工程工具的模型驱动流程，再继续 Node/npm 和其余 CLI 部署缺口；Claude 已取消，不纳入新增部署或模型验收。
