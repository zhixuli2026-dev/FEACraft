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

## 2026-11-26 画面捕获分段复验

两套 Linux API 和 PostgreSQL 复测 50 个真实 ParaView 会话，其中五个加载 1,045,456 单元的网格。测试继续使用 480×360 原尺寸，JPEG 质量 50 和 100 各有 150 次移动。观察内核在执行真实移动后记录序号，只把同序号且编码完成的 JPEG 计入延迟。每个会话的六次移动均收到六张摘要不同的完成帧。 [基线原始回执](../../tmp/ff-d22-phase-20261126/stock-50.json)

| 试验 | 质量 50 完成帧 p95 | 质量 100 完成帧 p95 | 质量 50 首次捕获 p95 | 完成帧 | 核验停止 |
|---|---:|---:|---:|---:|---:|
| 默认，两会话 | 54.4 ms | 66.5 ms | 18.3 ms | 12/12 | 2/2 |
| 默认，50 会话 | 310.8 ms | 270.6 ms | 163.3 ms | 300/300 | 50/50 |
| 延后鼠标按下时的首帧捕获，50 会话 | 452.3 ms | 506.8 ms | 221.1 ms | 300/300 | 50/50 |

50 会话默认轮的质量 50 数据中，移动处理函数 p95 为 6.0 ms，进入渲染进程至首次捕获开始为 41.7 ms，首次捕获的进程 CPU 时间为 30.6 ms。从首次捕获结束到发现完整 JPEG 可发布为 73.8 ms，包含编码和调度重试。端到端时间扣除渲染进程内区间后的 p95 为 131.3 ms，覆盖请求入站及图像出站。各分位数分别计算。延后捕获的对照轮中，完成帧与首次捕获均更慢，产品继续使用默认调度。 [阶段统计](../../tmp/ff-d22-phase-20261126/phase-comparison.json) · [对照原始回执](../../tmp/ff-d22-phase-20261126/defer-down-50.json)

两个 50 会话试验各通过 100 次主动刷新、100 次方向轴操作、百万单元算例重连和 50 条进程范围为空的停止证明。原 11 个运行容器标识一致。模型调用 0 次，200 ms 目标继续验收。 [计时脚本](../../tmp/ff-d22-phase-20261126/measure.py)

## 2026-11-26 CPU 调度与向量宽度对照

同一宿主用两套 Linux API、50 个真实 ParaView 会话复测。45 个会话使用普通网格，5 个使用 1,045,456 单元网格。每轮用质量 50 和 100 各测 150 张匹配移动且编码完成的原尺寸 JPEG。三轮均通过每个会话六张不同完成帧、100 次主动刷新、100 次方向轴操作、百万单元会话重连，以及 50 份停止回执。 [实测入口](../../tmp/ff-d22-scheduling-20261126/measure.py) · [原始数据汇总](../../tmp/ff-d22-scheduling-20261126/comparison.json)

| 配置 | 质量 50 拖动 p95 | 质量 100 拖动 p95 | 质量 50 首次捕获 p95 | 质量 50 进程外区间 p95 | 峰值及回收后内存 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 默认 | 325.1 ms | 339.3 ms | 173.6 ms | 113.0 ms | 9.28 / 0.82 GiB |
| 渲染进程 nice +5 | 346.1 ms | 408.0 ms | 221.7 ms | 54.5 ms | 9.30 / 0.83 GiB |
| llvmpipe `LP_NATIVE_VECTOR_WIDTH=128` | 423.8 ms | 310.0 ms | 277.7 ms | 80.2 ms | 9.38 / 0.82 GiB |

