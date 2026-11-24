# FloTHERM 辅助库的独立编译与结果发布

2026-11-05，本批实现与隔离验收完成。主线 53 项、全功能 295 项范围不变，本批不按局部测试关闭整项任务。

本地 `flotherm:build-library` 接入 `flotherm.build-library@1`。提交时封存来源文件与复制目标，Windows worker 通过所属原生执行通道启动 WSL 的非 root 编译进程，在 `/tmp/vibeflow-flotherm-library-<execution_id>` 下独立复制、编译并动态加载。实际 wmake、C++ 编译器、Python 加载器和 OpenFOAM 预加载库记录哈希；只有完成编译、加载、来源复核和发布校验才更新用户文件。

支持原同目录构建，以及从 source_dir 复制到新 destination_dir。复制模式保留原有目录不嵌套、目标必须不存在、最多 20 个文件及每文件 1 MiB 的限制；源码复制延迟到成功发布。失败或取消时，原目录保持不变，不在用户目录留下未完成的复制目标。普通同目录重编译保留旧库直至新库发布。

每次重新创建私人副本内的 OpenFOAM 对象缓存和 lnInclude，迫使选中的库重新编译链接。不会先删除用户原库，也不会删除私人副本中的其他依赖库。实际新建或重写的 ELF 库均经 CDLL 加载校验，不把既有旧库当成此次编译成功的证明。

`LIB = $(PWD)/...` 的库保留算例相对位置；`LIB = $(FOAM_USER_LIBBIN)/...` 使用本次执行专用的 userlib，加载通过后将库发布到选定 destination_dir，并在回执记录对应安装来源。本批没有向共享全局 OpenFOAM 用户库目录安装，不能据此宣称 FF-I09 的所有全局安装和恢复要求已经验收。Make 的外部依赖、RPATH、全部工具链/缓存语义仍需按原要求补齐。

发布使用不可变 bundle 与条件写入：复制目标被创建、源文件被编辑、目标库被替换或回执损坏时，不能覆盖用户的新内容。已有输入中只允许实际库输出、指定编译缓存与本次日志变化，其余源码和用户原件必须保持原哈希；原始输入版本、运行目录和 native 日志仍可用于诊断。独立目录不是操作系统沙箱。

SQLite/PostgreSQL 各 12 项通过：实际 wmake/CDLL、同目录重编译及新函数执行、私有 userlib 安装、编译/加载失败、源/目标变更、损坏回执、取消和已验证结果的 API 重启发布。额外读取完整原生日志，确认失败确实来自 C++ 编译错误或加载器拒绝缺失符号，不以任意失败冒充预期故障。

原 I09 库接口及 I01 转换接口在临时服务通过，双 API 旧接口提交兼容 16 项通过。实际 cc1plus 的同目录故障 4 项、复制编译故障及新 API 历史读取 5 项通过；复制编译器的工作目录实测位于对应私人 Linux stage，用户复制目标在取消和失败后不出现，封存源码不变。错误父进程登记没有释放子编译器；取消/超时停止回执可核实，并发/重复取消不改写既有回执，外部哨兵保持运行。

首轮两套故障测试的 25 秒期限大于实际编译耗时，编译成功导致超时断言失败；保留原始结果，改用 12 秒测试期限后捕获实际编译器超时退出。产品超时逻辑没有为测试改动。API 强杀各留下一个私人 stage，核对捕获进程已退出以及 UID/device/inode 后只清理这两个测试目录；总共核查 10 个 stage 回执。此清理不是产品自动回收实现，N5 对应要求仍待完成。

规范证据：`tmp/mainline-N2-flotherm-library-evidence/evidence-origin.json`；原双库结果：`tmp/flotherm-library-evidence-qnrqp967/result.json`。本批没有迁移开发数据库、重载 8000 或改变正式部署开关，临时 PostgreSQL 数据库已移除。

当前有 20 个注册处理器。下一项为 `/jobs` 中旧显式 mesh/solve 等带类型命令的独立执行，随后继续 S3 默认隔离及所有尚未完成的工作流接线、恢复和全功能交付。当前主线仍为 20 完成、14 进行中、19 待开始，剩 33；全功能严格整项验收 0/295。

## FF-I09 共享辅助库安装及回退

在已加载 OpenFOAM 的 Linux 或 WSL 中安装实际编译的 2R 或定流量风扇库。命令使用当前 `FOAM_USER_LIBBIN`，也可用 `--library-dir` 指定用户库目录。安装返回版本摘要、前一版摘要和 ELF 链接依赖。将求解器控制文件中的 `libs` 配置为相应库名，并以该用户库路径启动求解。

```shell
python3 platform/server/flotherm2foam.py install-helper /path/to/libvibeflowFlothermTwoR.so
python3 platform/server/flotherm2foam.py rollback-helper libvibeflowFlothermTwoR.so PREVIOUS_SHA256
```

安装前读取 ELF 依赖，并在单独的原生加载进程打开候选库。通过后按摘要保存版本，再原子切换活动链接。原有普通库文件可保存为首个历史版本。回退再次核对旧版字节和原生链接。两类辅助库都实际链接 `libOpenFOAM`、`libPstream`、`libfiniteVolume` 和 `libmeshTools`，风扇另外链接 `libfvOptions`。 [安装与回退实现](../server/app/services/flotherm_helper_install.py)

WSL 与 Linux 容器都以 UID 65534 执行真实 `wmake`、共享安装、原生新符号调用、故障保留和回退。两个环境的 2R 实际计算在第 4 步得到相同结温 `310.144883682 K`，定流量风扇在相同面区的实测质量流量为 `0.00137098909695 kg/s`。现有托管构建和较长的 2R、风扇串并行计算也纳入 T09。测试使用隔离的用户库目录。真实模型和模拟均为 0 次。 [T09 验收](../../tmp/FF-I09-shared-helpers-20261123/t09-validation.json) · [WSL 实算](../../tmp/FF-I09-shared-helpers-20261123/acceptance.log) · [Linux 容器实算](../../tmp/FF-I09-shared-helpers-20261123/native-container/acceptance.log)
