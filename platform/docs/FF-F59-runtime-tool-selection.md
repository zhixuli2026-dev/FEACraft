# FF-F59 运行时工具发现与显式路径

2026-11-21，工具注册表、Python/WSL 探测和 A–E/V 最小能力检查共用一次读取的配置。Node、npm 与 Pandoc 按指定的 PATH 查找，已配置的显式路径优先。无效的显式 Node 路径会使 V 阶段显示缺少 Node。WSL 发行版与 OpenFOAM bashrc 的可用性来自真实探测。CLI 路径与能力清单共用同一次发现结果。[修复前复现](../../tmp/runtime-registry-before-20261121-1413/before.json) · [注册表实现](../server/app/services/runtime_registry.py)

真实 Edge 验证了显式 Node 文件缺失、重启后选择有效路径并恢复 V 阶段。实际命令返回 Node v24.15.0 和 npm 11.12.1。隔离进程在空 PATH 下查不到两者，在指定 PATH 下发现 Node 可执行文件和 npm.cmd。浏览器还通过了 Python 更换后探测、WSL 发行版及 OpenFOAM bashrc 的缺失与恢复、普通组织成员修改被拒绝和冷启动恢复设置。真实非特权 WSL 作业对算例执行了路径与文件检查，停止证明通过。[Edge 和 WSL 证据](../../tmp/runtime-setup-browser-odbxeafc/runtime-setup-result.json)

三个实际 Pandoc 报告作业分别采用持久 Setup 路径、环境变量覆盖路径和恢复后的 Setup 路径。各作业均下载并校验 HTML 与 DOCX，封存记录绑定实际使用的 Pandoc。显式工具缺失时相应报告阶段返回缺项。三个作业和七个原生探测子进程均有停止证明。[正式报告证据](../../tmp/live-toolchain-config-w2dxpjay/toolchain-result.json)

最后一个报告由真实 DeepSeek Flash 调用 `read_text_file` 读回并原样返回随机验证串。两个真实上游 HTTP 200，模拟调用为零。[模型结果](../../tmp/live-toolchain-config-w2dxpjay/live-result.json) · [真实验收源审查](../../tmp/F59-toolchain-config-review-20261121.json)
