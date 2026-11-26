# FF-C09 COMSOL 本机批处理

在具备 COMSOL 安装与许可证的 API 和作业进程中设置 `VIBEFLOW_COMSOL`。Windows 指向 `comsolbatch.exe`，Linux 指向 `comsol`。两个进程使用同一安装及许可配置。`GET /api/v1/cases/{case_id}/comsol:environment` 按算例读权限检查启动程序，许可状态显示为 `unverified`，由正式批处理签出。 [Windows 命令参考](https://doc.comsol.com/6.3/doc/com.comsol.help.comsol/comsol_ref_running.38.31.html) · [Linux 命令参考](https://doc.comsol.com/6.3/doc/com.comsol.help.comsol/comsol_ref_running.38.32.html)

上传 `C-run` 下的 `.mph` 后，向 `POST /api/v1/cases/{case_id}/comsol:solve` 提交请求。`study` 可选，留空使用模型设置，指定时使用 Study 节点的标签。`cpus` 默认 1，范围 1 至 16。接口支持 `Idempotency-Key`。模型按字节封存，主输入使用现有执行工作区容量上限 1 GiB，账户配额和节点可用空间仍参与准入。

```json
{
  "input_path": "C-run/thermal.mph",
  "study": "std1",
  "cpus": 4
}
```

作业在私有尝试目录中使用本机批处理模式，分别设置临时文件、偏好、配置、工作区和恢复目录。当前入口执行本机模型 Study。批处理与集群设置按厂商 `-mode batch` 行为处理。作业详情、日志及取消接口与 Abaqus 共用。 [批处理模式与 Study 参数](https://doc.comsol.com/6.4/doc/com.comsol.help.comsol/comsol_installation.02.103.html)

成功发布须有退出码 0、非空 `result.mph` 和 `batch.log`。平台把产物和 `analysis-receipt.json` 放入 `C-run/comsol/<执行标识>/`，并记录原输入、所选 Study、启动程序和产物的绑定。发布前再次核对已封存输入和产物摘要。

## 协议验收

独立 Windows API 在文件系统和认证 SeaweedFS 上分别上传大于 2 MiB 的二进制文件，核对启动参数和 `std1`。检查未配置时返回 503 且无作业，拒绝无效 Study 和 `.class`，保留非零退出日志，幂等重放返回原作业，取消后的实际 Windows 子进程退出。两种存储都验证退出码为 0 但缺少产物时失败，以及合成协议产物发布后从算例读回。持证 COMSOL 尚未配置，实际求解、结果模型的数值校验和 Linux 厂商运行时仍待验收。模型调用 0 次。 [文件系统回执](../../tmp/comsol-owned-protocol-kwmry5x2/checks.json) · [认证 S3 回执](../../tmp/comsol-owned-protocol-gxl997_3/checks.json)

```powershell
& platform/server/.venv/Scripts/python.exe -B -u platform/server/commercial_batch_protocol_acceptance.py --backend comsol
```

加 `--seaweed` 和本机 SeaweedFS 可执行文件路径可在独立认证对象存储复验。 [协议验收脚本](../server/commercial_batch_protocol_acceptance.py)
