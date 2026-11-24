# FF-I03 FloTHERM Pack 导出

CLI `export` 会复制原始 Pack 到独立暂存目录，设置私有 `FLOUSERDIR`，生成 FloSCRIPT，再调用本机或可从 WSL 启动的 Windows FloTHERM。可指定 `--flotherm-bat` 或环境变量 `FLOTHERM_BAT`，接受已安装的 `.bat` 或 `.exe`。使用具有写权限的 Windows 盘作为输出位置。

```text
python platform/server/flotherm2foam.py export assembly.pack --output exported --flotherm-bat "C:\Program Files\FloTHERM\WinXP\bin\flotherm.bat" --keep-stage
python platform/server/flotherm2foam.py import exported/model.ecxml --output model-ir
python platform/server/flotherm2foam.py build model-ir --output thermal-case
```

ECXML 和 XTXMLA 格式检查均通过后才发布文件、原生输出日志及 Pack 摘要。默认回收暂存目录。`--keep-stage` 保留脚本、日志和原始暂存供排障。`--force` 沿用生成标记和未登记文件保护。Studio 入口支持配对中性格式及 IR。Pack 和 FloSCRIPT 运行与覆盖选项位于离线 CLI。[Studio 输入与输出](FF-I01-flotherm-studio.md) · [CLI 生成标记](FF-I02-I10A-flotherm.md)

Windows 与非特权 WSL 的阶段协议验收均实际启动 Windows 批处理进程，核对 `-f` 参数、FloSCRIPT 顺序、用户目录、退出码 37、失败时保留原输出与两种暂存模式。当前环境缺少已安装的有许可证 FloTHERM 和真实 Pack 算例，供应商导出及生成算例求解仍待验收，FF-I03 保持开启。真实模型调用 0，模拟调用 0。[Windows 进程回执](../../tmp/FF-I03-floscript-stage-20261123/windows-retry/stage-validation.json) · [WSL 进程回执](../../tmp/FF-I03-floscript-stage-20261123/wsl-retry/stage-validation.json)
