# 工具链部署、配置和恢复

2026-11-18 Gmsh：正式网格处理器新增 Gmsh 三维来源；Linux/WSL 完整热传导、MSH/VTU 预览、发布过期与恢复、真实 DeepSeek 工具读取已验收，见 [Gmsh 部署说明](linux-gmsh-deployment.md)。

2026-11-18 后续：Linux MPI 与 functionObject UDF 已通过真实完整报告流程、运行中取消及重建读回；Windows/WSL 对应流程通过。修复与证据版本见 [Linux MPI/UDF 部署验收](linux-mpi-udf-deployment.md)。

2026-11-18：新增可选 OpenFOAM 容器镜像与 Compose 覆盖配置，Linux 原生网格、串行求解、取消及检查点续算已通过真实工程验收；Windows/WSL 保持兼容。构建、运行时识别、回退和未覆盖工具见 [Linux 工程部署](linux-cae-deployment.md)。

2026-11-17 增量：Docker 整栈与真实 DeepSeek 工具回合已通过，见 [部署记录](docker-deployment-acceptance.md)。API/worker 镜像新增 Pandoc，正式 HTML/DOCX 生成、下载、重启后读回和真实模型读取通过；修复浅安装目录使全部专业处理器导入失败的问题，见 [报告部署](docker-report-deployment.md)。下文带旧日期的环境缺项以各自当时观察为准。

2026-11-16。先复用已有 Windows/WSL 环境；本说明不要求为普通本机运行安装 Docker。工具路径存在只证明可发现，真正可用还需要实际运行和产物校验。

## 配置入口

管理员可在运行环境 Setup 中设置 Python、Pandoc、WSL 发行版和 OpenFOAM bashrc，也可使用认证后的 `GET/PATCH /api/v1/runtime/config`。文件位置由启动进程的 `VIBEFLOW_RUNTIME_CONFIG` 指定，默认是 server/data/runtime-config.json；Compose 的 API/worker 共用 runtimeconfig 卷中的 `/data/runtime/runtime-config.json`。

四个配置的有效值按“进程环境变量 → 已保存 Setup → 默认值”读取。Python/Pandoc 路径统一展开并转为绝对路径，能力清单、阶段检查和实际处理器使用同一结果。环境变量正在控制的项，API 不接受冲突修改，也不会把环境覆盖值悄悄写进 Setup；要修改它应调整部署变量并重启相应进程。

| 工具 | 配置方式 | 验证边界 |
|---|---|---|
| Python/CadQuery/VTK/numpy/matplotlib | Setup python 或进程变量 VIBEFLOW_PYTHON | 使用实际 Python 包探测；几何/后处理还需实际算例 |
| Pandoc HTML/DOCX | Setup pandoc 或进程变量 VIBEFLOW_PANDOC | 阶段 D/E 检查所选路径，生成并下载 HTML/DOCX 才是业务验收 |
| WSL/OpenFOAM/wmake | Setup wsl_distro/openfoam_bashrc，或对应 VIBEFLOW_WSL_DISTRO / VIBEFLOW_OPENFOAM_BASHRC | 复用现有 vibeflow-linux 和 v2406 bashrc；实际 blockMesh/求解/UDF 各自验收 |
| Gmsh/meshio | WSL 内 gmsh；平台 Python 安装 requirements 中的 meshio | meshio 已纳入实际 Python 能力探测；真实生成、gmshToFoam、checkMesh 和 VTU 预览见下文 |
| TeX Live/SyncTeX | Windows 为 WSL 内 PATH/已配置发行版；Linux 为容器 PATH | latexmk、引擎、字体、strace、bibtex/biber、synctex 等由准备阶段实际核查 |
| CalculiX | 显式 VIBEFLOW_CALCULIX 优先；Linux 默认 PATH 中 ccx；Windows 保留原默认安装 | 指向实际 ccx；能力页不再把缺失默认路径的字符串 None 当作已安装 |
| SU2 | 显式 VIBEFLOW_SU2 优先；Linux 默认 PATH 中 SU2_CFD；Windows 保留原默认安装 | 指向实际 SU2_CFD；版本/网格兼容性仍需原生运行 |
| ParaView | 显式 VIBEFLOW_PVPYTHON / VIBEFLOW_PVBATCH 优先；Linux 默认 PATH；Windows 保留原安装 | 指向实际 ParaView 程序；pip VTK 不代替 ParaView |
| CLI 工具 | VIBEFLOW_CODEX / CLAUDE / OPENCODE / KIMI，或各自同前缀的 _DIR 版本目录 | 发现路径不执行推理；会话验收必须用真实模型 |
| Node/npm | PATH | Windows 命令行可用 node.exe/npm.cmd，避免把 npm.ps1 执行策略错误当作缺少 npm |

