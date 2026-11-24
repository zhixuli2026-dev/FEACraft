# FF-D12 前次运行引用

`referenceValues.<id>.previousRun` 接受 `publicationId` 与 `metric`。源是同一算例中已发布的后处理版本，可选当前或历史版本。服务在准备运行时核验该版本的发布清单和存档指标，取出指定标量或采样值。运行清单保存发布 ID、原作业 ID、指标名称及发布清单与指标的 SHA-256。单位、采样点和时序继续由公式及比较检查校验。

2026-11-19，使用已有 OpenFOAM 求解文件启动三次真实 VTK 后处理作业，取得三份发布，逐项核验进程退出。第二次已更改当前可见指标文件，按首个发布 ID 比较得到零差，回执记录首个版本的两个摘要。第三次改变采样坐标，比较报告标记 `sampling_mismatch`，首个版本原件仍可读取。缺失的发布 ID 在启动前被正式 API 以 HTTP 400 拒绝。本次实测入口为算例文件输入，真实模型调用及模拟均为 0。 [正式验收脚本](../server/postprocess_previous_reference_acceptance.py) · [实际结果](../../tmp/d12-prior-reference-ev_2tbvi/previous-reference.json)

注册求解路径也完成了实测。在已停止 C05 夹具的只读副本上，同一 API 以实际 `icoFoam` 求解发布为当前输入，运行两次真实 VTK。第二次同时固定第一份历史发布，在工作目录清单中记录其摘要，比较误差为零。原发布可读，两次启动分别以原键重放，未创建新作业。首次试验因夹具使用了另一条有效运行路径收到 `input_stale`，切回原发布的 `C-run/case-1` 后通过。两次原生作业和隔离 API 均核验退出。 [注册入口脚本](../server/postprocess_registered_reference_acceptance.py) · [实际结果](../../tmp/d12-registered-reference-2diyc50c/registered-reference.json) · [错位路径回执](../../tmp/d12-registered-reference-bvcfhffo/registered-reference.json)
