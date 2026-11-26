# FF-I03 FloTHERM Pack 导出

CLI `export` 会复制原始 Pack 到独立暂存目录，设置私有 `FLOUSERDIR`，生成 FloSCRIPT，再调用本机或可从 WSL 启动的 Windows FloTHERM。可指定 `--flotherm-bat` 或环境变量 `FLOTHERM_BAT`，接受已安装的 `.bat` 或 `.exe`。使用具有写权限的 Windows 盘作为输出位置。

```text
python platform/server/flotherm2foam.py export assembly.pack --output exported --flotherm-bat "C:\Program Files\FloTHERM\WinXP\bin\flotherm.bat" --keep-stage
python platform/server/flotherm2foam.py import exported/model.ecxml --output model-ir
python platform/server/flotherm2foam.py build model-ir --output thermal-case
```

ECXML 和 XTXMLA 格式检查均通过后才发布文件、原生输出日志及 Pack 摘要。默认回收暂存目录。`--keep-stage` 保留脚本、日志和原始暂存供排障。`--force` 沿用生成标记和未登记文件保护。 [CLI 生成标记](FF-I02-I10A-flotherm.md)

Windows 与非特权 WSL 的阶段协议验收均实际启动 Windows 批处理进程，核对 `-f` 参数、FloSCRIPT 顺序、用户目录、退出码 37、失败时保留原输出与两种暂存模式。当前环境缺少已安装的有许可证 FloTHERM 和真实 Pack 算例，供应商导出及生成算例求解仍待验收，FF-I03 保持开启。真实模型调用 0，模拟调用 0。[Windows 进程回执](../../tmp/FF-I03-floscript-stage-20261123/windows-retry/stage-validation.json) · [WSL 进程回执](../../tmp/FF-I03-floscript-stage-20261123/wsl-retry/stage-validation.json)

## 工作台 Pack 导出

在 Windows API 服务进程环境中设置 `FLOTHERM_BAT`，指向已安装 FloTHERM 的 `.bat` 或 `.exe`。工作台展开「Pack 导出」后检查运行程序路径，选择已上传到算例的 `.pack` 和空输出目录，提交后台作业。运行程序由部署方设置。用户可在「作业与输出」追踪、取消或打开日志。缺少运行程序时，工作台显示配置提示及重新检查入口。

作业封存输入与运行程序摘要，在自己的执行目录生成 FloSCRIPT 和私有 `FLOUSERDIR`。成功后发布 `model.ecxml`、`model.xtxmla`、两份原生日志及 `export-manifest.json`。回执记录 Pack 的相对路径、SHA-256、作业及原生退出码。点击「用 ECXML 预检」可继续导入并生成 IR。 [作业入口](../server/app/routers/flotherm.py) · [Windows 执行器](../server/app/services/flotherm_pack_worker.py)

本地文件系统和认证 SeaweedFS 分别通过实际 Windows 进程的协议验收。测试批处理分别退出失败、阻塞以测试取消，以及复制已有中性格式样本以核验成功发布。两种存储均验证失败日志、运行中取消及进程范围清空、原 Pack 摘要、相同幂等键重放、已有输出保护、跨账号拒绝和 ECXML 后续导入。Edge 另验运行程序不可用提示、恢复检查、非法输出路径被阻止、提交、刷新后作业找回及用导出文件预检。390 像素面板无横向溢出。离线 CLI 协议回归与原 Studio 转换及辅助库浏览器回归通过。模型调用 0 次。仍需持证 FloTHERM 真实导出 Pack 并验证转换算例。 [本地与 Edge](../../tmp/flotherm-pack-studio-ga0m8rj8/pack-result.json) · [SeaweedFS](../../tmp/flotherm-pack-studio-86z26mrz/pack-result.json) · [原 Studio 回归](../../tmp/flotherm-studio-browser-hpo1fydt/browser.json) · [CLI 复验](../../tmp/flotherm-pack-cli-odmvx7ai/stage-validation.json)