这些工具变量由运行进程读取。不要只把它们写进 server/.env 就假定所有独立进程已加载；在 API、worker 启动环境注入相应值。不同执行主机应使用各自可访问的绝对路径；Windows 路径不能直接作为 Linux 容器内工具路径。

Setup 的 WSL 下拉/接口仍限制现有命名发行版，OpenFOAM bashrc 要求 `/usr/lib/openfoam/*/etc/bashrc`。本次没有扩展安装器或更改普通 WSL 非特权执行决定。原生 CLI 私有状态根等项目配置见 [开发说明](dev-setup.md)和既有 CLI 部署记录。

## 迁移与恢复

1. 暂停提交新工作，等待已有任务收尾。旧任务封存了工具路径、版本及输入；不要先删除仍被历史恢复引用的旧工具。
2. 备份 runtime-config.json、部署环境变量、工具版本/安装来源，同时备份数据库、算例与原 provider 加密密钥。
3. 在目标主机安装或复用所需工具，把绝对路径指向该主机可访问的位置。不能把本机 D:/vibeflow_reverse 的兼容回退路径当作新机器配置。
4. 将原 Setup JSON 恢复到 VIBEFLOW_RUNTIME_CONFIG 所指位置。API/worker 使用一致配置后启动；移除环境覆盖项时会重新使用已保存的 Setup。
5. 登录查看 `/runtime/config`，再请求 `/runtime/capabilities`。检查实际 Python/WSL 探测结果及缺失项；最后跑对应业务并下载成果。

缺失显式工具不应自动转用本机其他版本来制造“可用”。本次修复了 Pandoc 环境路径只显示却未实际执行、阶段检查忽略 Setup，以及 CalculiX 缺省路径误报；SU2/ParaView 路径也纳入能力清单。

## 当前验收

显式入口 `server/live_llm_toolchain_config_acceptance.py` 使用真实 Pandoc 副本，放在含空格的新安装目录：Setup 路径、重启后的环境覆盖、移除覆盖并恢复 Setup 备份三个阶段，均通过正式报告 API 验证实际 HTML/DOCX。实际作业的封存发布记录必须指向所选工具，旧记录不能随 Setup 改变。

最后由真实 DeepSeek 自主调用 read_text_file 读取新生成的 HTML，并返回只有实际文件里才有的随机标识。配置/文件检查、实际原生工具和模型回合分别记账，不使用模拟回复。最终结果以验收 JSON 为准。

Docker 整栈、物理多主机、多实例索引分发及原全功能外部工具范围没有被这三个报告场景替代。N7-05 继续按原剩余条件验收。

## 实际结果与失败记录

实际目录为 `tmp/live-toolchain-config-eeir4snt` 和 `tmp/live-toolchain-config-xa1d3z1r`。两轮均完成 Setup、环境覆盖、恢复 Setup 三个真实 HTML/DOCX 作业；发布记录绑定实际选中的 Pandoc 路径，原记录保持。最终轮还通过显式缺失工具不回退、环境控制项冲突修改拒绝及不污染原 Setup 的检查。

最终轮真实 DeepSeek 读取生成 HTML，通过关联持久化完整工具结果、流式预览及最终回复核验随机标识。两轮共 4 次真实 HTTP200、19,902 tokens，模拟0；其中最终模型验收为2次、9,962 tokens。首轮模型已经返回标识，但原检查误要求2000字预览包含标识，故保留为失败；修正后核对完整持久结果及预览前缀。

最终整套脚本在模型通过后，因清理检查把 children_json 外层结构当成 child 而报 KeyError，原 passed=false 和 exit1 保留。随后只对已有记录独立验证：每轮3个作业、7个原生请求、31个原生子执行的完整停止证明，以及36个宿主/原生出生身份实际退出均通过。没有为修正清理检查重跑模型。独立核验最初传错 native 身份层级，修正为原 process 字段后通过；这些都是验收代码问题，未修改产品停止规则。

