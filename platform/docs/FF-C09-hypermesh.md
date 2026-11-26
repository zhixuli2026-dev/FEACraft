# FF-C09 Altair HyperMesh 网格导出

在具备 HyperMesh 安装和适用许可的 API 与作业进程中设置 `VIBEFLOW_HYPERMESH`，指向 Windows `hmbatch.exe` 或 Linux `hmbatch` 的绝对路径。`GET /api/v1/cases/{case_id}/hypermesh:environment` 检查程序配置。Linux 批处理使用前台运行选项。 [Altair 批处理启动参数](https://2025.help.altair.com/2025/hwdesktop/hwx/topics/getting_started/hm_batch_startup_options_r.htm)

向 `POST /api/v1/cases/{case_id}/hypermesh:export` 提交 `B-mesh` 或 `C-run` 下已经含网格的 `.hm`。`format` 支持 `optistruct` 和 `abaqus`，分别产生 `.fem` 与 `.inp`。模板来自安装目录，模型以目标求解器对应的 profile 建立。请求支持 `Idempotency-Key`，以网格作业登记。 [Altair 的批处理与安装模板示例](https://2025.help.altair.com/2025/hwdesktop/hwx/topics/conversion_between_solvers/batch_conversion.htm)

```json
{
  "input_path": "B-mesh/bracket.hm",
  "format": "abaqus"
}
```

固定 Tcl 在独立目录打开封存模型，读取节点及单元计数，使用所选模板导出并将 include 合并成单个 deck。只有网格计数有效、进程退出码为 0、所选格式的 deck 存在且非空时才发布。`B-mesh/hypermesh/<执行标识>/` 保存 deck、`mesh-summary.json` 和 `analysis-receipt.json`。作业日志和取消沿用通用接口。 [有限元导出命令](https://www.2022.help.altair.com/2022.1/hwdesktop/hwd/topics/reference/hm/_feoutputwithdata.htm) · [合并 include 命令](https://2025.help.altair.com/2025/hwdesktop/hwd/topics/reference/hm/_feoutputmergeincludefiles.htm)

## 协议验收

独立 Windows API 在文件系统和认证 SeaweedFS 上分别验证大于 2 MiB 的 `.hm` 输入、两种模板及网格作业类型。用 Tcl 8.6 执行生成的脚本，测试替身提供 Altair 专用命令。检验了空网格拒绝、非零退出日志、幂等重放、实际子进程退出，以及两种合成 deck 和计数回执发布后从算例读回。首轮记录暴露了嵌入式 Tcl 缺少 `exit`，最终流程在脚本完成后由批处理自然退出。当前缺少已配置的持证 HyperMesh，实际 `.hm`、厂商模板和网格导出仍待验收。模型调用 0 次。 [文件系统回执](../../tmp/hypermesh-owned-protocol-xf15ntew/checks.json) · [认证 S3 回执](../../tmp/hypermesh-owned-protocol-vxwe3y9_/checks.json) · [Altair 脚本生命周期](https://www.2024.help.altair.com/2024/hwdesktop/hwd/topics/reference/hm/running_scripts_r.htm)

```powershell
& platform/server/.venv/Scripts/python.exe -B -u platform/server/commercial_batch_protocol_acceptance.py --backend hypermesh
```

加 `--seaweed` 和本机 SeaweedFS 可执行文件路径可复验认证对象存储。 [协议验收脚本](../server/commercial_batch_protocol_acceptance.py)
