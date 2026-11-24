# FF-I01 FloTHERM 工作台浏览器交付

2026-11-20。在项目面板选择算例后，打开中栏「FloTHERM」标签。窄屏从「工作面板」选择 FloTHERM。

1. 在「输入预检」中选择算例已导入的 ECXML、PDML、XTXML 或 XTXMLA 文件。结果显示服务端判定的组合、文件摘要、bbox 数量、阻断原因和提示。
2. 在「IR 转换」中选择已有的 FloTHERM IR JSON、可选的转换设置 JSON 和算例相对输出目录。提交后使用平台作业号追踪状态，可进入对应作业日志或取消运行。
3. 作业完成发布后，从「项目输出文件」打开预检、覆盖报告及 OpenFOAM 文件。辅助库区可选择源码目录和目标目录，提交实际编译和动态加载检查，完成后可查看编译日志、加载日志和生成的 `.so`。

浏览器在当前账号和算例下保存最近一次提交的作业号及请求凭据。页面重载后恢复查看作业。提交响应丢失后，用户可用原参数及同一个幂等键确认。复验中出现过存储发布占用导致重放返回 HTTP 409，已修复为保留原请求，在可重试冲突后仍可继续确认。[面板实现](../web/src/panels/FloThermPanel.tsx) · [复验前的并发冲突](../../tmp/flotherm-studio-browser-kr23dxuj/browser.json)

## 验收证据

真实 Edge 使用构建后的工作台，连接隔离的 SQLite API 和文件存储。无模型调用，无模拟 LLM。Windows 运行转换，非特权 WSL 中运行实际 OpenFOAM `wmake` 和动态库加载检查。

| 路径 | 实测结果 |
| --- | --- |
| 输入预检 | 不同主文件名的配对被拒，同名 ECXML＋PDML 通过，摘要与原始文件相符。 |
| IR 转换 | 作业 1 完成，7.5 W 配置覆盖进入覆盖报告，生成 `system/blockMeshDict`，报告可在编辑器打开，作业日志定位正确。浏览器丢弃首次真实响应后重载，用原幂等键找回同一作业。 |
| 辅助库 | 作业 2 完成，浏览器显示加载日志和共享库，服务器产物具有 ELF 文件头。 |
| 取消 | 作业 3 在实际发布准备阶段接收取消，状态为 `cancelled`，原始 IR 完整，取消目标目录未发布。 |
| 算例切换与窄屏 | 独立算例没有继承作业回执，返回原算例可恢复。390 px 下可切换工作面板、选择文件并读取状态，无横向溢出。 |

[验收入口](../server/flotherm_studio_browser_acceptance.py) · [浏览器结果](../../tmp/flotherm-studio-browser-5bwxn6xg/browser.json) · [产物核验](../../tmp/flotherm-studio-browser-5bwxn6xg/attestation.json) · [桌面预检](../../tmp/flotherm-studio-browser-5bwxn6xg/preflight-desktop.png) · [桌面输出](../../tmp/flotherm-studio-browser-5bwxn6xg/desktop.png) · [窄屏](../../tmp/flotherm-studio-browser-5bwxn6xg/mobile.png)

## 离线 CLI

在项目根目录执行以下命令。`model-ir` 可以是含 `flotherm-ir.json` 的目录，也可以直接给 IR 文件路径。

```text
python platform/server/flotherm2foam.py inspect model.ecxml --pdml model.pdml
python platform/server/flotherm2foam.py validate model-ir
python platform/server/flotherm2foam.py build model-ir --config settings.json --output case-output
python platform/server/flotherm2foam.py check case-output
```

原 `--inspect`、`--version` 和输出准备选项仍可用。`build` 在临时目录验证 IR、应用配置并生成文件，随后发布到选定目录。`--force` 复用生成标记保护。`check` 核对生成文件是否缺失或修改，返回静态预检阻断和退出码。网格与求解验收继续使用实际 OpenFOAM 作业。

真实 Windows 和非特权 WSL 子进程均通过命令链，实际读回 7.5 W 覆盖值和 5 W 重建值。非法 IR、默认已有输出和带用户文件的目标均拒绝且保留原结果。修改或删除 `constant/fvOptions` 后，静态检查分别报告对应问题。两端模型调用 0，模拟 0。[CLI 实现](../server/app/services/flotherm_cli.py) · [验收入口](../server/flotherm_cli_acceptance.py) · [Windows](../../tmp/flotherm-cli-windows-20261120.json) · [WSL](../../tmp/flotherm-cli-wsl-20261120.json)

已追加 ECXML、V2310 PDML 和 XTXML/XTXMLA 配对的三维盒体导入。CLI 和网页均可生成 IR 后转换，实际求解和认证 S3 已验。Pack `export` 和 FloSCRIPT 阶段执行由 FF-I03 承接，复杂部件和物理映射由 FF-I04 承接。[ECXML 导入实测](FF-I01-I07-ecxml-import.md) · [PDML 与 XTXML](FF-I01-I07-paired-import.md) · [输入检查与输出保护](FF-I02-I10A-flotherm.md)