因此本阶段结论来自“实际功能/模型结果 + 独立清理核验”，不声称整套脚本一次通过。记录为两目录下的 toolchain-result.json、live-result.json 和 independent-exits.json，源审查为 N7-05-toolchain-config-review-v2-20261116.json。

实际能力探测显示 Python 3.11.7、CadQuery 2.8.0、VTK 9.6.2、numpy 2.4.6；WSL 的 OpenFOAM v2406、wmake 和 latexmk 可用，gmsh 当前未发现。该缺项仍列在外部工具环境范围，不能称完整工具链全部可用。Compose 11组配置检查通过，持久卷未冒充实际 Docker 部署验证。主线48/53、剩5项。

## Gmsh 缺项已解决

2026-11-16 后续已在现有 `vibeflow-linux` 使用 Ubuntu noble/universe 的 `gmsh 4.12.1+ds1-1.1build2`。安装命令为 `apt-get install -y --no-install-recommends gmsh`；新增29个包，未升级或删除已有包，日志在 `tmp/gmsh-install-20261116.log`。安装使用管理员权限，实际平台网格作业仍采用UID65534，不更改普通WSL非特权决定。

新增[可复用网格示例](../server/examples/gmsh-box/README.md)，真实平台作业完成 Gmsh 三维生成、gmshToFoam 转换、checkMesh、meshio解析、VTU预览和质量报告。结果为144节点、391四面体、655预览单元；最大非正交角46.037、偏斜度0.617394、长宽比4.16876，Mesh OK。运行环境已实际探测出meshio5.3.5，并显示gmsh可用。

首轮缺少fvSchemes，虽然生成/转换成功仍按失败保留；补齐fvSchemes和fvSolution后整轮通过。实际目录为 `tmp/live-gmsh-deployment-94ruyui9`（失败，模型0）与 `tmp/live-gmsh-deployment-3whs6juk`（通过）。最终真实DeepSeek2次HTTP200、8,584tokens，模拟0；原生作业完整停止回执与API退出已核验。没有重启开发API或改写开发算例。

该轮A/B/C/D/E/V最低阶段能力均可用，不等于全工具、全部算例或生产拓扑已经验收。该轮能力快照中Kimi CLI尚未发现；2026-11-17后续已安装Kimi2.6.2并完成真实平台终端/DeepSeek读文件/关闭流程，见[Kimi部署验收](kimi-deployment.md)。其他CLI的能力发现不代替各自模型验收。snappy/cfMesh、全部外部依赖和Docker整栈继续按原计划，不能用本次单个Gmsh算例关闭整个N7-05或FF-B17。

## 编译缓存的现有部署约束

UDF构建在作业私有副本中移除自身 `Make/<WM_OPTIONS>`、`lnInclude` 和旧目标库后执行真实wmake；`FOAM_USER_LIBBIN` 指向该次私有输出，不依赖宿主共享用户库制造缓存命中。依赖证明区分C/C++/Make输入和编译缓存，保留工具路径与摘要。该行为来自 `udf_native.py` / `udf_dependencies.py`，不是本轮新增的缓存验收。

迁移时保留原任务使用的工具和封存证据，不跨OpenFOAM/编译器版本复用二进制缓存。当前发行版可找到g++和make；cmake/ccache本次未发现，不能宣称所有外部项目构建依赖齐备。已有wmake流程无需因这两个工具缺失而修改权限或关闭验证；实际需要它们的构建目标应按自身依赖单独安装和验收。

Linux TeX 镜像、字体缓存、三引擎及 SyncTeX 实际验收见 [Linux TeX 部署](linux-tex-deployment.md)。

Linux/WSL FloTHERM 辅助库实际编译、私有安装、加载、失败保留旧库及取消验收见 [Linux FloTHERM部署](linux-flotherm-deployment.md)。

CalculiX Linux 源码镜像、实际数值与Windows兼容验收见 [Linux CalculiX部署](linux-calculix-deployment.md)。

SU2 Linux镜像、实际翼型求解/续算和Windows兼容验收见 [Linux SU2部署](linux-su2-deployment.md)。

ParaView Linux离屏镜像、双backend真实CFD处理及Windows兼容见 [Linux ParaView部署](linux-paraview-deployment.md)。

2026-11-18：Linux Node/npm与三非Claude CLI固定版本、真实模型及正式Codex会话、PID映射和私有链接统计修复见[Linux Node与CLI部署](linux-node-cli-deployment.md)。
