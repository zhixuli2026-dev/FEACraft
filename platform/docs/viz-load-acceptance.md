# 可视化容量和交互延迟

FF-D22 继续验收。Windows 通过 Docker 运行两套 Linux API 和共享 PostgreSQL。每个虚拟用户由独立的真实应用账号、算例、原生渲染进程和 WebSocket 构成。部分连接经过另一副本。大网格由 ParaView 生成，包含 522,730 点、1,045,456 单元，VTP 文件 46,003,630 字节，经项目分块上传入口传入算例。

| 实际场景 | 并发会话 | 结果 |
| --- | ---: | --- |
| 50 会话首次容量尝试 | 35 | 容量接近上限后结束隔离运行，两套 API 容器合计约 13.4 GiB |
| 两会话，普通与百万单元各一 | 2 | 强制全分辨率出帧 p95 276.4 ms，重连和两份停止证据通过 |
| 两会话，软件驱动对照 | 2 | `softpipe` 强制出帧 p95 1,142.7 ms |
| 8 会话，含百万单元网格 | 8 | 实际鼠标拖动 p95 61.4 ms，全分辨率出帧 p95 303.5 ms，8/8 签发停止证据 |

8 会话运行期间，两套 API 的 cgroup 峰值约 4.32 GB。其后可视化进程数回到 0，两套 API 的 cgroup 内存回到约 0.45 GB。关闭一次，其他七个由空闲回收完成，约 30.9 秒读回全部退出证据。200 ms 的交互目标已在 8 并发下达到，50 并发的容量和 p95 仍待恢复验收。真实模型 0 次，模拟 0 次。 [8 会话回执](../../tmp/FF-D22-viz-load-20261123/load-8-default.json) · [首次容量快照](../../tmp/FF-D22-viz-load-20261123/capacity-stop-docker.txt) · [两会话对照](../../tmp/FF-D22-viz-load-20261123/load-2-softpipe.json)

Docker 占用和 Windows 系统盘实际可用空间分别采样。首次容量试验触及本机 Docker 虚拟机内存预算，之后移除试验专用容器、网络和数据卷。 [Windows 前](../../tmp/FF-D22-viz-load-20261123/before-c.json) · [Windows 后](../../tmp/FF-D22-viz-load-20261123/after-c.json) · [Docker 前](../../tmp/FF-D22-viz-load-20261123/before-docker.txt) · [Docker 后](../../tmp/FF-D22-viz-load-20261123/after-docker.txt)

## 2026-11-24 内存分解复验

沿用两个实际 Linux API、共享 PostgreSQL、两种网格和原始 ParaView 图像协议。首轮压测夹具沿用旧的查询凭据，被当前接口拒绝。夹具已改用正式短期连接票据和 HttpOnly 会话 Cookie。实际连接覆盖拥有实例和另一副本。两轮各 8 个会话全部取得图像，断线重新连接后再次取图，8/8 Stop 证明均签发。 [默认分配器回执](../../tmp/ff-d22-memory-20261124/load-8-default.json) · [分配器参数对照](../../tmp/ff-d22-memory-20261124/load-8-trim.json)

| 分配器设定 | 拖动 p95 | 强制全分辨率 p95 | API cgroup 峰值 | 匿名内存峰值 | 回收后 cgroup |
| --- | ---: | ---: | ---: | ---: | ---: |
| 默认 | 50.8 ms | 260.2 ms | 4.332 GB | 4.172 GB | 0.460 GB |
| 限制 arena 与释放阈值 | 52.2 ms | 262.2 ms | 4.276 GB | 4.117 GB | 0.448 GB |

最大文件缓存约 93 MB。默认轮渲染进程的私有内存峰值约 3.663 GB，按共享页分摊的 PSS 峰值约 3.859 GB。分配器参数对照使 cgroup 峰值减少约 1.29%，仍不足以支撑原 50 会话目标。两个隔离环境已全部释放，当前服务继续采用原参数。模型调用为 0。 [进程和 cgroup 采样器](../../tmp/ff-d22-memory-20261124/sample-linux-memory.py)

