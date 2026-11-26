# FF-C09 Ansys Fluent 本机批处理

在具备 Ansys Fluent 安装和许可的 API 与作业进程中设置 `VIBEFLOW_FLUENT`，值为厂商启动程序的绝对路径。`GET /api/v1/cases/{case_id}/fluent:environment` 检查启动程序配置，许可签出发生在真实批处理中。Windows 使用 `-wait` 等待厂商进程退出。 [厂商批处理参考](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/flu_ug/flu_ug_BatchExecution.html)

向 `POST /api/v1/cases/{case_id}/fluent:solve` 提交算例内 `C-run` 下的 `.cas.h5` 或 `.cas`，可同时提供同格式的 `.dat.h5` 或 `.dat`。平台封存两份输入并按厂商约定用同一基本文件名暂存。只提交 case 时使用 `hybrid` 或 `standard` 初始化，提供 data 时从该结果继续。`dimension` 支持 `2d`、`2ddp`、`3d` 和 `3ddp`，默认为 `3ddp`。`cpus` 默认 1，范围 1 至 16。 [Case 和 Data 文件参考](https://ansyshelp.ansys.com/public/Views/Secured/corp/v252/en/flu_ug/flu_ug_CaseDataFiles.html)

```json
{
  "input_path": "C-run/duct.cas.h5",
  "data_path": "C-run/duct.dat.h5",
  "dimension": "3ddp",
  "cpus": 4,
  "time_steps": 5,
  "iterations": 20
}
```

`time_steps` 留空时执行 `iterations` 次稳态迭代。指定 `time_steps` 时，`iterations` 表示每步的迭代上限。平台根据参数生成 TUI journal，并利用算例已有的求解设置。请求支持 `Idempotency-Key`，详情、日志及取消复用通用作业接口。 [迭代命令参考](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/flu_tcl/x1-1000010.html)

成功发布须有退出码 0、符合 CFF 文件标识的 `result.cas.h5` 与 `result.dat.h5` 以及非空 `fluent.log`。产物写入 `C-run/fluent/<执行标识>/`，`analysis-receipt.json` 保存输入与产物摘要，并记录所用迭代、时间步和初始化选项。 [文件和日志的 TUI 命令](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/flu_tcl/x1-50005.html)

## 协议验收

独立 Windows API 分别在文件系统和认证 SeaweedFS 上验证了大于 2 MiB 的二进制输入、初始化后稳态迭代、成对 CFF case/data 的瞬态续算及旧版 `.cas/.dat` 续算。试验的 CFF data 在 512 字节用户块后放置 HDF5 标识。两种存储均核对了启动参数、生成的 journal 及两份封存输入的摘要。 [文件系统回执](../../tmp/fluent-owned-protocol-rargc72v/checks.json) · [认证 S3 回执](../../tmp/fluent-owned-protocol-e_5a6lnt/checks.json) · [HDF5 用户块说明](https://docs.hdfgroup.org/archive/support/HDF5/doc/PSandPDF/HDF5_UsersGuide.PDF)

未配置时拒绝创建作业，拒绝无效迭代数、`.jou` 输入和混用 case/data 文件格式。启动失败保留日志，相同幂等键返回原作业，取消后验证实际 Windows 子进程已退出。作业拒绝缺少产物的零退出，三组协议产物均可发布并从算例读回。持证求解 0 次，模型调用 0 次。当前环境尚未配置厂商运行时，Fluent 实际迭代、数值结果与 Linux 厂商启动仍待验收。

```powershell
& platform/server/.venv/Scripts/python.exe -B -u platform/server/commercial_batch_protocol_acceptance.py --backend fluent
```

加 `--seaweed` 和本机 SeaweedFS 可执行文件路径可在独立认证对象存储复验。 [协议验收脚本](../server/commercial_batch_protocol_acceptance.py)