降低渲染进程的调度优先级缩短了进程外区间，首次捕获的墙钟时间却上升 48.2 ms。首次捕获的 CPU 时间 p95 从 29.0 ms 变为 30.3 ms，说明这轮捕获变慢主要发生在 CPU 等待。128 位向量设置下，质量 50 的首次捕获 CPU 时间 p95 上升至 37.8 ms。默认配置在当前交互质量下最快，FF-D22 继续按 200 ms 验收。各分位数独立计算。 [默认回执](../../tmp/ff-d22-scheduling-20261126/stock-50.json) · [nice 回执](../../tmp/ff-d22-scheduling-20261126/nice5-50.json) · [128 位回执](../../tmp/ff-d22-scheduling-20261126/vector128-50.json) · [Mesa 参数](https://docs.mesa3d.org/drivers/llvmpipe.html#environment-variables)

原压测控制台的 `DRAG_P95_MS` 汇总质量 100。新检查器从原始完成帧独立核对两档样本、480×360 分辨率、同一组算例、跨实例重连及停止回收，并用浏览器实际使用的质量 50 判断原 200 ms 目标。缺失一张交互帧的回执已验证会被拒绝。三轮模型调用均为 0。原 11 个 Docker 服务均核对，构建缓存回收 0 B。Windows C 盘可用空间由 39,775,879,168 B 变为 39,772,327,936 B。 [验收检查器](../tools/validate_viz_load.py)

## 同机离屏渲染后端核对

2026-11-26，在现有 Docker Desktop 和 RTX 3060 上探测可用的离屏渲染路径。容器中的 `nvidia-smi` 识别到 12,288 MiB 显存。加载 WSL 的 D3D12 运行库后，当前 Linux 镜像的 Mesa D3D12 在 surfaceless 与 EGLDevice 两个入口均返回 `EGL_NOT_INITIALIZED`。同一探针用系统 Mesa 的软件 EGL 实际绘制并读回了像素，两种入口均报告 LLVM 19.1.7 的 llvmpipe。 [离屏探针回执](../../tmp/ff-d22-gpu-20261126/egl-result.json) · [WSL OpenGL 架构](https://devblogs.microsoft.com/commandline/wslg-architecture/)

当前交付镜像的 ParaView 5.11.2 在实际 `vtkOSOpenGLRenderWindow` 中报告 LLVM 7.0.0 的 llvmpipe。480×360 球体的十次旋转均完成渲染，单进程预检用时 12.9 至 14.0 ms。50 会话回执仍按原尺寸和实际拖动完成帧核对 200 ms 目标。 [当前镜像预检](../../tmp/ff-d22-gpu-20261126/osmesa-render-preflight.json) · [ParaView 的离屏构建选项](https://www.paraview.org/paraview-docs/v5.12.0/cxx/Offscreen.html)

本机 Windows 原生 ParaView 5.11.2 用同一球体完成十次旋转，`vtkWin32OpenGLRenderWindow` 报告实际使用 NVIDIA GeForce RTX 3060，每次渲染耗时 0.68 至 0.91 ms。该结果覆盖单进程离屏绘制。 [Windows GPU 预检](../../tmp/ff-d22-gpu-20261126/windows-render-preflight.json)

在临时镜像安装 `libopengl0` 后，官方 ParaView 5.11.2 EGL 包实测使用 `vtkEGLRenderWindow` 和 Mesa LLVM 19.1.7 llvmpipe。两会话对照的质量 50 完成帧 p95 分别为 OSMesa 53.7 ms 和 EGL 85.7 ms，两档质量各六张完成帧，原尺寸和两条停止回执齐全。 [OSMesa 两会话](../../tmp/ff-d22-gpu-20261126/stock-2.json) · [EGL 两会话](../../tmp/ff-d22-gpu-20261126/egl-2.json)

50 会话 EGL 轮包含五个百万单元算例，共取得 300 张原尺寸匹配完成帧，通过刷新、方向轴操作、跨副本重连，50 个进程范围均核验停止。浏览器质量 50 的完成帧 p95 为 322.0 ms，质量 100 为 306.3 ms，cgroup 内存峰值约 10.60 GiB，回收用时 48.81 秒。本机已有的 OSMesa 50 会话回执为 310.8 ms、270.6 ms 和 9.14 GiB。正式性能验收仍以 200 ms 为目标。模型调用 0 次。 [EGL 50 会话](../../tmp/ff-d22-gpu-20261126/egl-50.json) · [OSMesa 50 会话](../../tmp/ff-d22-phase-20261126/stock-50.json)

## Windows 原生 GPU API

2026-11-26，使用完整的官方 Windows ParaView 5.11.2，通过本机 API、独立 PostgreSQL 和实际 WebSocket 运行两轮双会话对照。两轮均确认渲染进程使用 RTX 3060，各取得 12 张原尺寸匹配完成帧。刷帧、方向轴切换、同 API 重连、打包的 Web Visualizer 页面与脚本读取，以及两份停止回执均通过。每轮有一个 1,045,456 单元的算例。 [官方安装包](https://www.paraview.org/files/v5.11/ParaView-5.11.2-Windows-Python3.9-msvc2017-AMD64.zip) · [安装包摘要](../../tmp/ff-d22-windows-20261126/official-windows-archive.json)

| Windows 双会话 | 两会话就绪 | API 进程树私有内存峰值 | 质量 50 完成帧 p95 | 质量 100 完成帧 p95 |
| --- | ---: | ---: | ---: | ---: |
| 原材质加载 | 15.48 s | 2,059,956,224 B | 76.601 ms | 85.178 ms |
| 延迟加载纹理 | 6.84 s | 1,374,871,552 B | 61.532 ms | 81.190 ms |

延迟加载后，单独验证了原厂材质库的 Checker_Alpha 纹理路径、加载及 OSPRay pathtracer 实际出图。Windows 内存字段为进程树私有字节。以上实测调用模型 0 次。正式验收仍需 50 会话、五个百万单元算例和质量 50 p95 小于 200 ms。 [原材质回执](../../tmp/ff-d22-windows-20261126/run-304adc63/result.json) · [延迟加载回执](../../tmp/ff-d22-windows-20261126/run-f3b0fa9b/result.json) · [光线追踪回执](../../tmp/ff-d22-windows-20261126/windows-raytrace.json)

配置 Windows 原生可视化时，将 API 的 ParaView 路径指向完整发行包的 `bin/pvpython.exe`。若另行部署同版本的 Web Visualizer 静态资源，在 API 环境中将 `VIBEFLOW_PARAVIEW_VISUALIZER_DIR` 设为包含 `index.html` 和 `Visualizer.js` 的绝对目录。

## Linux 可视化会话内核额度

ParaView 和 Trame 复用 Linux 作业的内核启动门。启用额度时，API 在持久登记前核对所需的控制器环境和挂载权限。每个会话冻结 CPU 累计时限和内存字节上限，登记该会话的 PID 命名空间后才放行渲染器。经核验的停止回执随会话返回 `stop.linux_compute_usage`，含配置、启动序号、实际 CPU 纳秒、内存峰值和 OOM 计数。CPU 与内存超额分别返回 `cpu_limit`、`memory_limit`。启用条件不满足时返回 `viz_compute_unverified`。 [部署配置](deployment.md#可视化会话内核额度) · [验收入口](../server/linux_viz_compute_acceptance.py)

2026-11-26 在 Docker 的 Linux cgroup v1 私有命名空间及实际 PostgreSQL 上验收。两个账户的 ParaView 会话在同一 API 实际并行出图。负载会话出图 202 次后，在累计 6.026 秒 CPU 时触发 6 秒额度并核验退出。另一账户仍可出图，主动关闭的回执记录 2.739 秒 CPU 和 163,168,256 字节内存峰值。对端 API 从数据库读回一致的回执，其他账户访问返回 404。

48 MiB 内存及交换空间上限在真正启动 ParaView 时产生 1 次内核 OOM。创建返回 `viz_memory_limit`，已关闭历史记录保留 50,331,648 字节峰值及完整停止证明。Trame 完成真实启动和页面代理读取，主动关闭后记录 2.152 秒 CPU 与 228,167,680 字节峰值。默认额度为 0 的独立 ParaView 实测正常启停。缺少控制器挂载权限时，复验确认 API 在登记前拒绝，未占用会话名额。 [最终回执](../../tmp/j16-viz-compute-sb6fr85m/result.json)

五条渲染停止记录均通过范围核验，所有试验 API 的内核控制组和渲染器进程读数回到零。原 11 个 Docker 服务的完整 ID 一致，试验容器、网络与卷已清理。模型调用 0 次。复用已安装镜像，构建缓存回收 0 B，C 盘可用由 39,805,661,184 B 变为 39,822,991,360 B。此次计量为 FF-J16 的渲染入口验收。 [磁盘回执](../../tmp/ff-j16-viz-usage-20261126/disk-after.json) · [Docker 占用](../../tmp/ff-j16-viz-usage-20261126/docker-after.txt)