## 2026-11-25 延后解码 OSPRay 材质纹理

独立的 `pvpython` 进程将 ParaView 5.11.2 的会话初始化分开计时。在打开任何数据集前，原生 `InitializeSession` 使匿名内存从 61.8 MiB 增至 353.2 MiB。隔离对照通过 ParaView 的材质搜索路径先加载空库，完成同一个 480×360 首帧时匿名内存从 407.3 MiB 减到 120.7 MiB。随后通过 ParaView 原生接口加载原库，升至 410.5 MiB。三张普通渲染 PNG 的 SHA-256 相同。关闭 MPI 仅减少约 5 MiB。 [原生初始化调用](https://github.com/Kitware/ParaView/blob/v5.11.2/Remoting/ServerManager/vtkSMParaViewPipelineController.cxx#L272) · [材质搜索路径](https://github.com/Kitware/ParaView/blob/v5.11.2/Remoting/Views/vtkPVMaterialLibrary.cxx#L44) · [原材质对照](../../tmp/ff-d22-renderer-profile-20261125/stock-materials.json) · [延后加载对照](../../tmp/ff-d22-renderer-profile-20261125/cold-materials.json)

Linux 原生实时视图在启动时注册已安装库中的材质名称和参数。编辑光线追踪或 OSPRay 材质属性时，从原库加载实际纹理。Windows 和显式指定材质搜索路径的部署使用供应商初始化，离线 ParaView 工程作业继续调用原入口。需要恢复启动时加载全部纹理时，在 Linux API 的部署环境显式设置 `PV_MATERIALS_PATH` 指向安装版的材质目录。 [实时视图入口](../server/app/services/viz_paraview_kernel.py)

| 实际数据 | 原首帧匿名内存 | 调整后首帧匿名内存 | JPEG 核对 |
| --- | ---: | ---: | --- |
| 普通 VTP | 424.3 MiB | 137.7 MiB | 14,372 B，哈希一致 |
| 1,045,456 单元 VTP | 522.8 MiB | 236.4 MiB | 14,103 B，哈希一致 |

均经真实上传、短时连接票据和对端 API 渲染、拖动及重连。重连时主动发起一次新的拖动并核对图像，早期按原视角等待重复帧的超时保留在回执。两种数据退出时均读回已核验停止状态，进程计数回到零。真实 WebSocket 修改到 OSPRay pathtracer 并选择 `Checker_Alpha` 后出图，进程匿名内存由 137.8 MiB 升至 451.9 MiB。模型调用 0 次。 [原生命周期](../../tmp/ff-d22-renderer-profile-20261125/memory-lifecycle-stock.json) · [调整后生命周期](../../tmp/ff-d22-renderer-profile-20261125/memory-lifecycle.json) · [启用光线追踪](../../tmp/ff-d22-renderer-profile-20261125/memory-raytrace.json) · [原视角超时](../../tmp/ff-d22-renderer-profile-20261125/unchanged-frame-reconnect.log)

## 50 会话容量

两套实际 Linux API 共用隔离 PostgreSQL 和原生文件卷。50 个独立账号各上传 VTP 并启动原生 ParaView。45 个会话使用普通网格，5 个使用百万单元网格。首轮 50/50 均取得实际 JPEG，创建阶段用时 96.0 秒，随后在内存采样中同时观察到全部 50 个渲染范围。两套 API 的 cgroup 内存合计峰值 9.06 GiB。重复请求同一视角之后收到 WebSocket 关闭，首轮未生成完整的交互延迟和空闲回收回执。原 11 个 Docker 服务继续运行，Docker 构建缓存回收 0 B，Windows C 盘可用从 41,248,710,656 B 变为 41,218,097,152 B。 [容量回执](../../tmp/ff-d22-renderer-profile-20261125/load-50-first-frame.json) · [重复帧失败记录](../../tmp/ff-d22-renderer-profile-20261125/load50-repeated-frame.log)

第二轮 50/50 仍同时取到首帧，启动用时 349.1 秒。面向方向轴的连续刷新请求中出现等待帧超时，保留失败回执。容量与持续交互是独立观测。 [第二轮回执](../../tmp/ff-d22-renderer-profile-20261125/load-50-refresh-timeout.json) · [刷新超时](../../tmp/ff-d22-renderer-profile-20261125/load50-refresh-timeout.log)

第三轮让 50 个已有会话同时拖动，每个会话发送三次实际相机操作，150/150 次收到确认和 JPEG，无交互错误。拖动到返回画面的 p95 为 316.7 ms，原计划的 200 ms 目标仍待达到。百万单元会话断开末端连接后再次挂接，并完成相机操作。结束阶段并发查询状态收到 HTTP 500，因此本轮的 50 个空闲回收记录未闭合。两次主动刷新试验的原始超时记录继续保留。 [拖动与重连回执](../../tmp/ff-d22-renderer-profile-20261125/load-50-drag.json) · [状态查询失败日志](../../tmp/ff-d22-renderer-profile-20261125/load50-drag.log)

在共用 PostgreSQL 的两个 API 中用同一条真实渲染退出记录复现状态查询问题。旧副本在鉴权占用连接时另取连接查询状态，连接池日志明确记录超时。50 个并发请求中 46 个返回 HTTP 500，整批 30.266 秒。修复后的详情和列表复用鉴权请求的数据库会话，两批各 50/50 返回 HTTP 200，耗时分别为 0.421 秒和 0.313 秒。两种请求均核对会话已停止，外账号仍返回 404。 [状态路由](../server/app/routers/viz.py) · [数据库连接池实测](../../tmp/ff-d22-renderer-profile-20261125/status-pressure.json)

修复后再次完整执行。50 个原生会话在 97.0 秒内同时就绪，150/150 次实际拖动收到图像，百万单元会话重连后再次读到图像。一个会话主动关闭，另外 49 个进入 35 秒空闲回收，46.27 秒时查齐 50 个停止回执，渲染范围归零。两套 API 的 cgroup 内存从峰值 8.93 GiB 回落至约 0.49 GiB。拖动 p95 为 357.3 ms，持续刷新和原 200 ms 性能目标继续验收。模型调用 0 次。隔离资源退出后原 11 个 Docker 服务继续运行，构建缓存回收 0 B，C 盘可用由 41,210,818,560 B 变为 41,209,757,696 B。 [完整复跑回执](../../tmp/ff-d22-renderer-profile-20261125/load-50-default.json) · [运行日志](../../tmp/ff-d22-renderer-profile-20261125/load50-status-fixed.log)

```powershell
& platform/server/.venv/Scripts/python.exe -B -u tmp/ff-d22-renderer-profile-20261125/check-load.py 50
```

从项目根目录运行。依赖现有 `vibeflow/api:j14-release-paraview-20261125`、`postgres:16-alpine`、`redis:7-alpine` 镜像及前序验收留存的两份 VTP。脚本复制当前源码到专用只读挂载卷，为两套 API 建立隔离数据和账号，运行后清理测试容器、数据卷与网桥。 [压测入口](../../tmp/ff-d22-renderer-profile-20261125/check-load.py) · [实际内存采样器](../../tmp/ff-d22-renderer-profile-20261125/sample-linux-memory.py)

## 主动刷新与图像发布

在相同的 50 会话规模下，分别采集 wslink 请求确认、JPEG 和图像发布消息。旧版两轮主动刷新中有 3 次在 31–128 ms 内收到确认，6 秒内均未收到 JPEG 或发布消息。两轮方向轴操作均收到图像。主动刷新由 ParaView 原生协议从编码器读取图像，取到的版本号与上次发布相同时会抑制重复发布。显式刷新现在使用零版本号触发发布，并允许本次请求在动画期间执行。编码完成后的新图像仍通过原有推送机制发布。 [源协议](https://github.com/Kitware/ParaView/blob/v5.11.2/Web/Python/paraview/web/protocols.py) · [编码器接口](https://github.com/Kitware/ParaView/blob/v5.11.2/Clients/Web/vtkPVWebApplication.cxx) · [改造前](../../tmp/ff-d22-viz-refresh-20261125/burst-50-before.json) · [实时视图](../server/app/services/viz_paraview_kernel.py)

修复后两轮主动刷新 100/100、两轮方向轴操作 100/100，均收到确认、JPEG 和发布。普通和百万单元会话分别跨副本重连，相机改变后显式刷新获得不同哈希的已完成帧。50 个会话的停止证明再次齐全。 [修复后](../../tmp/ff-d22-viz-refresh-20261125/burst-50-refresh-fixed.json) · [新帧复验](../../tmp/ff-d22-viz-refresh-20261125/probe-fixed.json)

安装包实际拖动使用 JPEG 质量 50、原尺寸比例 1。修复后，在 480×360 画面上，质量 100 和 50 各执行 150 次同步拖动，p95 分别为 341.1 ms 和 363.5 ms。保留原 200 ms 目标。本机检测到 RTX 3060，现有容器带 GPU 设备参数实际运行 VTK EGL，报告渲染器为 Mesa llvmpipe。 [质量对照](../../tmp/ff-d22-viz-refresh-20261125/burst-50-refresh-fixed.json) · [EGL 真实出图](../../tmp/ff-d22-viz-refresh-20261125/egl.log)

隔离源码对照让鼠标移动直接触发图像采样，质量 100 和 50 的 p95 分别为 396.2 ms 和 426.6 ms。工作源码采用上文的显式刷新修复。设备调试中，直接选择 D3D12、只读挂载 WSL 图形库和驱动、指定 EGL 无窗口平台的试验均在初始化退出。阶段内 Docker 构建缓存回收 0 B，Windows C 盘可用由 41,209,151,488 B 变为 40,834,306,048 B。原 11 个运行容器的标识逐项一致，本轮模型调用 0 次。 [立即采样对照](../../tmp/ff-d22-viz-refresh-20261125/burst-50-eager.json) · [D3D12 初始化](../../tmp/ff-d22-viz-refresh-20261125/egl-mounted.log) · [阶段空间读数](../../tmp/ff-d22-viz-refresh-20261125/disk-after.json)

## 鼠标移动到完成编码的图像

隔离内核为真实移动 RPC 和 JPEG 发布附加同一编号。压测收到匹配编号的 wslink 确认，再以该移动对应的第一张非 `stale` JPEG 计算出图时间。保留首个发布时刻用于对照，分辨率保持 480×360，网格和质量沿用上面的试验。ParaView 会在捕获视图后异步编码，可在新图完成前读取上一张。 [原生编码实现](https://github.com/Kitware/ParaView/blob/v5.11.2/Clients/Web/vtkPVWebApplication.cxx#L145) · [验收入口](../../tmp/ff-d22-cadence-20261125/measure.py)

默认调度下，两个会话以实际浏览器质量 50 运行的 6 次移动均获得完成帧，p95 为 50.9 ms。50 个会话同一质量的 150 次移动全部取得完整 JPEG，p95 为 357.7 ms，确认 p95 为 135.8 ms。同一请求从确认至匹配完成帧的 p95 为 284.8 ms。有 52 次先收到了另一条发布消息。50 个会话同时在场，100 次主动刷新、100 次方向轴操作、百万单元会话重连及 50 条停止证明齐全。 [两会话](../../tmp/ff-d22-cadence-20261125/stock-2.json) · [50 会话](../../tmp/ff-d22-cadence-20261125/stock-50.json)

隔离对照将编码未完成后的重试间隔从 100 ms 缩短为 30 ms。质量 50 的匹配完成帧 p95 为 352.8 ms，质量 100 为 356.1 ms，默认轮相应为 357.7 ms 与 345.7 ms。两轮的四种刷新批次都取齐，全部渲染范围均已回收，cgroup 内存均从约 9.28 GiB 峰值回落至 0.81 GiB。单改重试间隔没有稳定降低交互延迟。 [30 ms 对照](../../tmp/ff-d22-cadence-20261125/retry30-50.json) · [分段结果](../../tmp/ff-d22-cadence-20261125/timing-comparison.json)

按鼠标事件调度且沿用异步 JPEG 的初版，在两会话中有 11/12 次取得对应完成帧。百万单元网格首个移动收到了两张图像和两次发布，等待匹配的完成帧超时，两个会话随后正常退出。改用实际 VTK 捕获及原尺寸 JPEG 同步编码的试验，12/12 次移动取得对应完成帧，质量 50 的 p95 为 28.2 ms。百万单元会话重连和两条停止记录通过。 [异步编码试跑](../../tmp/ff-d22-cadence-20261125/event-2.json) · [同步编码试跑](../../tmp/ff-d22-cadence-20261125/event-sync-2.json)

三轮 50 会话均有 300/300 次移动取得匹配的完成帧，每轮共 100 次主动刷新、100 次方向轴操作，百万单元会话跨副本重连，50 个渲染范围退出。三轮均使用同一台 4 物理核、8 逻辑线程宿主和 llvmpipe 软件渲染。 [完成帧分解](../../tmp/ff-d22-cadence-20261125/timing-comparison.json)

| 50 会话试验 | 质量 50 完成帧 p95 | 质量 100 完成帧 p95 | 质量 50 请求确认 p95 | 峰值及回收后 cgroup 内存 |
| --- | ---: | ---: | ---: | ---: |
| 默认调度 | 357.7 ms | 345.7 ms | 135.8 ms | 9.27 / 0.81 GiB |
| 重试间隔 30 ms | 352.8 ms | 356.1 ms | 154.0 ms | 9.28 / 0.81 GiB |
| 按移动触发并同步编码 | 408.2 ms | 390.8 ms | 187.5 ms | 9.45 / 0.81 GiB |

工作源码采用已验证的默认调度。同步编码的小样本改善未延续到 50 会话，原 200 ms 目标仍待达到。试验内核的代码加载和停机通过现有会话管理；原 11 个运行容器身份一致，临时栈、卷和网络均已回收。模型调用 0 次，Docker 构建缓存回收 0 B。C 盘可用由 40,710,467,584 B 变为 40,725,213,184 B。 [同步编码 50 会话](../../tmp/ff-d22-cadence-20261125/event-sync-50.json) · [C/D 盘](../../tmp/ff-d22-cadence-20261125/disk-after.json) · [Docker 占用](../../tmp/ff-d22-cadence-20261125/docker-after.txt)

## 50 会话的软件渲染线程对照

沿用两套 Linux API、普通与百万单元网格、480×360 画面和匹配完成帧的计时，隔离比较 llvmpipe 的 `LP_NUM_THREADS=0` 与已有 `=1` 回执。两种 JPEG 质量各 150 次，共 300/300 次取得原尺寸完成帧。质量 50 的 p95 从 357.7 ms 变为 403.3 ms，质量 100 从 345.7 ms 变为 397.9 ms。此次设置未带来改善，继续采用现有设置。 [Mesa 参数](https://docs.mesa3d.org/envvars.html#llvmpipe-driver-environment-variables) · [数据对照](../../tmp/ff-d22-llvmpipe-20261125/comparison.json) · [本轮原始回执](../../tmp/ff-d22-llvmpipe-20261125/llvmpipe-0-50.json)

50 个会话的刷新、百万单元重连及停止记录齐全。回收用时 45.61 秒，渲染进程归零。两套 API 峰值约 9.34 GiB，回收后约 0.82 GiB。原 11 个容器身份一致，隔离容器、网络和数据卷已回收。模型调用 0 次。C 盘可用由 40,691,417,088 B 变为 40,689,172,480 B，Docker 构建缓存 5.819 GB，回收 0 B。 [运行日志](../../tmp/ff-d22-llvmpipe-20261125/llvmpipe-0-50.log) · [Windows 可用空间](../../tmp/ff-d22-llvmpipe-20261125/disk-after.json) · [Docker 占用](../../tmp/ff-d22-llvmpipe-20261125/docker-after.txt)
