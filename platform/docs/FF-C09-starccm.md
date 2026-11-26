# FF-C09 Simcenter STAR-CCM+ 本机批处理

在具备 STAR-CCM+ 和适用许可证的 API 与作业进程中设置 `VIBEFLOW_STARCCM`，值为厂商启动程序的绝对路径。`GET /api/v1/cases/{case_id}/starccm:environment` 检查程序路径，许可由实际批处理签出。Windows 和 Linux 各使用本机安装。 [Siemens 批处理说明](https://community.sw.siemens.com/s/article/How-to-kill-a-PoD-simulation-after-a-specified-amount-of-time)

向 `POST /api/v1/cases/{case_id}/starccm:solve` 提交 `C-run` 下的 `.sim`。`operation` 默认为 `solve`，也可选 `mesh_then_solve`。`cpus` 默认 1，范围 1 至 16。平台封存输入，在本机使用 `-batch run` 或 `-batch mesh,run`，沿用模型中的网格和停止条件。请求支持 `Idempotency-Key`。 [STAR-CCM+ 命令参考镜像](https://www.tech-doc.cn/wp-content/uploads/Free/StarCCM2406/GUID-A871C88B-038B-48AE-80F4-886521497B0F.html)

```json
{
  "input_path": "C-run/duct.sim",
  "operation": "mesh_then_solve",
  "cpus": 4
}
```

平台在同一批处理序列末尾运行固定的保存宏，明确保存 `result.sim` 并写入完成标记。作业要求进程退出码为 0、保存已确认且结果非空。结果和 `analysis-receipt.json` 发布到 `C-run/starccm/<执行标识>/`。详情、日志和取消使用通用作业接口。 [厂商宏与保存接口说明镜像](https://www.topcfd.cn/Ebook/STARCCMP/GUID-1C6EB939-532D-47AE-B9B6-C04A15BA542F.html)

## 协议验收

独立 Windows API 在文件系统与认证 SeaweedFS 分别验证了大于 2 MiB 的输入、两种操作顺序、固定保存宏、缺少最终保存标记时拒绝发布、失败日志、幂等重放和取消后实际子进程退出。协议产物从原算例读回。当前环境尚未配置持证 STAR-CCM+，实际网格、求解结果与宏运行仍待厂商环境验收。本轮模型调用 0 次。 [文件系统回执](../../tmp/starccm-owned-protocol-et93605n/checks.json) · [认证 S3 回执](../../tmp/starccm-owned-protocol-howgp2ns/checks.json)

```powershell
& platform/server/.venv/Scripts/python.exe -B -u platform/server/commercial_batch_protocol_acceptance.py --backend starccm
```

加 `--seaweed` 和本机 SeaweedFS 可执行文件路径可复验认证对象存储。 [协议验收脚本](../server/commercial_batch_protocol_acceptance.py)
