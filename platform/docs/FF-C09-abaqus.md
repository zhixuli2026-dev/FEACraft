# FF-C09 Abaqus 本机批处理

在持有有效 Abaqus 安装与许可的 API 和作业进程中设置 `VIBEFLOW_ABAQUS`，值为厂商启动程序的绝对路径。Windows 支持 `.exe`、`.bat` 和 `.cmd`，Linux 需要可执行文件。将 API 和作业进程指向同一个受信安装。`GET /api/v1/cases/{case_id}/abaqus:environment` 按算例读权限检查配置。`available` 表示启动程序存在且符合平台的路径要求，`license` 保持 `unverified`。许可签出由实际分析命令执行。

`POST /api/v1/cases/{case_id}/abaqus:solve` 接收算例内 `C-run` 下的自包含 `.inp`，同时支持 `Idempotency-Key`。与其他登记的求解器一样，主输入当前上限为 1 MiB。批处理接收 ASCII 输入，`cpus` 默认 1，范围 1 至 16。外部输入、用户子程序、重启动和联合仿真需对应的封存和执行契约。当前入口面向本机批处理。

```json
{
  "input_path": "C-run/beam.inp",
  "cpus": 1
}
```

平台封存选中的输入和启动程序摘要，在独立尝试目录以固定文件名启动正式 `analysis` 命令，指定 `interactive` 和 `resultsformat=odb`。标准输出和错误进入通用作业日志。通过现有作业详情、日志和取消接口读取状态、取消及核对原生进程停止。兼容的启动程序须保留同步运行语义，许可由部署环境配置。 [厂商命令参数](https://docs.software.vt.edu/abaqusv2026/English/SIMACAEEXCRefMap/simaexc-c-analysisproc.htm)

作业要求退出码为 0、非空 ODB 和成功结束的 `.sta`。发布目录为 `C-run/abaqus/<执行标识>/`，发布时再次核对封存输入、捕获摘要和输出目录。`analysis-receipt.json` 记录作业、尝试、输入及启动程序摘要，以及发布的产物摘要。厂商文档描述了 ODB、状态文件和诊断文件各自的用途。 [Abaqus 输出文档](https://docs.software.vt.edu/abaqusv2026/English/SIMACAEOUTRefMap/simaout-c-output.htm)

## 本机验收

独立 Windows API 使用本地文件系统和认证 SeaweedFS 分别验证未配置时 HTTP 503 且没有作业、拒绝外部引用、启动程序非零退出保留日志，以及相同幂等键返回相同作业。两次取消均按进程出生身份确认真实 Windows 子进程已退出。两种存储也都验证退出码为 0 但缺少产物时失败，以及合成协议产物发布后能从原算例 API 读回。持证求解 0 次、模型调用 0 次。当前环境缺持证 Abaqus，实际 ODB 和数值结果仍待验收。 [文件系统回执](../../tmp/abaqus-owned-protocol-yle_wfqg/checks.json) · [认证 S3 回执](../../tmp/abaqus-owned-protocol-ybsthgid/checks.json)

与 COMSOL 共用封存、启动及发布流程后，两种存储的 Abaqus 作业协议复验均通过。 [文件系统复验](../../tmp/abaqus-owned-protocol-v_7wzfcf/checks.json) · [认证 S3 复验](../../tmp/abaqus-owned-protocol-dd3mwnus/checks.json)

从仓库根目录运行协议验收，脚本会创建独立 API 和算例。传入 `--seaweed` 可在独立认证对象存储中复跑。 [验收脚本](../server/commercial_batch_protocol_acceptance.py)

```powershell
& platform/server/.venv/Scripts/python.exe -B -u platform/server/commercial_batch_protocol_acceptance.py --backend abaqus
```
